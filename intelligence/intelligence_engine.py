"""
Intelligence Engine
===================

Top-level coordinator and the ONLY class ``core/`` imports from this package.

Pipeline for ``evaluate``:
    topic = extractor.extract_query(question)
    signal = cache.get(topic)                      # cache hit -> done
    articles = fetcher.fetch(topic)                # cache miss
    prob, conf, reason = analyzer.analyze(...)
    signal = MarketSignal(...)
    cache.set(topic, signal)
    return _summarize(signal, arb_edge)            # SignalSummary for the arb engine

Every step is wrapped so that any failure degrades to a neutral summary — the
arb engine must behave exactly as it would with no intelligence at all.
"""

import logging

from intelligence.ai_analyzer import AIAnalyzer
from intelligence.cache import SignalCache
from intelligence.news_fetcher import NewsFetcher
from intelligence.signal import MarketSignal, SignalSummary, classify_direction
from intelligence.topic_extractor import TopicExtractor

logger = logging.getLogger(__name__)


def build_engine(intel_config) -> "IntelligenceEngine | None":
    """Construct an IntelligenceEngine from config + environment variables.

    Returns None when intelligence is disabled or construction fails — callers
    treat a None engine as "intelligence unavailable" and proceed normally.
    Never raises. API keys come exclusively from the environment (no hardcoding):
    NEWSAPI_KEY, ANTHROPIC_API_KEY, and optional ANTHROPIC_BASE_URL (OpenClaw).
    """
    import os

    if not getattr(intel_config, "enabled", False):
        return None

    try:
        cache = SignalCache(ttl_minutes=intel_config.news.cache_ttl_minutes)
        fetcher = NewsFetcher(api_key=os.getenv("NEWSAPI_KEY"), cache=cache)
        analyzer = AIAnalyzer(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            model=intel_config.claude.model,
            max_tokens=intel_config.claude.max_tokens,
            timeout_seconds=intel_config.claude.timeout_seconds,
        )
        return IntelligenceEngine(
            fetcher=fetcher, analyzer=analyzer, config=intel_config, cache=cache
        )
    except Exception as e:  # noqa: BLE001 — optional layer, must never break boot
        logger.warning("[Intelligence] build_engine failed, continuing without: %s", e)
        return None


