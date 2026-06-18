"""
Resolution Parser
=================

Extracts a concise *resolution criteria* string from a Polymarket market
description, so it can be given to Claude as a distinct field (FEAT-02).

Why this matters: a market can have bullish news but still resolve NO on a
technicality in its resolution criteria (e.g. "must happen at the June FOMC
meeting per the official Fed press release"). Without the criteria, Claude may
read "Fed signals pause" as bullish for a "Will the Fed raise rates?" YES
position when the criteria actually requires a specific event by a specific date.

Polymarket's Gamma API already populates ``Market.description`` with this text;
this parser just cleans and trims it for the prompt (no network calls).
"""

import re

# Sentences mentioning these are kept first when a description is too long.
_RESOLUTION_KEYWORDS = (
    "resolve", "resolution", "resolved", "resolves",
    "settle", "settled", "criteria", "deadline", "expire",
)


class ResolutionParser:
    """Cleans a raw market description into prompt-ready resolution criteria."""

    def __init__(self, max_chars: int = 500):
        self.max_chars = max_chars

    def extract(self, description: str | None) -> str | None:
        """Return cleaned resolution criteria, or None if there's nothing useful.

        - Collapses whitespace.
        - If short enough, returns the whole thing.
        - If too long, keeps sentences that mention resolution keywords (falling
          back to a head-truncation), so the most relevant text survives the cap.
        """
        if not description or not description.strip():
            return None

        text = " ".join(description.split())  # collapse all whitespace runs
        if len(text) <= self.max_chars:
            return text

        sentences = re.split(r"(?<=[.!?])\s+", text)
        relevant = [s for s in sentences if self._mentions_resolution(s)]
        candidate = " ".join(relevant) if relevant else text

        if len(candidate) <= self.max_chars:
            return candidate
        return candidate[: self.max_chars].rstrip() + "…"

    @staticmethod
    def _mentions_resolution(sentence: str) -> bool:
        lower = sentence.lower()
        return any(keyword in lower for keyword in _RESOLUTION_KEYWORDS)
