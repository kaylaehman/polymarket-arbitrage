"""
Topic Extractor
===============

Converts a prediction-market question into a concise news search query for
NewsAPI. Produces a *search query string* only — it does NOT re-implement the
fuzzy market-matching logic in ``core/cross_platform_arb.py``.

Two extraction paths:
- ``extract`` (sync, always available) — regex/stopword stripping.
- ``extract_query`` (async) — uses Claude for a sharper query if an analyzer is
  injected, falling back to the regex path on any failure.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Question words and filler removed during regex extraction.
_QUESTION_WORDS = {
    "will", "would", "does", "do", "did", "is", "are", "was", "were",
    "can", "could", "should", "when", "who", "what", "which", "how", "why",
}
_FILLER_WORDS = {
    "the", "a", "an", "at", "in", "on", "by", "for", "of", "to", "be",
    "before", "after", "than", "then", "with", "and", "or", "this", "that",
}
_STOPWORDS = _QUESTION_WORDS | _FILLER_WORDS

# System prompt for the Claude-assisted extraction path.
_EXTRACT_SYSTEM_PROMPT = (
    "You extract a 4-6 word news search query from a prediction market question.\n"
    "Respond with ONLY the search query, no punctuation, no explanation."
)

_MAX_WORDS = 6


class TopicExtractor:
    """Extracts a short news-search query from a market question."""

    def __init__(self, analyzer=None):
        """
        Args:
            analyzer: optional ``AIAnalyzer`` (or any object exposing
                ``async complete(system, user) -> str``). If provided,
                ``extract_query`` uses Claude; otherwise it falls back to regex.
        """
        self._analyzer = analyzer

    def extract(self, market_question: str) -> str:
        """Regex/stopword extraction. Always available, never raises.

        Example:
            "Will the Federal Reserve raise interest rates at the June 2026 FOMC meeting?"
            -> "Federal Reserve raise interest rates June"
        """
        if not market_question:
            return ""

        # Strip punctuation to spaces, collapse whitespace.
        cleaned = re.sub(r"[^\w\s]", " ", market_question)
        words = cleaned.split()

        kept: list[str] = []
        for word in words:
            lower = word.lower()
            # Keep numbers/years, proper-noun-ish tokens, and any non-stopword.
            if lower in _STOPWORDS and not word[0].isupper() and not word.isdigit():
                continue
            kept.append(word)

        return " ".join(kept[:_MAX_WORDS])

    async def extract_query(self, market_question: str) -> str:
        """Claude-assisted extraction with a regex fallback.

        Never raises — on any analyzer failure it returns the regex result.
        """
        fallback = self.extract(market_question)

        if self._analyzer is None:
            return fallback

        try:
            query = await self._analyzer.complete(
                system=_EXTRACT_SYSTEM_PROMPT,
                user=market_question,
            )
            query = (query or "").strip()
            if query:
                # Defensive trim — keep the query short even if Claude over-answers.
                return " ".join(query.split()[:_MAX_WORDS])
        except Exception as e:  # noqa: BLE001 — advisory path, must never raise
            logger.warning("[Intelligence] Claude topic extraction failed, using regex: %s", e)

        return fallback
