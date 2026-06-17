"""
Cross-Platform Execution Engine
===============================

Executes a detected Polymarket<->Kalshi arbitrage as an atomic two-legged trade.

Key idea — there is no naked shorting on either venue, so the engine's notional
"buy YES on A, sell YES on B" opportunity is executed as TWO BUYS of complementary
outcomes on the SAME underlying event:

    buy   `token`           on buy_platform  @ buy_price            (the ask)
    buy   opposite(`token`) on sell_platform @ (1 - sell_price)     (opposite ask)

Because the two markets resolve on the same event, exactly one leg pays $1, so the
combined cost is `buy_price + (1 - sell_price) = 1 - gross_edge` and the locked
profit equals `gross_edge` — identical to what `CrossPlatformArbEngine` scored,
but executable with no inventory and no shorting.

Atomicity across two independent venues is impossible, so this is best-effort:
both legs are placed concurrently; if exactly one leg lands we attempt to unwind
it (sell back the shares we now hold) and flag loudly if the unwind also fails.

Sizes are in SHARES / contracts (what both clients' place_order expect).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from polymarket_client.models import OrderSide, OrderStatus, TokenType

logger = logging.getLogger(__name__)


def _opposite(token: TokenType) -> TokenType:
    return TokenType.NO if token == TokenType.YES else TokenType.YES


@dataclass
class CrossExecConfig:
    """Configuration for the cross-platform executor."""
    dry_run: bool = True
    execution_enabled: bool = False     # hard gate on actually placing live orders
    max_trade_notional: float = 15.0    # max $ committed across BOTH legs per trade
    min_size: int = 1                   # minimum shares/contracts per leg
    cooldown_seconds: float = 30.0      # per-pair re-execution cooldown
    unwind_slippage: float = 0.05       # price concession when unwinding a stuck leg


@dataclass
class CrossExecStats:
    executed: int = 0          # both legs placed
    skipped: int = 0           # cooldown / risk / disabled
    partial_unwound: int = 0   # one leg placed, other failed, unwound ok
    failed: int = 0            # one leg placed, unwind failed -> live exposure
    errors: int = 0


@dataclass
class CrossExecResult:
    status: str                # "executed" | "skipped" | "unwound" | "exposed" | "error"
    detail: str = ""
    size: int = 0
    notional: float = 0.0
    legs: list = field(default_factory=list)


class CrossPlatformExecutor:
    """Executes CrossPlatformOpportunity objects across Polymarket + Kalshi."""

    def __init__(
        self,
        poly_client,
        kalshi_client,
        risk_manager,
        portfolio,
        config: CrossExecConfig,
    ):
        self.poly = poly_client
        self.kalshi = kalshi_client
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.config = config
        self.stats = CrossExecStats()
        self._cooldown: dict[str, datetime] = {}

    def _on_cooldown(self, pair_id: str) -> bool:
        until = self._cooldown.get(pair_id)
        return until is not None and datetime.utcnow() < until

    def _arm_cooldown(self, pair_id: str) -> None:
        self._cooldown[pair_id] = datetime.utcnow() + timedelta(seconds=self.config.cooldown_seconds)

    async def _place_leg(self, platform: str, ident: str, token: TokenType,
                         side: OrderSide, price: float, size: float, tag: str):
        """Place one leg on the named platform. Returns the unified Order."""
        if platform == "polymarket":
            return await self.poly.place_order(
                market_id=ident, token_type=token, side=side,
                price=price, size=size, strategy_tag=tag,
            )
        return await self.kalshi.place_order(
            ticker=ident, token_type=token, side=side,
            price=price, size=size, strategy_tag=tag,
        )

    def _ident(self, platform: str, opp) -> str:
        """Platform-specific market identifier for an opportunity's pair."""
        if platform == "polymarket":
            return opp.market_pair.polymarket_id
        return opp.market_pair.kalshi_ticker

    async def execute(self, opp) -> CrossExecResult:
        """Execute a CrossPlatformOpportunity as an atomic two-leg buy/buy."""
        pair_id = opp.market_pair.pair_id

        if self._on_cooldown(pair_id):
            self.stats.skipped += 1
            return CrossExecResult("skipped", "cooldown")

        # --- translate to two BUY legs of complementary outcomes ---
        token = TokenType.YES if opp.token.upper() == "YES" else TokenType.NO
        buy_token = token
        hedge_token = _opposite(token)
        buy_price = float(opp.buy_price)
        hedge_price = max(0.0, 1.0 - float(opp.sell_price))  # opposite-outcome ask

        # --- size (shares/contracts), clamped to the per-trade notional cap ---
        size = int(max(self.config.min_size, opp.suggested_size or 0))
        per_share_cost = buy_price + hedge_price
        if per_share_cost <= 0:
            self.stats.skipped += 1
            return CrossExecResult("skipped", "non-positive cost")
        max_by_notional = int(self.config.max_trade_notional // per_share_cost)
        if max_by_notional < self.config.min_size:
            self.stats.skipped += 1
            return CrossExecResult("skipped", f"min trade exceeds notional cap (${self.config.max_trade_notional})")
        size = min(size, max_by_notional)
        notional = size * per_share_cost

        # --- risk gate ---
        if not self.risk_manager.within_global_limits():
            self.stats.skipped += 1
            return CrossExecResult("skipped", "global risk limit", size=size, notional=notional)

        self._arm_cooldown(pair_id)

        buy_platform = opp.buy_platform
        hedge_platform = opp.sell_platform
        buy_ident = self._ident(buy_platform, opp)
        hedge_ident = self._ident(hedge_platform, opp)

        plan = (
            f"BUY {size} {buy_token.value} on {buy_platform}@{buy_price:.3f} + "
            f"BUY {size} {hedge_token.value} on {hedge_platform}@{hedge_price:.3f} "
            f"(notional=${notional:.2f}, net_edge={opp.net_edge:.4f})"
        )

        # --- dry-run: place against the simulated clients, no money moves ---
        if self.config.dry_run or not self.config.execution_enabled:
            mode = "DRY-RUN" if self.config.dry_run else "DETECT-ONLY (execution disabled)"
            logger.info(f"[{mode}] cross-platform arb: {plan}")
            if self.config.dry_run:
                # exercise the simulated order paths so dashboards/portfolio see it
                try:
                    await asyncio.gather(
                        self._place_leg(buy_platform, buy_ident, buy_token, OrderSide.BUY, buy_price, size, "xplat_arb"),
                        self._place_leg(hedge_platform, hedge_ident, hedge_token, OrderSide.BUY, hedge_price, size, "xplat_arb"),
                    )
                except Exception as e:
                    logger.debug(f"dry-run leg sim error (ignored): {e}")
            self.stats.executed += 1
            return CrossExecResult("executed", f"{mode}: {plan}", size=size, notional=notional)

        # --- live: place both legs concurrently ---
        logger.info(f"[LIVE] placing cross-platform arb: {plan}")
        results = await asyncio.gather(
            self._place_leg(buy_platform, buy_ident, buy_token, OrderSide.BUY, buy_price, size, "xplat_arb"),
            self._place_leg(hedge_platform, hedge_ident, hedge_token, OrderSide.BUY, hedge_price, size, "xplat_arb"),
            return_exceptions=True,
        )
        buy_res, hedge_res = results

        buy_ok = self._leg_ok(buy_res)
        hedge_ok = self._leg_ok(hedge_res)

        if buy_ok and hedge_ok:
            self.stats.executed += 1
            logger.info(f"[LIVE] cross-platform arb FILLED both legs: {plan}")
            return CrossExecResult("executed", plan, size=size, notional=notional,
                                   legs=[buy_res, hedge_res])

        if not buy_ok and not hedge_ok:
            self.stats.errors += 1
            logger.error(f"[LIVE] cross-platform arb FAILED both legs: buy={buy_res} hedge={hedge_res}")
            return CrossExecResult("error", "both legs failed", size=size, notional=notional)

        # exactly one leg landed -> we have one-sided exposure. Unwind it.
        if buy_ok:
            stuck_platform, stuck_ident, stuck_token, stuck_price = buy_platform, buy_ident, buy_token, buy_price
            other_err = hedge_res
        else:
            stuck_platform, stuck_ident, stuck_token, stuck_price = hedge_platform, hedge_ident, hedge_token, hedge_price
            other_err = buy_res

        logger.warning(
            f"[LIVE] cross-platform arb PARTIAL: filled {stuck_token.value} on "
            f"{stuck_platform}, other leg failed ({other_err}). Unwinding."
        )
        unwound = await self._unwind(stuck_platform, stuck_ident, stuck_token, stuck_price, size)
        if unwound:
            self.stats.partial_unwound += 1
            return CrossExecResult("unwound", f"one leg filled then unwound; other failed: {other_err}",
                                   size=size, notional=notional)
        self.stats.failed += 1
        logger.error(
            f"[NEEDS-ATTENTION] one-sided {stuck_token.value} position on {stuck_platform} "
            f"({stuck_ident}) x{size} could NOT be unwound. Manual intervention required."
        )
        return CrossExecResult("exposed", f"UNWIND FAILED — open {stuck_token.value} on {stuck_platform}",
                               size=size, notional=notional)

    @staticmethod
    def _leg_ok(res) -> bool:
        return (not isinstance(res, Exception)
                and res is not None
                and getattr(res, "status", None) != OrderStatus.REJECTED)

    async def _unwind(self, platform: str, ident: str, token: TokenType,
                      entry_price: float, size: int) -> bool:
        """Sell back a leg we are now holding (best effort)."""
        sell_price = max(0.01, round(entry_price * (1 - self.config.unwind_slippage), 2))
        try:
            res = await self._place_leg(platform, ident, token, OrderSide.SELL, sell_price, size, "xplat_unwind")
            return self._leg_ok(res)
        except Exception as e:
            logger.error(f"Unwind sell failed on {platform} {ident}: {e}")
            return False
