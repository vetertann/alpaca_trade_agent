"""Rolling in-memory series.

Historical endpoints exclude the most recent fifteen minutes, so the question a
cycle most needs to answer -- what moved since the last cycle -- cannot be served
from history for the interval that matters. The watcher accumulates its own
series from the stream instead.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as stats
import threading
from collections import deque
from pathlib import Path

from agent.config import ET

MINUTES_PER_YEAR = 252 * 390
GAP_DOMINANT_EM = 0.50
GAP_CONTINUATION_EM = 0.25


class RollingSeries:
    """Per-symbol price history, second and minute resolution, session-scoped."""

    def __init__(self, max_seconds: int = 3600, max_minutes: int = 800):
        self.sec: dict[str, deque] = {}
        self.min: dict[str, deque] = {}
        self._max_s, self._max_m = max_seconds, max_minutes
        self._cur_min: dict[str, tuple[int, float]] = {}
        self._session_reference: dict[str, dict] = {}
        # Stream callbacks write on the asyncio thread while decision preflight
        # reads in `asyncio.to_thread`. Deque operations are individually atomic,
        # but iteration is not: an append during iteration raises RuntimeError.
        self._lock = threading.RLock()

    def observe(self, symbol: str, price: float, when: dt.datetime) -> None:
        if price <= 0:
            return
        with self._lock:
            s = self.sec.setdefault(symbol, deque(maxlen=self._max_s))
            s.append((when, price))
            bucket = int(when.timestamp() // 60)
            cur = self._cur_min.get(symbol)
            if cur and cur[0] != bucket:
                m = self.min.setdefault(symbol, deque(maxlen=self._max_m))
                m.append((dt.datetime.fromtimestamp(
                    cur[0] * 60, dt.timezone.utc), cur[1]))
            self._cur_min[symbol] = (bucket, price)

    # ---- reads -------------------------------------------------------------
    def last(self, symbol: str) -> float | None:
        with self._lock:
            s = self.sec.get(symbol)
            return s[-1][1] if s else None

    def minute_closes(self, symbol: str) -> list[float]:
        with self._lock:
            return [p for _, p in self.min.get(symbol, ())]

    def session_range(self, symbol: str) -> tuple[float, float] | None:
        with self._lock:
            prices = [p for _, p in self.sec.get(symbol, ())]
        return (min(prices), max(prices)) if prices else None

    def move_since(self, symbol: str, when: dt.datetime) -> float | None:
        """Fractional move from the first observation at or after `when` to now."""
        with self._lock:
            s = self.sec.get(symbol)
            if not s:
                return None
            ref = next((p for t, p in s if t >= when), None)
            return None if not ref else s[-1][1] / ref - 1.0

    def realized_vol(self, symbol: str, lookback: int = 60) -> float | None:
        """Annualised from minute log returns. The core volatility-state input."""
        closes = self.minute_closes(symbol)[-(lookback + 1):]
        if len(closes) < 12:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 10:
            return None
        return stats.pstdev(rets) * math.sqrt(MINUTES_PER_YEAR)

    def set_session_reference(self, symbol: str, session_date: dt.date, *,
                              prior_close: float, session_open: float,
                              expected_move: float, source: str) -> None:
        """Install restart-proof session anchors obtained from broker bars."""
        values = (float(prior_close), float(session_open), float(expected_move))
        if any(not math.isfinite(value) or value <= 0 for value in values):
            return
        with self._lock:
            self._session_reference[str(symbol).upper()] = {
                "session_date": session_date.isoformat(),
                "prior_close": values[0], "session_open": values[1],
                "expected_move": values[2], "source": str(source),
            }

    def _session_minutes(self, symbol: str,
                         now: dt.datetime | None = None) -> list[tuple[dt.datetime, float]]:
        """Return a stable, current-session minute series including the live minute.

        The stream carries quote midpoints rather than OHLCV bars.  Naming this
        explicitly matters: these observations describe price direction, not order
        flow or volume-weighted execution pressure.
        """
        with self._lock:
            rows = list(self.min.get(symbol, ()))
            current = self._cur_min.get(symbol)
            if current:
                rows.append((dt.datetime.fromtimestamp(
                    current[0] * 60, dt.timezone.utc), float(current[1])))
        if not rows:
            return []
        reference = (now or rows[-1][0]).astimezone(ET)
        session_open = reference.replace(hour=9, minute=30, second=0, microsecond=0)
        session_close = reference.replace(hour=16, minute=0, second=0, microsecond=0)
        return [(when, price) for when, price in rows
                if session_open <= when.astimezone(ET) <= session_close]

    @staticmethod
    def _window_metrics(rows: list[tuple[dt.datetime, float]], minutes: int,
                        latest_at: dt.datetime) -> tuple[float | None, float | None,
                                                         float | None]:
        """Return fractional return, normalized displacement and path efficiency.

        Normalized displacement is signed log return divided by root-sum-square
        minute movement.  Efficiency is absolute net movement divided by total
        absolute path movement.  Both make trend strength comparable without
        pretending that a raw five-basis-point move means the same thing at every
        volatility level.
        """
        cutoff = latest_at - dt.timedelta(minutes=minutes)
        window = [(when, price) for when, price in rows if when >= cutoff]
        if len(window) < 2 or window[0][0] > cutoff + dt.timedelta(minutes=2):
            return None, None, None
        prices = [price for _, price in window if price > 0]
        if len(prices) < 2:
            return None, None, None
        logs = [math.log(b / a) for a, b in zip(prices, prices[1:])]
        net = math.log(prices[-1] / prices[0])
        noise = math.sqrt(sum(value * value for value in logs))
        path = sum(abs(value) for value in logs)
        return (prices[-1] / prices[0] - 1.0,
                net / noise if noise > 0 else 0.0,
                abs(net) / path if path > 0 else 0.0)

    def directional_context(self, symbol: str,
                            now: dt.datetime | None = None) -> dict:
        """Host-labelled intraday direction from recent quote-midpoint history.

        This deliberately reports its ingredients and coverage.  The label is a
        compact description of observed price action, not a return forecast.
        """
        rows = self._session_minutes(symbol, now)
        if not rows:
            return {
                "symbol": symbol, "source": "streamed equity quote midpoints",
                "observed_at_et": None, "sample_count": 0,
                "sample_coverage_minutes": 0.0,
                "classification": "insufficient_data", "strength": "none",
                "classification_basis": ["no current-session minute observations"],
            }
        latest_at, latest = rows[-1]
        coverage = max((latest_at - rows[0][0]).total_seconds() / 60.0, 0.0)
        horizons = (1, 5, 15, 30, 60)
        metrics = {minutes: self._window_metrics(rows, minutes, latest_at)
                   for minutes in horizons}
        low = min(price for _, price in rows)
        high = max(price for _, price in rows)
        range_position = ((latest - low) / (high - low)) if high > low else 0.5

        positive: list[str] = []
        negative: list[str] = []
        for minutes in (5, 15, 30, 60):
            change, normalized, _ = metrics[minutes]
            if change is None or normalized is None:
                continue
            # Half a root-sum-square move rejects direction labels built from
            # microscopic drift while remaining relative to current noise.
            if normalized >= 0.5:
                positive.append(f"{minutes}m")
            elif normalized <= -0.5:
                negative.append(f"{minutes}m")

        basis: list[str] = []
        if positive:
            basis.append("positive normalized movement over " + ", ".join(positive))
        if negative:
            basis.append("negative normalized movement over " + ", ".join(negative))
        if range_position >= 0.65:
            basis.append(f"price is in the upper {100 * (1 - range_position):.0f}% "
                         "of the observed session range")
        elif range_position <= 0.35:
            basis.append(f"price is in the lower {100 * range_position:.0f}% "
                         "of the observed session range")

        available = sum(metrics[m][0] is not None for m in (5, 15, 30, 60))
        if coverage < 15 or available < 2:
            classification, strength = "insufficient_data", "none"
            basis.append("less than two usable horizons or 15 minutes of coverage")
        elif len(positive) >= 2 and len(positive) > len(negative) and range_position >= 0.55:
            classification = "bullish"
            strength = "strong" if len(positive) >= 3 and not negative else "moderate"
        elif len(negative) >= 2 and len(negative) > len(positive) and range_position <= 0.45:
            classification = "bearish"
            strength = "strong" if len(negative) >= 3 and not positive else "moderate"
        elif positive and negative:
            classification, strength = "mixed", "weak"
            basis.append("usable horizons disagree")
        else:
            classification, strength = "neutral", "weak"
            basis.append("movement is not persistent enough for a directional label")

        out = {
            "symbol": symbol,
            "source": "streamed equity quote midpoints",
            "observed_at_et": latest_at.astimezone(ET).isoformat(timespec="seconds"),
            "sample_count": len(rows),
            "sample_coverage_minutes": round(coverage, 1),
            "last_price": round(latest, 4),
            "session_low": round(low, 4),
            "session_high": round(high, 4),
            "session_range_position": round(range_position, 4),
            "classification": classification,
            "strength": strength,
            "classification_basis": basis or ["no persistent directional evidence"],
        }
        for minutes in horizons:
            change, normalized, efficiency = metrics[minutes]
            out[f"return_{minutes}m"] = (round(change, 6)
                                          if change is not None else None)
            out[f"normalized_move_{minutes}m"] = (
                round(normalized, 4) if normalized is not None else None)
            out[f"trend_efficiency_{minutes}m"] = (
                round(efficiency, 4) if efficiency is not None else None)
        if len(rows) >= 2 and rows[0][1] > 0:
            out["return_since_observed_session_open"] = round(
                latest / rows[0][1] - 1.0, 6)
        else:
            out["return_since_observed_session_open"] = None

        with self._lock:
            reference = dict(self._session_reference.get(symbol.upper()) or {})
        if reference.get("session_date") == latest_at.astimezone(ET).date().isoformat():
            prior = float(reference["prior_close"])
            opened = float(reference["session_open"])
            expected = float(reference["expected_move"])
            gap_em = (opened - prior) / expected
            intraday_em = (latest - opened) / expected
            out["session_reference"] = {
                **reference,
                "available": True,
                "gap_move_em": round(gap_em, 4),
                "intraday_move_em": round(intraday_em, 4),
                "interpretation": (
                    "independent signed moves; positive is up, negative is down; "
                    "values are fractions of one expected daily move"),
            }
            gap_sign = 1.0 if gap_em > 0 else -1.0
            continuation = gap_sign * intraday_em
            if (abs(gap_em) >= GAP_DOMINANT_EM
                    and continuation < GAP_CONTINUATION_EM
                    and out["classification"] == (
                        "bullish" if gap_em > 0 else "bearish")):
                out["classification"] = "neutral"
                out["strength"] = "weak"
                out["classification_basis"].append(
                    f"gap dominated ({gap_em:+.2f} EM) without "
                    f"{GAP_CONTINUATION_EM:.2f} EM same-direction intraday "
                    f"continuation ({intraday_em:+.2f} EM); directional alignment "
                    "is not promoted")
        else:
            out["session_reference"] = {
                "available": False,
                "status": "unavailable",
                "gap_move_em": None,
                "intraday_move_em": None,
                "interpretation": (
                    "prior close and official 09:30 ET opening bar are required; "
                    "missing data is never treated as a zero gap"),
            }
        return out

    def directional_contexts(self, symbols: list[str] | tuple[str, ...] | set[str],
                             now: dt.datetime | None = None) -> dict[str, dict]:
        """Add explicitly labelled cross-asset confirmation to each context."""
        ordered = sorted({str(symbol).upper() for symbol in symbols})
        contexts = {symbol: self.directional_context(symbol, now) for symbol in ordered}
        for symbol, row in contexts.items():
            peers = {
                peer: {
                    "classification": value.get("classification"),
                    "return_15m": value.get("return_15m"),
                }
                for peer, value in contexts.items() if peer != symbol
            }
            classification = row.get("classification")
            same = (1 if classification in ("bullish", "bearish") else 0) + sum(
                value.get("classification") == classification for value in peers.values())
            row["cross_asset_confirmation"] = {
                "peers": peers,
                "same_direction_count_including_self": same,
                "interpretation": (
                    f"{same} observed underlyings share the {classification} label"
                    if classification in ("bullish", "bearish")
                    else "the primary underlying has no directional label to confirm"),
            }
        return contexts

    # ---- durability --------------------------------------------------------
    def checkpoint(self, path: str | Path) -> None:
        """A restart mid-session recovers its recent window rather than starting blind."""
        with self._lock:
            payload = {
                "schema_version": 3,
                "minute": {sym: [(t.isoformat(), p) for t, p in dq]
                           for sym, dq in self.min.items()},
                "second": {sym: [(t.isoformat(), p) for t, p in dq]
                           for sym, dq in self.sec.items()},
                "current_minute": {sym: [bucket, price]
                                   for sym, (bucket, price) in self._cur_min.items()},
                "session_reference": self._session_reference,
            }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        with temporary.open("w") as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def restore(self, path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        raw = json.loads(p.read_text())
        if raw.get("schema_version") in (2, 3):
            minute_rows = raw.get("minute") or {}
            second_rows = raw.get("second") or {}
            current_minute = {
                sym: (int(value[0]), float(value[1]))
                for sym, value in (raw.get("current_minute") or {}).items()}
            session_reference = (raw.get("session_reference") or {}
                                 if raw.get("schema_version") == 3 else {})
        else:  # original minute-only checkpoint
            minute_rows, second_rows = raw, {}
            current_minute = {}
            session_reference = {}
        with self._lock:
            self._cur_min = current_minute
            self._session_reference = {
                str(symbol).upper(): dict(value)
                for symbol, value in session_reference.items()}
            for sym, rows in minute_rows.items():
                dq = self.min.setdefault(sym, deque(maxlen=self._max_m))
                for iso, price in rows:
                    dq.append((dt.datetime.fromisoformat(iso), price))
            for sym, rows in second_rows.items():
                dq = self.sec.setdefault(sym, deque(maxlen=self._max_s))
                for iso, price in rows:
                    dq.append((dt.datetime.fromisoformat(iso), price))
        return True
