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
    fill_timeout_seconds: float = 12.0  # how long to wait for a leg to fill
    fill_poll_seconds: float = 1.0      # interval between fill-status polls


@dataclass
class CrossExecStats:
    executed: int = 0          # both legs filled in matched quantity
    skipped: int = 0           # cooldown / risk / disabled
    reconciled: int = 0        # filled with a residual that was unwound ok
    no_fill: int = 0           # nothing filled (orders cancelled, no exposure)
    failed: int = 0            # residual could not be unwound -> live exposure
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
        buy_res, hedge_res = await asyncio.gather(
            self._place_leg(buy_platform, buy_ident, buy_token, OrderSide.BUY, buy_price, size, "xplat_arb"),
            self._place_leg(hedge_platform, hedge_ident, hedge_token, OrderSide.BUY, hedge_price, size, "xplat_arb"),
            return_exceptions=True,
        )
        buy_placed = self._leg_ok(buy_res)
        hedge_placed = self._leg_ok(hedge_res)

        if not buy_placed and not hedge_placed:
            self.stats.errors += 1
            logger.error(f"[LIVE] cross-platform arb FAILED to place both legs: buy={buy_res} hedge={hedge_res}")
            return CrossExecResult("error", "both legs failed to place", size=size, notional=notional)

        # --- reconcile on actual FILLS, not placement ---
        # Wait for each placed leg's terminal fill state; cancel leftover resting size.
        buy_filled = await self._await_fill(buy_platform, buy_res) if buy_placed else 0.0
        hedge_filled = await self._await_fill(hedge_platform, hedge_res) if hedge_placed else 0.0

        matched = min(buy_filled, hedge_filled)
        residual_buy = buy_filled - matched      # excess buy_token holdings to shed
        residual_hedge = hedge_filled - matched  # excess hedge_token holdings to shed

        logger.info(
            f"[LIVE] fills: buy={buy_filled} hedge={hedge_filled} -> matched={matched}, "
            f"residual buy={residual_buy} hedge={residual_hedge}"
        )

        if matched <= 0 and residual_buy <= 0 and residual_hedge <= 0:
            self.stats.no_fill += 1
            return CrossExecResult("no_fill", "nothing filled; orders cancelled (no exposure)",
                                   size=size, notional=notional)

        # Unwind any one-sided residual (we hold these shares, so SELL is valid).
        exposed = []
        if residual_buy > 0:
            if not await self._unwind(buy_platform, buy_ident, buy_token, buy_price, residual_buy):
                exposed.append(f"{int(residual_buy)} {buy_token.value} on {buy_platform}")
        if residual_hedge > 0:
            if not await self._unwind(hedge_platform, hedge_ident, hedge_token, hedge_price, residual_hedge):
                exposed.append(f"{int(residual_hedge)} {hedge_token.value} on {hedge_platform}")

        if exposed:
            self.stats.failed += 1
            logger.error(f"[NEEDS-ATTENTION] cross-platform residual could NOT be unwound: {'; '.join(exposed)}")
            return CrossExecResult("exposed", f"UNWIND FAILED — open: {'; '.join(exposed)}",
                                   size=int(matched), notional=matched * per_share_cost)

        if residual_buy > 0 or residual_hedge > 0:
            self.stats.reconciled += 1
            logger.info(f"[LIVE] cross-platform arb reconciled: matched {int(matched)}, residual unwound")
            return CrossExecResult("reconciled", f"matched {int(matched)}; residual unwound",
                                   size=int(matched), notional=matched * per_share_cost)

        self.stats.executed += 1
        logger.info(f"[LIVE] cross-platform arb FILLED both legs matched x{int(matched)}")
        return CrossExecResult("executed", f"matched {int(matched)}", size=int(matched),
                               notional=matched * per_share_cost, legs=[buy_res, hedge_res])

    def _kalshi_leg(self, opp):
        """The Kalshi side of a cross-platform opp, as a BUY (token, price)."""
        token = TokenType.YES if opp.token.upper() == "YES" else TokenType.NO
        if opp.buy_platform == "kalshi":
            return token, float(opp.buy_price)
        # sell_platform == kalshi -> "sell token" == buy opposite at 1 - sell_price
        return _opposite(token), max(0.0, 1.0 - float(opp.sell_price))

    async def execute_kalshi_leg_only(self, opp) -> CrossExecResult:
        """
        DIRECTIONAL: take only the Kalshi leg of a cross-platform gap, using
        Polymarket as a price oracle. This is NOT riskless arbitrage — it leaves
        an open position with real event risk; the edge is Kalshi being mispriced
        vs Polymarket's (typically more efficient) price.
        """
        pair_id = opp.market_pair.pair_id
        if self._on_cooldown(pair_id):
            self.stats.skipped += 1
            return CrossExecResult("skipped", "cooldown")

        token, price = self._kalshi_leg(opp)
        if price <= 0:
            self.stats.skipped += 1
            return CrossExecResult("skipped", "non-positive price")

        size = int(max(self.config.min_size, opp.suggested_size or 0))
        max_by_notional = int(self.config.max_trade_notional // price)
        if max_by_notional < self.config.min_size:
            self.stats.skipped += 1
            return CrossExecResult("skipped", "min trade exceeds notional cap")
        size = min(size, max_by_notional)
        notional = size * price

        if not self.risk_manager.within_global_limits():
            self.stats.skipped += 1
            return CrossExecResult("skipped", "global risk limit", size=size, notional=notional)

        self._arm_cooldown(pair_id)
        ticker = opp.market_pair.kalshi_ticker
        plan = f"DIRECTIONAL BUY {size} {token.value} on kalshi@{price:.3f} (oracle: poly={opp.buy_price if opp.buy_platform=='polymarket' else opp.sell_price})"

        if self.config.dry_run:
            logger.info(f"[DRY-RUN] kalshi oracle trade: {plan}")
            try:
                await self.kalshi.place_order(ticker=ticker, token_type=token, side=OrderSide.BUY,
                                              price=price, size=size, strategy_tag="kalshi_oracle")
            except Exception as e:
                logger.debug(f"dry-run kalshi oracle sim error (ignored): {e}")
            self.stats.executed += 1
            return CrossExecResult("executed", f"DRY-RUN: {plan}", size=size, notional=notional)

        logger.info(f"[LIVE] kalshi oracle trade: {plan}")
        try:
            res = await self.kalshi.place_order(ticker=ticker, token_type=token, side=OrderSide.BUY,
                                                price=price, size=size, strategy_tag="kalshi_oracle")
            if self._leg_ok(res):
                self.stats.executed += 1
                return CrossExecResult("executed", plan, size=size, notional=notional, legs=[res])
            self.stats.errors += 1
            return CrossExecResult("error", f"kalshi order rejected: {res}", size=size, notional=notional)
        except Exception as e:
            self.stats.errors += 1
            logger.error(f"kalshi oracle trade failed: {e}")
            return CrossExecResult("error", str(e), size=size, notional=notional)

    @staticmethod
    def _leg_ok(res) -> bool:
        return (not isinstance(res, Exception)
                and res is not None
                and getattr(res, "status", None) != OrderStatus.REJECTED)

    async def _await_fill(self, platform: str, order) -> float:
        """
        Poll a placed leg until it reaches a terminal state (filled/cancelled) or
        the timeout elapses; on timeout, cancel any resting remainder to stop
        further fills. Returns the filled share count.
        """
        client = self.poly if platform == "polymarket" else self.kalshi
        oid = getattr(order, "order_id", None)
        if not oid:
            return 0.0

        filled = float(getattr(order, "filled_size", 0.0) or 0.0)
        polls = max(1, int(self.config.fill_timeout_seconds / max(0.1, self.config.fill_poll_seconds)))
        for _ in range(polls):
            try:
                st = await client.get_order(oid)
                filled = st.get("filled_size", filled)
                if st.get("status") in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                    return filled
            except Exception as e:
                logger.debug(f"fill poll error on {platform} {oid}: {e}")
            await asyncio.sleep(self.config.fill_poll_seconds)

        # Timed out still resting/partial -> cancel remainder, then read final fill.
        try:
            await client.cancel_order(oid)
        except Exception as e:
            logger.debug(f"cancel-on-timeout failed for {platform} {oid}: {e}")
        try:
            st = await client.get_order(oid)
            filled = st.get("filled_size", filled)
        except Exception:
            pass
        return filled

    async def _unwind(self, platform: str, ident: str, token: TokenType,
                      entry_price: float, size: float) -> bool:
        """Sell back shares we are now holding (best effort)."""
        qty = int(round(size))
        if qty <= 0:
            return True
        sell_price = max(0.01, round(entry_price * (1 - self.config.unwind_slippage), 2))
        try:
            res = await self._place_leg(platform, ident, token, OrderSide.SELL, sell_price, qty, "xplat_unwind")
            return self._leg_ok(res)
        except Exception as e:
            logger.error(f"Unwind sell failed on {platform} {ident}: {e}")
            return False
