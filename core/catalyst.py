"""
Catalyst proximity scoring for directional trading.

`catalyst_proximity` returns a boost in [0, 1] when a market's title or
category matches a calendar event keyword AND the event is within the
specified window.  Boost = 1 - (hours_to_event / window_hours), clamped
to [0, 1].  Returns the maximum across all matching entries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def catalyst_proximity(
    market_title: str,
    market_category: str,
    now: datetime,
    calendar: list[dict[str, Any]],
    window_hours: float,
) -> float:
    """Return boost in [0, 1] for the closest matching catalyst within window.

    Args:
        market_title: Market question/title string.
        market_category: Market category string.
        now: Timezone-aware current datetime (UTC recommended).
        calendar: List of dicts with keys: name, date (ISO str), keywords (list[str]).
        window_hours: How many hours ahead to consider a catalyst "near".

    Returns:
        Float in [0, 1]; 0.0 when no matching catalyst is within the window.
    """
    if not calendar:
        return 0.0

    title_lower = market_title.lower()
    category_lower = market_category.lower()
    max_boost = 0.0

    # Ensure now is timezone-aware
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    for entry in calendar:
        keywords: list[str] = entry.get("keywords", [])
        date_str: str = entry.get("date", "")

        if not keywords or not date_str:
            continue

        # Check keyword match (case-insensitive substring)
        matched = any(
            kw.lower() in title_lower or kw.lower() in category_lower
            for kw in keywords
        )
        if not matched:
            continue

        # Parse entry date
        try:
            entry_dt = datetime.fromisoformat(date_str)
        except ValueError:
            continue

        # Make entry_dt timezone-aware
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)

        hours_to = (entry_dt - now).total_seconds() / 3600.0

        # Must be in the future and within window
        if hours_to < 0 or hours_to > window_hours:
            continue

        # Boost: 1.0 when hours_to=0 (event now), 0.0 at hours_to=window_hours
        boost = max(0.0, min(1.0, 1.0 - hours_to / window_hours))
        if boost > max_boost:
            max_boost = boost

    return max_boost
