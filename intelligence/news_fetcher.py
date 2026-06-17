"""
News Fetcher
============

Fetches recent news headlines from NewsAPI.org via ``httpx`` (async).
Free tier: 500 requests/day, articles from the last 30 days, English only.

Error handling is intentionally forgiving — this layer is advisory, so every
failure mode degrades to "return an empty list" rather than raising:
- 429 rate limit  -> warn, return []
- 401 bad key     -> error once, disable fetcher for the session, return []
- network timeout -> return []
- empty results   -> return [] (normal, not an error)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

_NEWSAPI_URL = "https://newsapi.org/v2/everything"
_DEFAULT_TIMEOUT = 5.0  # seconds


@dataclass
class NewsArticle:
    title: str
    description: str | None
    source: str
    published_at: datetime
    url: str


class NewsFetcher:
    """Fetches recent headlines for a topic from NewsAPI."""

    def __init__(self, api_key: str | None, cache=None, timeout: float = _DEFAULT_TIMEOUT):
        """
        Args:
            api_key: NewsAPI key (from ``NEWSAPI_KEY``). If falsy, the fetcher is
                inert and always returns [].
            cache: optional ``SignalCache``. The engine-level signal cache is the
                primary dedupe, so this is accepted for API compatibility and
                reserved for future article-level caching.
            timeout: per-request timeout in seconds.
        """
        self.api_key = api_key
        self._cache = cache
        self.timeout = timeout
        self._disabled = not bool(api_key)

    async def fetch(
        self,
        topic: str,
        lookback_hours: int = 4,
        max_articles: int = 5,
        sources: list[str] | None = None,
    ) -> list[NewsArticle]:
        """Fetch up to ``max_articles`` recent articles about ``topic``.

        Returns [] on any error or when disabled. Never raises.
        """
        if self._disabled or not topic:
            return []

        params = {
            "q": topic,
            "from": self._lookback_iso(lookback_hours),
            "sortBy": "relevancy",
            "language": "en",
            "pageSize": max_articles,
            "apiKey": self.api_key,
        }
        if sources:
            params["sources"] = ",".join(sources)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(_NEWSAPI_URL, params=params)
        except httpx.TimeoutException:
            logger.warning("[Intelligence] NewsAPI timeout for topic %r", topic)
            return []
        except httpx.HTTPError as e:
            logger.warning("[Intelligence] NewsAPI request error for %r: %s", topic, e)
            return []

        if resp.status_code == 429:
            logger.warning("[Intelligence] NewsAPI rate limited (429) — skipping topic %r", topic)
            return []
        if resp.status_code == 401:
            logger.error("[Intelligence] NewsAPI 401 (bad key) — disabling fetcher for session")
            self._disabled = True
            return []
        if resp.status_code != 200:
            logger.warning("[Intelligence] NewsAPI returned %s for %r", resp.status_code, topic)
            return []

        return self._parse_articles(resp.json(), max_articles)

    def _parse_articles(self, payload: dict, max_articles: int) -> list[NewsArticle]:
        """Parse a NewsAPI /everything response into NewsArticle objects."""
        articles: list[NewsArticle] = []
        for raw in (payload.get("articles") or [])[:max_articles]:
            try:
                articles.append(
                    NewsArticle(
                        title=raw.get("title") or "",
                        description=raw.get("description"),
                        source=(raw.get("source") or {}).get("name") or "unknown",
                        published_at=self._parse_dt(raw.get("publishedAt")),
                        url=raw.get("url") or "",
                    )
                )
            except Exception as e:  # noqa: BLE001 — skip malformed article, keep the rest
                logger.debug("[Intelligence] Skipping malformed article: %s", e)
        return articles

    @staticmethod
    def _lookback_iso(lookback_hours: int) -> str:
        """ISO-8601 timestamp ``lookback_hours`` ago (UTC), for NewsAPI ``from``."""
        return (datetime.utcnow() - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _parse_dt(value: str | None) -> datetime:
        """Parse a NewsAPI ISO-8601 timestamp, tolerating the trailing 'Z'."""
        if not value:
            return datetime.utcnow()
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.utcnow()