class IntelligenceEngine:
    """Coordinates extraction -> news -> Claude -> signal -> summary."""

    def __init__(self, fetcher: NewsFetcher, analyzer: AIAnalyzer, config, cache: SignalCache | None = None):
        """
        Args:
            fetcher: configured ``NewsFetcher``.
            analyzer: configured ``AIAnalyzer``.
            config: the ``intelligence`` config section (see CORE_HOOKS.md). Needs
                ``mode``, ``min_confidence``, ``min_edge_boost``, ``max_position_boost``,
                and a ``news`` sub-config with ``lookback_hours``/``max_articles``/``sources``.
            cache: optional ``SignalCache`` (created from config TTL if omitted).
        """
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.config = config
        self.cache = cache or SignalCache(
            ttl_minutes=getattr(config.news, "cache_ttl_minutes", 10)
        )
        self.extractor = TopicExtractor(analyzer=analyzer)

    async def evaluate(
        self,
        market_id: str,
        market_question: str,
        current_yes_price: float,
        arb_edge: float,
    ) -> SignalSummary:
        """Produce a ``SignalSummary`` for one opportunity. Never raises."""
        try:
            signal = await self._get_or_build_signal(market_id, market_question, current_yes_price)
        except Exception as e:  # noqa: BLE001 — advisory layer must never break core
            logger.warning("[Intelligence] evaluate failed for %s: %s", market_id, e)
            return SignalSummary.neutral(arb_edge, reason=f"Intelligence error: {e}")

        if signal is None:
            return SignalSummary.neutral(arb_edge, reason="No signal produced")

        logger.info(
            "[Intelligence] Signal for %s: %s (confidence: %.2f)%s",
            market_id, signal.direction, signal.confidence,
            " [cached]" if signal.cache_hit else "",
        )
        return self._summarize(signal, arb_edge)

    async def _get_or_build_signal(
        self, market_id: str, market_question: str, current_yes_price: float
    ) -> MarketSignal | None:
        """Return a cached signal or build a fresh one (extract -> news -> Claude)."""
        topic = await self.extractor.extract_query(market_question)
        if not topic:
            return None

        cached = self.cache.get(topic)
        if cached is not None:
            return cached

        news_cfg = self.config.news
        articles = await self.fetcher.fetch(
            topic=topic,
            lookback_hours=getattr(news_cfg, "lookback_hours", 4),
            max_articles=getattr(news_cfg, "max_articles", 5),
            sources=getattr(news_cfg, "sources", None) or None,
        )
        if articles:
            logger.info('[Intelligence] Fetched %d articles for "%s"', len(articles), topic)

        ai_prob, confidence, reasoning = await self.analyzer.analyze(
            market_question=market_question,
            current_yes_price=current_yes_price,
            articles=articles,
            lookback_hours=getattr(news_cfg, "lookback_hours", 4),
        )

        signal = MarketSignal(
            market_id=market_id,
            market_question=market_question,
            current_yes_price=current_yes_price,
            ai_probability=ai_prob,
            confidence=confidence,
            direction=classify_direction(ai_prob, current_yes_price, confidence),
            reasoning=reasoning,
            news_headlines=[a.title for a in articles],
        )
        self.cache.set(topic, signal)
        return signal

    # ------------------------------------------------------------------
    # DECISION POINT — signal -> trade decision policy (moderate, tiered).
    #
    # This maps a MarketSignal onto should_filter / should_boost / adjusted_edge.
    # It encodes risk appetite and is the most opinionated logic in the module.
    #
    # Tiered policy (between "conservative" and "aggressive"):
    #   |gap| <= min_edge_boost            -> no action (market and AI agree)
    #   min_edge_boost < |gap| < min_edge_filter, directional -> BOOST
    #   |gap| >= min_edge_filter, directional                 -> FILTER (+ BOOST in "both")
    #
    # Rationale: a *small* divergence is noise; a *moderate* divergence is a
    # directional opportunity worth flagging; only a *large* adverse divergence
    # means news likely hasn't been priced in yet, so the cross-platform gap may
    # close against us — skip that arb. Tune min_edge_filter to slide the policy
    # toward conservative (lower) or aggressive (higher).
    # ------------------------------------------------------------------
    def _summarize(self, signal: MarketSignal, arb_edge: float) -> SignalSummary:
        """Translate a signal into a trade decision for the arb engine."""
        mode = getattr(self.config, "mode", "filter")
        min_conf = getattr(self.config, "min_confidence", 0.65)
        min_boost = getattr(self.config, "min_edge_boost", 0.03)
        # New knob: how large an adverse gap must be before we *filter* an arb.
        # Defaults to 0.10 when absent from config (no config change required yet).
        min_filter = getattr(self.config, "min_edge_filter", 0.10)

        if signal.confidence < min_conf:
            return SignalSummary(
                signal=signal,
                should_filter=False,
                should_boost=False,
                adjusted_edge=arb_edge,
                reason=f"Low confidence ({signal.confidence:.2f} < {min_conf})",
            )

        gap = signal.edge_vs_market  # +bullish / -bearish
        magnitude = abs(gap)
        directional = signal.direction in ("bullish", "bearish")

        should_filter = (
            directional and magnitude >= min_filter and mode in ("filter", "both")
        )
        should_boost = (
            directional and magnitude > min_boost and mode in ("boost", "both")
        )

        # Nudge the edge by the AI/market gap, capped so a single signal can't
        # dominate the underlying arb economics.
        adjusted_edge = arb_edge + max(-0.05, min(0.05, gap))

        if should_filter:
            reason = (
                f"AI {signal.direction} (p={signal.ai_probability:.2f} vs "
                f"mkt={signal.current_yes_price:.2f}, conf={signal.confidence:.2f}, "
                f"gap={gap:+.2f} >= {min_filter}) — news likely unpriced, skipping arb"
            )
        elif should_boost:
            reason = (
                f"AI {signal.direction} boost: gap {gap:+.2f}, conf={signal.confidence:.2f}"
            )
        else:
            reason = f"AI {signal.direction}, gap {gap:+.2f} below thresholds, no action"

        return SignalSummary(
            signal=signal,
            should_filter=should_filter,
            should_boost=should_boost,
            adjusted_edge=adjusted_edge,
            reason=reason,
        )
