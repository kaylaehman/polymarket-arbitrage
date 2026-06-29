"""Maker Longshot strategy — NO-bias resting limit on longshot Kalshi markets.

EDGE: on longshot markets (YES mid <= max_yes_price, so NO >= ~0.85), the
structural longshot/NO bias (Jon-Becker/pma research) makes NO underpriced.
Acting as MAKER (resting NO BUY limit at post_price, 0% fee) captures the
spread + the bias. Hold to resolution.

Structural score:
    structural_score(1 - yes_mid, "NO", category) — passes the NO-side price
    (1 - yes_mid) so the function reads it as a heavy-NO / longshot-YES market
    and returns the correct positive bias. Passing yes_mid directly would
    mis-read the longshot as a favorite and invert the score.

Resting maker price:
    post_price = round(no_ask - price_improvement_cents/100.0, 2)
    clamped to [0.01, 0.99]; must be strictly below no_ask to be non-marketable.

Weather forecast gate (additive — only affects weather candidates):
    Kalshi KXHIGH* T-type (above-threshold): KEEP NO when forecast <= threshold - safe_margin_f.
    Kalshi KXHIGH* B-type (bucket [lo,hi]):  KEEP NO when forecast <= lo - safe_margin_f OR
                              forecast >= hi + safe_margin_f (forecast far outside
                              the 1° bucket on either side).
    PM.US tc-temp-* (gte{N}f sentinel hi=999): KEEP when forecast <= lo - safe_margin_f.
    PM.US tc-temp-* (bucket): delegates to bucket_gate_keep.
    Non-weather candidates pass through UNCHANGED.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from core.market_data import FinancialMarket, crossing_margin, parse_financial_ticker
from core.macro_data import parse_macro_ticker, macro_threshold_keep, macro_bucket_keep
from core.sports_data import kalshi_series_to_odds, match_team, sports_gate_keep
from core.weather import (
    PMUSWeatherBucket,
    WeatherBucket,
    WeatherMarket,
    bucket_gate_keep,
    forecast_high,
    forecast_margin,
    parse_bucket_ticker,
    parse_pmus_slug,
    parse_weather_ticker,
    pmus_bucket_gate_keep,
)
from kalshi_client.models import KalshiMarket
from utils.structural_bias import structural_score

logger = logging.getLogger(__name__)


class MakerLongshotStrategy(Strategy):
    """Post resting NO BUY limits on structurally-favoured longshot markets.

    Args:
        min_structural_score: Minimum structural_score(1 - yes_mid, "NO", category).
        min_yes_price: Skip markets where yes_mid < this (fat-tail floor;
            default 0.05 = NO > 0.95 rejected).
        max_yes_price: Skip markets where yes_mid > this (longshot filter).
        price_improvement_cents: How many cents below no_ask to post the bid.
        skip_categories: Category strings to skip entirely.
        max_days_to_resolution: Skip markets whose close_time is more than this many
            days from now (or in the past). Markets with no close_time are skipped
            to be safe.
        weather_cfg: Optional WeatherCfg; if None the weather gate is disabled.
    """

    def __init__(
        self,
        min_structural_score: float,
        max_yes_price: float,
        price_improvement_cents: int,
        skip_categories: list[str],
        min_yes_price: float = 0.05,
        max_days_to_resolution: float = 90.0,
        weather_cfg: Optional[Any] = None,
        financial_cfg: Optional[Any] = None,
        macro_cfg: Optional[Any] = None,
        sports_cfg: Optional[Any] = None,
    ) -> None:
        self._min_score = min_structural_score
        self._min_yes = min_yes_price
        self._max_yes = max_yes_price
        self._pip = price_improvement_cents
        self._skip = set(skip_categories)
        self._max_days = max_days_to_resolution
        self._weather = weather_cfg  # WeatherCfg | None
        self._financial = financial_cfg  # FinancialCfg | None
        self._macro = macro_cfg  # MacroCfg | None
        self._sports = sports_cfg  # SportsCfg | None

    @property
    def name(self) -> str:
        return "maker_longshot"

    async def _fetch_forecast_gated(
        self,
        ticker: str,
        series: str,
        target_date,
        delta_days: float,
        http: Any,
    ):
        """Fetch NWS forecast, handling horizon/unavailable per config.

        Returns (fc, skip_reason) where fc is the forecast float or None, and
        skip_reason is a non-empty string if the caller should SKIP (no forecast
        and require_forecast=True), else None.
        """
        cfg = self._weather
        if delta_days > cfg.forecast_horizon_days:
            if cfg.require_forecast:
                logger.debug(
                    "[weather-gate] %s beyond horizon (%d > %d days), skipping",
                    ticker, int(delta_days), cfg.forecast_horizon_days,
                )
                return None, "beyond_horizon"
            return None, None  # structural fallback

        fc = await forecast_high(series, target_date, http=http)
        if fc is None:
            if cfg.require_forecast:
                logger.debug("[weather-gate] %s: no forecast, skipping", ticker)
                return None, "no_forecast"
            logger.debug("[weather-gate] %s: no forecast, structural fallback (keep)", ticker)
        return fc, None

    async def _apply_weather_gate(
        self,
        market: KalshiMarket,
        wm: WeatherMarket,
        delta_days: float,
        http: Any,
    ) -> bool:
        """Return True to KEEP the T-type NO candidate; False to SKIP."""
        fc, skip_reason = await self._fetch_forecast_gated(
            market.ticker, wm.series, wm.date, delta_days, http
        )
        if skip_reason is not None:
            return False
        if fc is None:
            return True  # structural fallback (require_forecast=False, no forecast)

        margin = forecast_margin(fc, wm.threshold)
        keep = margin <= -self._weather.safe_margin_f
        logger.info(
            "[weather-gate] %s: fc=%.1f threshold=%.1f margin=%.1f safe=%.1f -> %s",
            market.ticker, fc, wm.threshold, margin, self._weather.safe_margin_f,
            "KEEP" if keep else "SKIP",
        )
        return keep

    async def _apply_bucket_gate(
        self,
        market: KalshiMarket,
        wb: WeatherBucket,
        delta_days: float,
        http: Any,
    ) -> bool:
        """Return True to KEEP the B-type bucket NO candidate; False to SKIP."""
        fc, skip_reason = await self._fetch_forecast_gated(
            market.ticker, wb.series, wb.date, delta_days, http
        )
        if skip_reason is not None:
            return False
        if fc is None:
            return True  # structural fallback (require_forecast=False, no forecast)

        keep = bucket_gate_keep(fc, wb.lo, wb.hi, self._weather.safe_margin_f)
        logger.info(
            "[weather-gate] %s: fc=%.1f bucket=[%d,%d] safe=%.1f -> %s",
            market.ticker, fc, wb.lo, wb.hi, self._weather.safe_margin_f,
            "KEEP" if keep else "SKIP",
        )
        return keep

    async def _apply_pmus_bucket_gate(
        self,
        market: Any,
        wb: PMUSWeatherBucket,
        delta_days: float,
        http: Any,
    ) -> bool:
        """Return True to KEEP the PM.US weather bucket NO candidate; False to SKIP."""
        fc, skip_reason = await self._fetch_forecast_gated(
            market.ticker, wb.series, wb.date, delta_days, http
        )
        if skip_reason is not None:
            return False
        if fc is None:
            return True  # structural fallback

        keep = pmus_bucket_gate_keep(fc, wb, self._weather.safe_margin_f)
        logger.info(
            "[weather-gate] %s: fc=%.1f pmus_bucket=[%d,%d] safe=%.1f -> %s",
            market.ticker, fc, wb.lo, wb.hi, self._weather.safe_margin_f,
            "KEEP" if keep else "SKIP",
        )
        return keep

    async def _apply_financial_gate(
        self,
        market,
        fm: "FinancialMarket",
        delta_days: float,
        ctx: dict,
    ) -> bool:
        """Return True to KEEP the T-type financial NO candidate; False to SKIP."""
        cfg = self._financial
        av = ctx.get("av")

        if delta_days > cfg.horizon_days:
            if cfg.require_data:
                logger.debug("[financial-gate] %s beyond horizon (%d > %d days), skipping",
                            market.ticker, int(delta_days), cfg.horizon_days)
                return False
            return True

        if av is None:
            keep = not cfg.require_data
            logger.debug("[financial-gate] %s: no av client, require_data=%s -> %s",
                        market.ticker, cfg.require_data, "KEEP" if keep else "SKIP")
            return keep

        price = await av.get_price(fm.underlying)
        if price is None:
            keep = not cfg.require_data
            logger.debug("[financial-gate] %s: price unavailable, require_data=%s -> %s",
                        market.ticker, cfg.require_data, "KEEP" if keep else "SKIP")
            return keep

        vol = await av.daily_vol(fm.underlying)
        z = crossing_margin(price, vol, fm.threshold, delta_days)
        keep = z >= cfg.min_sigma
        logger.info(
            "[financial-gate] %s: price=%.4f vol=%.4f threshold=%.4f days=%.1f z=%.2f min_sigma=%.1f -> %s",
            market.ticker, price, vol, fm.threshold, delta_days, z, cfg.min_sigma,
            "KEEP" if keep else "SKIP",
        )
        return keep

    async def _apply_macro_gate(self, market, mm, delta_days: float, ctx: dict) -> bool:
        """Return True to KEEP a macro NO candidate; False to SKIP. Mirrors the
        financial gate but uses a Fed nowcast + per-indicator σ."""
        cfg = self._macro
        client = ctx.get("macro")
        if delta_days > cfg.horizon_days:
            return not cfg.require_data
        if client is None:
            return not cfg.require_data
        nowcast = await client.nowcast(mm.indicator)
        if nowcast is None:
            keep = not cfg.require_data
            logger.debug("[macro-gate] %s: no nowcast, require_data=%s -> %s",
                         market.ticker, cfg.require_data, "KEEP" if keep else "SKIP")
            return keep
        sigma = float(cfg.sigma.get(mm.indicator, 0.0))
        if mm.market_type == "bucket" and mm.bucket_hi is not None:
            keep = macro_bucket_keep(nowcast, mm.bucket_lo, mm.bucket_hi, sigma, cfg.min_sigma)
        elif mm.market_type == "bucket":
            # single-sided bucket (lo only): treat like a threshold floor
            keep = macro_threshold_keep(nowcast, sigma, mm.bucket_lo, cfg.min_sigma)
        else:
            keep = macro_threshold_keep(nowcast, sigma, mm.threshold, cfg.min_sigma)
        logger.info("[macro-gate] %s: nowcast=%.3f thr=%.3f sigma=%.3f min_sigma=%.1f -> %s",
                    market.ticker, nowcast, mm.threshold, sigma, cfg.min_sigma,
                    "KEEP" if keep else "SKIP")
        return keep

    async def _apply_sports_gate(self, market, ctx: dict) -> bool:
        """Return True to KEEP a sports-futures NO candidate; False to SKIP.
        Keeps NO only when the de-vigged bookmaker consensus also says the team is
        a longshot (<= max_prob). Ambiguous/missing match -> require_data decides."""
        cfg = self._sports
        client = ctx.get("sports")
        if client is None:
            return not cfg.require_data
        try:
            probs = await client.championship_probs(market.ticker)
        except Exception:  # noqa: BLE001
            probs = {}
        if not probs:
            return not cfg.require_data
        team = getattr(market, "yes_sub_title", "") or getattr(market, "subtitle", "")
        prob = match_team(team, probs)
        if prob is None:
            return not cfg.require_data
        keep = sports_gate_keep(prob, cfg.max_prob)
        logger.info("[sports-gate] %s team=%s consensus=%.3f max=%.3f -> %s",
                    market.ticker, team, prob, cfg.max_prob, "KEEP" if keep else "SKIP")
        return keep

    async def scan(
        self,
        markets: list[KalshiMarket],
        ctx: dict[str, Any],
    ) -> list[DirectionalCandidate]:
        """Scan for longshot NO maker opportunities.

        For each market:
        1. Skip excluded categories.
        2. Skip if yes_mid <= 0, < min_yes_price, or > max_yes_price
           (accepted band: min_yes_price <= yes_mid <= max_yes_price).
        3. Compute structural_score(1 - yes_mid, "NO", category); skip if < min.
        4. Fetch no_ask; skip if unavailable.
        5. [Weather gate] If ticker is a PM.US pmus: prefixed weather market,
           apply pmus_bucket_gate. If ticker parses as a KXHIGH* T-type
           above-threshold market OR a B-type bucket market, apply NWS gate.
           Non-weather candidates skip step 5 unchanged.
        6. Build resting post_price strictly below no_ask.
        7. Emit NO DirectionalCandidate with strategy="maker_longshot".
        """
        no_ask_fn = ctx["no_ask"]
        http = ctx.get("http")  # httpx.AsyncClient or compatible; None if not provided
        candidates: list[DirectionalCandidate] = []

        for market in markets:
            if market.category in self._skip:
                continue

            close = market.close_time
            if close is None:
                continue
            if close.tzinfo is None:
                close = close.replace(tzinfo=timezone.utc)
            delta_days = (close - datetime.now(timezone.utc)).total_seconds() / 86400.0
            if delta_days <= 0 or delta_days > self._max_days:
                continue

            yes_mid = market.yes_price
            if yes_mid <= 0 or yes_mid < self._min_yes or yes_mid > self._max_yes:
                continue

            score = structural_score(1 - yes_mid, "NO", market.category)
            if score < self._min_score:
                continue

            no_ask = no_ask_fn(market.ticker)
            if no_ask is None:
                continue

            # Weather forecast gate — KXHIGH* T-type (above-threshold), B-type, and PM.US pmus:*
            if self._weather is not None and self._weather.enabled:
                if http is not None:
                    ticker = market.ticker
                    # PM.US pmus: prefix — parse as PMUSWeatherBucket
                    if ticker.startswith("pmus:"):
                        slug = ticker[len("pmus:"):]
                        wb_pmus = parse_pmus_slug(slug)
                        if wb_pmus is not None:
                            if not await self._apply_pmus_bucket_gate(
                                market, wb_pmus, delta_days, http
                            ):
                                continue
                    else:
                        # Kalshi KXHIGH* tickers
                        wm = parse_weather_ticker(ticker)
                        if wm is not None and wm.is_above_threshold:
                            if not await self._apply_weather_gate(market, wm, delta_days, http):
                                continue
                        else:
                            wb = parse_bucket_ticker(ticker)
                            if wb is not None:
                                if not await self._apply_bucket_gate(market, wb, delta_days, http):
                                    continue
                else:
                    # No HTTP client: treat as forecast unavailable for any weather ticker
                    ticker = market.ticker
                    if ticker.startswith("pmus:"):
                        wb_pmus = parse_pmus_slug(ticker[len("pmus:"):])
                        is_weather = wb_pmus is not None
                    else:
                        wm = parse_weather_ticker(ticker)
                        wb = parse_bucket_ticker(ticker) if wm is None else None
                        is_weather = (wm is not None and wm.is_above_threshold) or wb is not None
                    if is_weather:
                        keep = not self._weather.require_forecast
                        logger.debug(
                            "[weather-gate] %s: no http client, require_forecast=%s -> %s",
                            market.ticker, self._weather.require_forecast,
                            "KEEP" if keep else "SKIP",
                        )
                        if not keep:
                            continue

            # Financial market gate — KXBTC*/KXETH*/KXWTI/KXEURUSD T-type above-threshold
            if self._financial is not None and self._financial.enabled:
                fm = parse_financial_ticker(market.ticker)
                if fm is not None and fm.market_type == "threshold" and fm.direction == "above":
                    if not await self._apply_financial_gate(market, fm, delta_days, ctx):
                        continue

            # Macro nowcast gate — KXCPI*/KXCPIYOY*/KXCPICORE*/KXPCECORE*/KXGDP*
            if self._macro is not None and self._macro.enabled:
                mm = parse_macro_ticker(market.ticker)
                if mm is not None:
                    if not await self._apply_macro_gate(market, mm, delta_days, ctx):
                        continue

            # Sports consensus gate — KXNBA/KXMLB/KXNHL/KXNFL championship futures
            if self._sports is not None and self._sports.enabled:
                if kalshi_series_to_odds(market.ticker) is not None:
                    if not await self._apply_sports_gate(market, ctx):
                        continue

            # Build non-marketable resting bid: strictly < no_ask
            improvement = self._pip / 100.0
            post_price = round(no_ask - improvement, 2)
            post_price = max(0.01, min(0.99, post_price))
            if post_price >= no_ask:
                post_price = round(no_ask - 0.01, 2)
            if post_price <= 0:
                continue

            candidates.append(
                DirectionalCandidate(
                    market_id=market.to_unified_market_id(),
                    title=market.title,
                    category=market.category,
                    side="NO",
                    market_price=post_price,
                    ai_probability=None,
                    confidence=None,
                    edge=score,
                    strategy=self.name,
                    reasoning=(
                        f"yes_mid={yes_mid:.3f} score={score:.4f} "
                        f"no_ask={no_ask:.3f} post={post_price:.3f}"
                    ),
                )
            )

        return candidates
