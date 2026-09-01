"""Small, explicit calendar for the scored window.

This is context, not a trading rule.  The calendar is deliberately host-owned and
source-labelled so generated code never has to infer an event from headlines or
confuse a scheduled release with its (unknown) outcome.
"""
from __future__ import annotations

import datetime as dt

from agent.config import ET, WINDOW_CLOSE


CALENDAR_CHECKED_AT = "2026-09-01"

# Primary-source schedules checked on CALENDAR_CHECKED_AT.  `market_relevance` is
# a descriptive grouping, not a forecast of impact and not an entry blackout.
EVENTS = (
    {
        "at_et": "2026-09-01T10:00:00-04:00",
        "name": "Job Openings and Labor Turnover Survey (July)",
        "publisher": "U.S. Bureau of Labor Statistics",
        "market_relevance": "labour_market",
        "source_url": "https://www.bls.gov/schedule/2026/09_sched_list.htm",
    },
    {
        "at_et": "2026-09-01T10:00:00-04:00",
        "name": "Construction Spending (July)",
        "publisher": "U.S. Census Bureau",
        "market_relevance": "broad_macro",
        "source_url": "https://www.census.gov/economic-indicators/calendar-listview.html",
    },
    {
        "at_et": "2026-09-02T10:00:00-04:00",
        "name": "Manufacturers' Shipments, Inventories and Orders (July)",
        "publisher": "U.S. Census Bureau",
        "market_relevance": "growth_and_manufacturing",
        "source_url": "https://www.census.gov/economic-indicators/calendar-listview.html",
    },
    {
        "at_et": "2026-09-02T14:00:00-04:00",
        "name": "Federal Reserve Beige Book",
        "publisher": "Federal Reserve Board",
        "market_relevance": "monetary_policy_context",
        "source_url": "https://www.federalreserve.gov/newsevents/2026-september.htm",
    },
    {
        "at_et": "2026-09-03T08:30:00-04:00",
        "name": "Productivity and Costs, revised (Q2)",
        "publisher": "U.S. Bureau of Labor Statistics",
        "market_relevance": "growth_and_inflation",
        "source_url": "https://www.bls.gov/schedule/2026/09_sched_list.htm",
    },
    {
        "at_et": "2026-09-03T08:30:00-04:00",
        "name": "U.S. International Trade in Goods and Services (July)",
        "publisher": "U.S. Bureau of Economic Analysis / Census Bureau",
        "market_relevance": "broad_macro",
        "source_url": "https://www.bea.gov/news/schedule/full",
    },
    {
        "at_et": "2026-09-03T09:05:00-04:00",
        "name": "Federal Reserve Governor Barr speech",
        "publisher": "Federal Reserve Board",
        "market_relevance": "monetary_policy_context",
        "source_url": "https://www.federalreserve.gov/newsevents/2026-september.htm",
    },
)


def context(now: dt.datetime, *, recent_hours: float = 6.0,
            horizon_hours: float = 72.0) -> dict:
    """Return recent and upcoming events with an unambiguous ET clock.

    Events after the competition window are irrelevant to these positions and are
    omitted.  No event outcome, consensus estimate, or surprise is implied.
    """
    now_et = now.astimezone(ET)
    earliest = now_et - dt.timedelta(hours=float(recent_hours))
    latest = min(now_et + dt.timedelta(hours=float(horizon_hours)), WINDOW_CLOSE)
    rows = []
    for raw in EVENTS:
        at = dt.datetime.fromisoformat(str(raw["at_et"])).astimezone(ET)
        if not earliest <= at <= latest:
            continue
        minutes = round((at - now_et).total_seconds() / 60.0, 1)
        rows.append({
            **raw,
            "timing": "upcoming" if minutes >= 0 else "recently_released",
            "minutes_until": minutes,
        })
    rows.sort(key=lambda row: row["at_et"])
    upcoming = [row for row in rows if row["timing"] == "upcoming"]
    return {
        "label": "scheduled_event_context",
        "as_of_et": now_et.isoformat(timespec="seconds"),
        "calendar_checked_at": CALENDAR_CHECKED_AT,
        "advisory_only": True,
        "not_a_blackout_gate": True,
        "outcomes_not_included": True,
        "interpretation": (
            "Known release timing can change event risk and decision-lag cost. "
            "It does not say whether the release will surprise, in which direction, "
            "or whether a trade should be rejected."),
        "next_event": upcoming[0] if upcoming else None,
        "events": rows,
    }
