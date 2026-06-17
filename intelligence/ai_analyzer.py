"""
AI Analyzer
===========

Sends market context + recent news to Claude and parses a structured signal.

Uses ``httpx`` directly against the Anthropic Messages API (``/v1/messages``)
rather than the ``anthropic`` SDK, per project conventions:
- CLAUDE.md mandates httpx-only async calls (no new heavy deps).
- ``base_url`` is configurable so requests can be routed through the OpenClaw
  proxy (``ANTHROPIC_BASE_URL``) instead of api.anthropic.com.
"""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"

_ANALYZE_SYSTEM_PROMPT = """You are a prediction market analyst. You assess whether current market odds correctly reflect recent news. You respond ONLY in valid JSON with no other text.

Response format:
{
  "probability": 0.72,
  "confidence": 0.8,
  "reasoning": "..."
}

Rules:
- If news is irrelevant or insufficient, return probability close to current price and confidence below 0.5
- Never return confidence above 0.9 — prediction markets are inherently uncertain
- Base your estimate on the news provided, not your training data alone
- If you cannot assess the question, return {"probability": current_price, "confidence": 0.0, "reasoning": "Insufficient information"}"""


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class AIAnalyzer:
    """Wraps a single Claude Messages API call into a structured assessment."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 512,
        timeout_seconds: int = 8,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self._disabled = not bool(api_key)

    async def analyze(
        self,
        market_question: str,
        current_yes_price: float,
        articles: list,  # list[NewsArticle]
        lookback_hours: int = 4,
    ) -> tuple[float, float, str]:
        """Assess a market against recent news.

        Returns ``(ai_probability, confidence, reasoning)``. On any failure it
        degrades gracefully to ``(current_yes_price, 0.0, <reason>)`` — never raises.
        """
        if self._disabled:
            return current_yes_price, 0.0, "Analyzer disabled (no API key)"

        user_prompt = self._build_user_prompt(
            market_question, current_yes_price, articles, lookback_hours
        )

        try:
            text = await self.complete(_ANALYZE_SYSTEM_PROMPT, user_prompt)
        except Exception as e:  # noqa: BLE001 — advisory path, must never raise
            logger.warning("[Intelligence] Claude analyze failed: %s", e)
            return current_yes_price, 0.0, "Claude request failed"

        return self._parse_response(text, current_yes_price)

    async def complete(self, system: str, user: str) -> str:
        """Low-level helper: one Messages API call, returns the text content.

        Reused by ``TopicExtractor`` for the Claude-assisted extraction path.
        Raises on transport/HTTP errors so callers can decide how to degrade.
        """
        if self._disabled:
            raise RuntimeError("AIAnalyzer is disabled (no API key)")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages", headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()

        # Anthropic returns content as a list of blocks; concatenate text blocks.
        parts = [
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        return "".join(parts).strip()

    def _parse_response(self, text: str, current_yes_price: float) -> tuple[float, float, str]:
        """Parse Claude's JSON, stripping markdown fences and clamping ranges."""
        cleaned = self._strip_fences(text)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[Intelligence] Claude returned non-JSON: %r", text[:120])
            return current_yes_price, 0.0, "Parse error"

        probability = _clamp(float(data.get("probability", current_yes_price)))
        confidence = _clamp(float(data.get("confidence", 0.0)))
        reasoning = str(data.get("reasoning", "")).strip() or "No reasoning provided"
        return probability, confidence, reasoning

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove ```json ... ``` markdown fences if Claude wraps its output."""
        stripped = (text or "").strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Drop the opening fence (``` or ```json) and a trailing fence.
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        return stripped

    @staticmethod
    def _build_user_prompt(
        market_question: str,
        current_yes_price: float,
        articles: list,
        lookback_hours: int,
    ) -> str:
        if articles:
            headlines = "\n".join(
                f"- {a.title} ({a.source})"
                + (f": {a.description}" if getattr(a, "description", None) else "")
                for a in articles
            )
        else:
            headlines = "(no recent articles found)"

        return (
            f"Market question: {market_question}\n"
            f"Current YES price: {current_yes_price:.0%}\n\n"
            f"Recent news ({len(articles)} articles from last {lookback_hours} hours):\n"
            f"{headlines}\n\n"
            f"Based on this news, what is the true probability for YES?"
        )
