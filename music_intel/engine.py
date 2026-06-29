"""Phase 5 — coordinator: ingest -> project -> match markets -> edge -> alert.

Emits a TAGGED ChartSignal (source="chart-intel") so it integrates with the
existing intelligence pipeline as one weighted voice — it never silently
outvotes NewsAPI/Claude.

NO AUTO-EXECUTION (policy): this module never trades. ENABLE_CHART_EXECUTION
defaults false and is NEVER flipped here; even if the env var were set, the
execution path in this module is a logging no-op. Humans act on alerts.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from music_intel.config import MusicIntelConfig
from music_intel.edge import compute_edge
from music_intel.projection import project_number_one
from music_intel.sources.base import ChartRecord
from music_intel.sources.markets import parse_market_target

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChartSignal:
    source: str          # tag — always "chart-intel"
    market_id: str
    question: str
    chart: str
    target: str
    model_prob: float
    market_prob: float
    confidence: float
    net_edge: float
    side: str
    drivers: list
    note: str = ""

    def to_market_signal(self):
        """Adapter to intelligence.MarketSignal — reasoning is tagged so the
        signal aggregator treats it as the chart-intel source, not Claude."""
        from intelligence.signal import MarketSignal
        return MarketSignal(
            market_id=self.market_id, market_question=self.question,
            current_yes_price=self.market_prob, ai_probability=self.model_prob,
            confidence=self.confidence,
            direction="bullish" if self.side == "YES" else "bearish",
            reasoning=f"[chart-intel] {self.note}", news_headlines=[],
        )


@dataclass
class RunResult:
    chart: str
    snapshot_count: int
    market_count: int
    signals: list = field(default_factory=list)


def _match_target(question: str, records: list[ChartRecord]) -> Optional[ChartRecord]:
    """Best-effort: the record whose artist (or title) is named in the question."""
    q = (question or "").lower()
    for r in records:
        if r.artist and r.artist.lower() in q:
            return r
    for r in records:
        if r.title and r.title.lower() in q:
            return r
    return None


class MusicIntelEngine:
    """Dependency-injected coordinator (no live network in tests)."""

    def __init__(
        self,
        sources: list,                 # ordered ChartDataSource list (any trust tier)
        discover_fn,                   # async () -> list[MarketCandidate]
        *,
        store: Any = None,
        alert_sink: Any = None,
        cfg: MusicIntelConfig = None,
    ) -> None:
        self._sources = sources
        self._discover = discover_fn
        self._store = store
        self._sink = alert_sink
        self._cfg = cfg or MusicIntelConfig()

    # --- NO-EXECUTION GUARD (never trades; flag is never flipped here) ---------
    @staticmethod
    def execution_enabled() -> bool:
        # Policy: this module is alert-only. Even if the env flag is "true", we
        # do NOT trade from here — the seam stays a no-op by construction.
        os.environ.get("ENABLE_CHART_EXECUTION")  # read only; never acted upon
        return False

    async def _ingest(self, chart: str, as_of: Optional[date]) -> list[ChartRecord]:
        """Trust hierarchy: prefer the highest-trust source that returns data."""
        best: list[ChartRecord] = []
        best_tier = -1
        for src in self._sources:
            try:
                recs = await src.fetch(chart, as_of=as_of)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[music-intel] source %s failed: %s", getattr(src, "name", "?"), exc)
                continue
            if recs and src.trust_tier > best_tier:
                best, best_tier = recs, src.trust_tier
        return best

    async def run_once(self, chart: str, *, as_of: Optional[date] = None) -> RunResult:
        as_of = as_of or date.today()
        records = await self._ingest(chart, as_of)
        if self._store and records:
            for r in records:
                try:
                    self._store.record_snapshot(r)
                except Exception:  # noqa: BLE001
                    pass

        try:
            markets = await self._discover()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[music-intel] market discovery failed: %s", exc)
            markets = []

        if not markets:
            # "No market this week" is FIRST-CLASS: info, not error; projection
            # still ran so we accumulate a backtestable record.
            logger.info("[music-intel] no open music market for %s — projection recorded only", chart)

        signals: list[ChartSignal] = []
        for m in markets:
            # Prefer the SPECIFIC track the market names ("Title - Artist"); these
            # markets resolve on one track, not the artist's best-charting song.
            t_artist, t_title = parse_market_target(m.question)
            if not (t_artist or t_title):
                # Artist-level question (no quoted track) -> legacy artist match.
                tgt = _match_target(m.question, records)
                if tgt is None and records:
                    tgt = records[0]  # fall back to the field leader
                t_artist = tgt.artist if tgt else ""
                t_title = tgt.title if tgt else ""
            proj = project_number_one(
                records, t_artist, t_title,
                stream_eu=self._cfg.stream_eu, margin_k=self._cfg.margin_k, as_of=as_of,
            )
            market_prob = m.prices[0] if m.prices else 0.5
            days = self._days_to(m.close_time, as_of)
            edge = compute_edge(
                proj.prob, proj.confidence, market_prob,
                liquidity=m.liquidity, days_to_resolution=days, cfg=self._cfg.edge,
            )
            if self._store:
                try:
                    self._store.record_projection(
                        market_key=m.market_id, chart=chart, as_of=as_of,
                        point_estimate=proj.point_estimate_units,
                        prob_low=proj.prob_low, prob_high=proj.prob_high,
                        confidence=proj.confidence,
                        drivers_json=json.dumps(proj.drivers), model_prob=proj.prob,
                    )
                except Exception:  # noqa: BLE001
                    pass
            if edge.passes:
                sig = ChartSignal(
                    source="chart-intel", market_id=m.market_id, question=m.question,
                    chart=chart, target=proj.target, model_prob=edge.model_prob,
                    market_prob=edge.market_prob, confidence=edge.confidence,
                    net_edge=edge.net_edge, side=edge.side, drivers=proj.drivers,
                    note=f"{edge.side} edge {edge.net_edge:.3f} (model {edge.model_prob:.2f} "
                         f"vs mkt {edge.market_prob:.2f}, conf {edge.confidence:.2f})",
                )
                signals.append(sig)
                await self._alert(sig)

        return RunResult(chart=chart, snapshot_count=len(records),
                         market_count=len(markets), signals=signals)

    @staticmethod
    def _days_to(close_time, as_of: date) -> float:
        if close_time is None:
            return 7.0  # unknown -> assume a typical tracking week
        try:
            ref = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
            ct = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
            return max(0.0, (ct - ref).total_seconds() / 86400.0)
        except Exception:  # noqa: BLE001
            return 7.0

    async def _alert(self, sig: ChartSignal) -> None:
        if not (self._cfg.alerts_enabled and self._sink):
            return
        try:
            await self._sink.emit(
                title=f"Chart edge: {sig.side} {sig.question[:80]}",
                body=f"{sig.note}\nmarket={sig.market_id} chart={sig.chart}\n"
                     f"target={sig.target}\nNOTE: alert only — manual execution.",
                dedup_key=f"chart:{sig.market_id}:{sig.side}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[music-intel] alert failed: %s", exc)
