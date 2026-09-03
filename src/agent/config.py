"""Credential and profile resolution.

Two rules the rest of the system depends on:

* the active Alpaca profile is chosen explicitly -- there is no default;
* each account is asserted against an expected id supplied at runtime.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# FAQ distinguishes the economic mark from the formal measurement endpoint.
WINDOW_OPEN = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)
EOD_EQUITY_MARK = dt.datetime(2026, 9, 3, 16, 0, tzinfo=ET)
MEASUREMENT_END = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
# Compatibility/economic-horizon name used by valuation code. Options are valued
# at Thursday EOD, not at Friday's post-window snapshot timestamp.
WINDOW_CLOSE = EOD_EQUITY_MARK

# Entry admission is deliberately separate from session close and from exit
# enforcement.  Normal sessions stop adding risk at 15:45 ET; on the final
# scored session the book may still add risk until 15:55 ET.  The same values
# are consumed by the decision loop, preflight and the broker-submit boundary.
ENTRY_OPEN_ET = dt.time(9, 45)
ENTRY_CUTOFF_ET = dt.time(15, 45)
FINAL_ENTRY_CUTOFF_ET = dt.time(15, 55)


def entry_cutoff_et(day: dt.date) -> dt.time:
    return FINAL_ENTRY_CUTOFF_ET if day == WINDOW_CLOSE.date() else ENTRY_CUTOFF_ET


def entry_submission_allowed(now: dt.datetime | None = None) -> tuple[bool, str]:
    """Last host-owned guard before any new-entry broker submission."""
    now_et = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ET)
    if not in_scored_window(now_et):
        return False, "outside the scored window"
    if now_et.weekday() >= 5:
        return False, "not a trading day"
    cutoff = entry_cutoff_et(now_et.date())
    if now_et.time() < ENTRY_OPEN_ET:
        return False, f"new entries open at {ENTRY_OPEN_ET.strftime('%H:%M')} ET"
    if now_et.time() >= cutoff:
        return False, f"new entries closed at {cutoff.strftime('%H:%M')} ET"
    return True, "entry window active"

# Total equity, not realised cash, is scored at WINDOW_CLOSE.  Contract expiry is
# therefore not an eligibility boundary: any active broker-listed option can
# contribute marked value at the score horizon.  Liquidity, score-window
# sensitivity and risk decide whether a tenor is useful; a calendar constant does
# not.

# A hand-edited .env should degrade to a warning, never to a silently missing provider.
ALIASES: dict[str, tuple[str, ...]] = {
    "ALPACA_SECRET_KEY": ("SECRET", "ALPACA_SECRET"),
    "OPENAI_API_KEY": ("OPEN_AI_API_KEY",),
    "ANTHROPIC_API_KEY": ("CLAUDE_API_KEY",),
    "FEATHERLESS_API_KEY": ("FEATHERLESS_KEY",),
}


class ConfigError(RuntimeError):
    pass


def load_env(path: str | Path = ".env") -> None:
    """Read .env into os.environ without clobbering anything already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


def get_key(name: str, *, required: bool = True) -> str | None:
    """Resolve a credential, accepting known aliases."""
    value = os.environ.get(name)
    if value:
        return value
    for alias in ALIASES.get(name, ()):
        value = os.environ.get(alias)
        if value:
            print(f"[config] using alias {alias} for {name}")
            return value
    if required:
        raise ConfigError(f"{name} is not set (aliases tried: {ALIASES.get(name, ())})")
    return None


@dataclass(frozen=True)
class Profile:
    """An explicitly chosen Alpaca account."""

    name: str
    api_key: str
    secret_key: str
    expected_account_id: str

    @property
    def is_competition(self) -> bool:
        return self.name == "competition"


def profile(name: str) -> Profile:
    """Build a profile. `name` must be given -- there is deliberately no default."""
    if name == "competition":
        p = Profile("competition", get_key("ALPACA_API_KEY"), get_key("ALPACA_SECRET_KEY"),
                    get_key("ALPACA_ACCOUNT_ID"))
    elif name == "dev":
        p = Profile("dev", get_key("DEV_ALPACA_API_KEY"), get_key("DEV_ALPACA_SECRET_KEY"),
                    get_key("DEV_ALPACA_ACCOUNT_ID"))
    else:
        raise ConfigError(f"unknown profile {name!r}; expected 'competition' or 'dev'")

    if not p.api_key.startswith("PK"):
        raise ConfigError(f"{p.name}: key does not look like a paper key (prefix {p.api_key[:2]!r})")
    if p.name == "dev" and p.api_key == os.environ.get("ALPACA_API_KEY"):
        raise ConfigError("dev profile resolved to the competition credentials")
    return p


def in_scored_window(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ET)
    return WINDOW_OPEN <= now < MEASUREMENT_END


def assert_paper(url: str) -> None:
    if url.rstrip("/") != PAPER_TRADING_URL:
        raise ConfigError(f"refusing non-paper trading endpoint: {url!r}")
