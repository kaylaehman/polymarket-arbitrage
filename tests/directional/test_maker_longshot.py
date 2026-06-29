"""Tests for the Maker Longshot (NO-bias resting limit) strategy.

Covers:
- Strategy: scan emits NO maker candidate on a qualifying longshot
- Strategy: post_price is strictly below no_ask (non-marketable)
- Strategy: skips when yes_mid > max_yes_price
- Strategy: skips when structural_score < min_structural_score
- Strategy: skips when no_ask is None
- Strategy: skips categories in skip_categories
- Strategy: post_price clamping to [0.01, 0.99]
- Executor: paper records at post_price immediately (no API call)
- Executor: live places resting NO BUY limit + records pending with order_id
- Executor: live aborts if balance < notional
- Tracker: pending → fill → status "open"
- Tracker: pending past TTL → cancel + status "closed"
- Config: MakerLongshotCfg defaults; DirectionalConfig includes maker_longshot
- Config: maker_longshot.mode validated (typo raises ConfigError)
- Config: maker_longshot block parsed from YAML
- Engine: builds MakerLongshotStrategy as the third strategy
- Engine: run_once in paper mode records a maker_longshot position
"""
from __future__ import annotations

import pytest
import yaml
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.directional.models import DirectionalCandidate, DirectionalOrder, DirectionalPosition
from core.directional.store import DirectionalStore
from core.directional.strategies.maker_longshot import MakerLongshotStrategy
from core.directional.executor import Executor
from core.directional.tracker import Tracker
from polymarket_client.models import OrderStatus


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_market(
    ticker="KX-TEST-ML-1",
    yes_price=0.08,
    no_price=0.92,
    category="Politics",
    title="Test market",
    close_time=None,
):
    m = SimpleNamespace()
    m.ticker = ticker
    m.event_ticker = ticker
    m.yes_price = yes_price
    m.no_price = no_price
    m.category = category
    m.title = title
    m.status = "open"
    m.result = None
    m.close_time = close_time
    m.to_unified_market_id = lambda: f"kalshi:{ticker}"
    return m


def default_strategy(**overrides):
    kwargs = dict(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,  # disabled in unit tests; tested separately
    )
    kwargs.update(overrides)
    return MakerLongshotStrategy(**kwargs)


def make_ctx(no_ask: float | None = 0.94):
    return {"no_ask": lambda ticker: no_ask}


# ── Strategy tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_emits_no_candidate_on_longshot():
    """YES=0.08 Sports market produces a NO maker candidate above the default score threshold.

    With the corrected structural_score(1 - yes_mid, "NO", category) call,
    score = structural_score(0.92, "NO", "Sports") ≈ 0.10, which clears the
    default min_structural_score=0.02 comfortably.
    """
    strategy = default_strategy(min_structural_score=0.02)
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(yes_price=0.08, category="Sports", close_time=near_term)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.94))

    assert len(candidates) == 1
    c = candidates[0]
    assert c.side == "NO"
    assert c.strategy == "maker_longshot"
    assert c.ai_probability is None
    assert c.confidence is None
    assert c.edge >= 0.02


@pytest.mark.asyncio
async def test_post_price_strictly_below_no_ask():
    """post_price must be strictly < no_ask so the order rests in the book."""
    strategy = default_strategy(price_improvement_cents=1)
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(yes_price=0.08, category="Sports", close_time=near_term)
    no_ask = 0.94
    candidates = await strategy.scan([market], make_ctx(no_ask=no_ask))

    assert len(candidates) == 1
    post_price = candidates[0].market_price
    assert post_price < no_ask, f"post_price {post_price} must be < no_ask {no_ask}"
    assert post_price == round(no_ask - 0.01, 2)


@pytest.mark.asyncio
async def test_skips_when_yes_mid_above_max():
    """yes_mid > max_yes_price → not a longshot, skip."""
    strategy = default_strategy(max_yes_price=0.15)
    market = make_market(yes_price=0.20)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.82))
    assert candidates == []


@pytest.mark.asyncio
async def test_skips_when_yes_mid_exactly_at_zero():
    """yes_mid <= 0 → skip."""
    strategy = default_strategy()
    market = make_market(yes_price=0.0)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.99))
    assert candidates == []


@pytest.mark.asyncio
async def test_skips_when_yes_mid_below_min():
    """yes_mid < min_yes_price (NO > 0.95) -> fat-tail extreme, skip."""
    strategy = MakerLongshotStrategy(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.05,
    )
    market = make_market(yes_price=0.02)  # NO@0.98 > 0.95 threshold
    candidates = await strategy.scan([market], make_ctx(no_ask=0.99))
    assert candidates == []


@pytest.mark.asyncio
async def test_emits_within_band():
    """yes_mid=0.08 with min=0.05 / max=0.15 -> within band, emits candidate."""
    strategy = MakerLongshotStrategy(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.05,
        max_days_to_resolution=9999.0,
    )
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(yes_price=0.08, category="Sports", close_time=near_term)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.94))
    assert len(candidates) == 1
    assert candidates[0].side == "NO"


@pytest.mark.asyncio
async def test_skips_when_score_below_min():
    """structural_score below threshold → skip."""
    # yes_mid=0.50 → score near 0 (near-zero longshot/NO bias at 50¢)
    strategy = default_strategy(min_structural_score=0.50)
    market = make_market(yes_price=0.50, category="Finance")
    candidates = await strategy.scan([market], make_ctx(no_ask=0.55))
    assert candidates == []


@pytest.mark.asyncio
async def test_skips_when_no_ask_is_none():
    """no_ask unavailable → skip market."""
    strategy = default_strategy()
    market = make_market(yes_price=0.08)
    candidates = await strategy.scan([market], make_ctx(no_ask=None))
    assert candidates == []


@pytest.mark.asyncio
async def test_skips_excluded_category():
    """Market in skip_categories → skip."""
    strategy = default_strategy(skip_categories=["Sports"])
    market = make_market(yes_price=0.05, category="Sports")
    candidates = await strategy.scan([market], make_ctx(no_ask=0.97))
    assert candidates == []


@pytest.mark.asyncio
async def test_post_price_clamped_to_minimum():
    """post_price clamped to >= 0.01 even when no_ask is very low."""
    strategy = default_strategy(price_improvement_cents=5)
    market = make_market(yes_price=0.05, category="Finance")
    # no_ask=0.03 → raw post = 0.03 - 0.05 = -0.02 → clamped to 0.01
    # But clamp puts it at 0.01 which is < no_ask=0.03, so valid
    candidates = await strategy.scan([market], make_ctx(no_ask=0.03))
    if candidates:
        assert candidates[0].market_price >= 0.01


@pytest.mark.asyncio
async def test_post_price_safety_below_no_ask_after_clamp():
    """After clamping, post_price must still be < no_ask OR strategy skips."""
    strategy = default_strategy(price_improvement_cents=1)
    market = make_market(yes_price=0.08)
    no_ask = 0.01  # very tight: post_price = 0.01 - 0.01 = 0.00 → clamped to 0.01 >= no_ask
    # The strategy should skip because post_price (after clamp 0.01) >= no_ask (0.01)
    # and post_price = round(0.01 - 0.01, 2) = 0.00 → clamped to 0.01 → 0.01 >= 0.01 → skip
    candidates = await strategy.scan([market], make_ctx(no_ask=no_ask))
    # Either skipped or post_price < no_ask
    for c in candidates:
        assert c.market_price < no_ask


@pytest.mark.asyncio
async def test_candidate_fields_populated():
    """DirectionalCandidate has correct market_id, category, reasoning."""
    strategy = default_strategy(min_structural_score=0.02)
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(ticker="KX-SAMPLE", yes_price=0.10, category="Sports", close_time=near_term)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.92))

    assert len(candidates) == 1
    c = candidates[0]
    assert c.market_id == "kalshi:KX-SAMPLE"
    assert c.category == "Sports"
    assert "yes_mid=" in c.reasoning
    assert "no_ask=" in c.reasoning


# ── Executor tests ────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.saved: list[DirectionalPosition] = []

    def record_position(self, p):
        self.saved.append(p)
        return 1


class FakeKalshiClient:
    def __init__(self, balance=100.0, order_id="korder_test123"):
        self._balance = balance
        self._order_id = order_id
        self.place_calls: list[dict] = []

    async def get_balance(self):
        return self._balance

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        o = SimpleNamespace()
        o.order_id = self._order_id
        return o


def maker_order(price=0.93, strategy="maker_longshot") -> DirectionalOrder:
    return DirectionalOrder(
        market_id="kalshi:KX-ML-1",
        side="NO",
        price=price,
        size=5,
        notional=price * 5,
        strategy=strategy,
    )


@pytest.mark.asyncio
async def test_executor_paper_records_no_api():
    """Paper maker: position recorded at post_price; no API call."""
    store, client = FakeStore(), FakeKalshiClient()
    pos = await Executor(client, store).place(maker_order(), mode="paper")

    assert pos is not None
    assert pos.status == "open"
    assert pos.strategy == "maker_longshot"
    assert abs(pos.entry_price - 0.93) < 1e-9
    assert client.place_calls == []


@pytest.mark.asyncio
async def test_executor_live_places_resting_limit_and_records_pending():
    """Live maker: place_order called + position recorded as pending with order_id."""
    store = FakeStore()
    client = FakeKalshiClient(balance=100.0, order_id="korder_abc")
    pos = await Executor(client, store).place(maker_order(price=0.93), mode="live")

    assert pos is not None
    assert pos.status == "pending"
    assert pos.order_id == "korder_abc"
    assert len(client.place_calls) == 1

    call = client.place_calls[0]
    from polymarket_client.models import TokenType, OrderSide
    assert call["token_type"] == TokenType.NO
    assert call["side"] == OrderSide.BUY
    assert abs(call["price"] - 0.93) < 1e-9


@pytest.mark.asyncio
async def test_executor_live_aborts_on_insufficient_balance():
    """Live maker: aborts if balance < notional."""
    store = FakeStore()
    client = FakeKalshiClient(balance=1.0)  # 1.0 < 4.65 notional
    pos = await Executor(client, store).place(maker_order(price=0.93), mode="live")

    assert pos is None
    assert client.place_calls == []
    assert store.saved == []


@pytest.mark.asyncio
async def test_executor_non_maker_strategy_unaffected():
    """Non-maker strategy follows the original code path."""
    store = FakeStore()
    client = FakeKalshiClient(balance=100.0)
    order = DirectionalOrder(
        market_id="kalshi:KX-SC-1",
        side="NO",
        price=0.90,
        size=5,
        notional=4.5,
        strategy="safe_compounder",
    )
    pos = await Executor(client, store).place(order, mode="paper")
    assert pos.status == "open"
    assert pos.order_id is None


# ── Tracker tests ─────────────────────────────────────────────────────────────

def _make_pending_position(
    market_id="kalshi:KX-ML-2",
    opened_at=None,
    order_id="korder_pending",
    mode="live",
) -> DirectionalPosition:
    if opened_at is None:
        opened_at = datetime(2026, 6, 20, 0, 0, 0)
    return DirectionalPosition(
        market_id=market_id,
        side="NO",
        entry_price=0.93,
        size=5,
        strategy="maker_longshot",
        mode=mode,
        opened_at=opened_at,
        stop_loss=None,
        take_profit=None,
        notional=4.65,
        status="pending",
        order_id=order_id,
    )


@pytest.mark.asyncio
async def test_tracker_pending_fill_transitions_to_open(tmp_path):
    """On FILLED status, pending maker position transitions to open."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    pos = _make_pending_position()
    store.record_position(pos)

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    client.get_order = AsyncMock(return_value={"status": OrderStatus.FILLED, "filled_size": 5.0, "size": 5.0})

    tracker = Tracker(store, kalshi_client=client, executor=MagicMock(), risk_manager=MagicMock())
    await tracker.sweep(now=datetime(2026, 6, 20, 0, 30, 0), order_ttl_minutes=60.0)

    open_pos = store.open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].market_id == "kalshi:KX-ML-2"
    # Still no closed positions — it became open, not closed
    assert len(store.pending_positions()) == 0


@pytest.mark.asyncio
async def test_tracker_pending_past_ttl_cancelled_and_closed(tmp_path):
    """Pending past order_ttl_minutes → cancel_order called + position closed."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    # opened 90 min ago
    pos = _make_pending_position(opened_at=datetime(2026, 6, 20, 0, 0, 0))
    store.record_position(pos)

    cancel_calls = []

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    # Still resting (not filled)
    client.get_order = AsyncMock(return_value={"status": OrderStatus.OPEN, "filled_size": 0.0, "size": 5.0})

    async def mock_cancel(order_id):
        cancel_calls.append(order_id)

    client.cancel_order = AsyncMock(side_effect=mock_cancel)

    tracker = Tracker(store, kalshi_client=client, executor=MagicMock(), risk_manager=MagicMock())
    # sweep at 90 min after opened_at, TTL is 60 min
    await tracker.sweep(now=datetime(2026, 6, 20, 1, 30, 0), order_ttl_minutes=60.0)

    assert cancel_calls == ["korder_pending"]
    # Pending list should be empty
    assert len(store.pending_positions()) == 0


@pytest.mark.asyncio
async def test_tracker_pending_within_ttl_not_cancelled(tmp_path):
    """Pending within TTL and not filled → no cancellation."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    # opened 30 min ago
    pos = _make_pending_position(opened_at=datetime(2026, 6, 20, 0, 0, 0))
    store.record_position(pos)

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    client.get_order = AsyncMock(return_value={"status": OrderStatus.OPEN, "filled_size": 0.0, "size": 5.0})
    client.cancel_order = AsyncMock()

    tracker = Tracker(store, kalshi_client=client, executor=MagicMock(), risk_manager=MagicMock())
    await tracker.sweep(now=datetime(2026, 6, 20, 0, 30, 0), order_ttl_minutes=60.0)

    client.cancel_order.assert_not_called()
    # Still pending
    assert len(store.pending_positions()) == 1


@pytest.mark.asyncio
async def test_tracker_maker_open_position_not_stop_lossed(tmp_path):
    """maker_longshot open positions are NOT swept by stop-loss (hold to resolution)."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    # Maker position, opened 10h ago, NO price has crashed (would trigger SL if checked)
    maker_pos = DirectionalPosition(
        market_id="kalshi:KX-ML-OPEN",
        side="NO",
        entry_price=0.93,
        size=5,
        strategy="maker_longshot",
        mode="live",
        opened_at=datetime(2026, 6, 19, 14, 0, 0),
        stop_loss=0.70,
        take_profit=0.99,
        notional=4.65,
        status="open",
    )
    store.record_position(maker_pos)

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))

    executor = MagicMock()
    executor.close_position = AsyncMock()

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=MagicMock())
    await tracker.sweep(now=datetime(2026, 6, 20, 0, 0, 0), max_hold_hours=72)

    executor.close_position.assert_not_called()
    assert len(store.open_positions()) == 1  # still open


@pytest.mark.asyncio
async def test_tracker_maker_resolves_settles_pnl(tmp_path):
    """Resolved maker position books P&L (NO wins → resolution_price=1.0)."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    maker_pos = DirectionalPosition(
        market_id="kalshi:KX-ML-RES",
        side="NO",
        entry_price=0.93,
        size=5,
        strategy="maker_longshot",
        mode="paper",
        opened_at=datetime(2026, 6, 19, 0, 0, 0),
        stop_loss=None,
        take_profit=None,
        notional=4.65,
        status="open",
    )
    store.record_position(maker_pos)

    client = MagicMock()
    # Market resolved NO (YES lost) → NO position wins at 1.0
    resolved_market = MagicMock()
    resolved_market.result = "no"
    client.get_market = AsyncMock(return_value=resolved_market)

    tracker = Tracker(store, kalshi_client=client, executor=MagicMock(), risk_manager=MagicMock())
    await tracker.sweep(now=datetime(2026, 6, 20, 0, 0, 0))

    assert len(store.open_positions()) == 0
    summary = store.pnl_summary()
    assert summary["closed_count"] == 1
    # P&L net of the Kalshi entry fee: (1.0 - 0.93)*5 - fee_per_contract(0.93)*5
    from core.kalshi_fees import fee_per_contract
    expected = (1.0 - 0.93) * 5 - fee_per_contract(0.93) * 5
    assert abs(summary["total_realized_pnl"] - expected) < 1e-6


async def test_tracker_strips_venue_prefix_before_get_market(tmp_path):
    """Regression: tracker must call get_market with the BARE ticker, not the
    'kalshi:'-prefixed market_id. Without stripping, get_market returns nothing
    and resolution NEVER fires (every paper position stuck open, $0 realized)."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    store.record_position(
        DirectionalPosition(
            market_id="kalshi:KXHIGHNY-26JUN22-B74.5",
            side="NO",
            entry_price=0.93,
            size=8,
            strategy="maker_longshot",
            mode="paper",
            opened_at=datetime(2026, 6, 22, 0, 0, 0),
            stop_loss=None,
            take_profit=None,
            notional=7.44,
            status="open",
        )
    )

    seen = {}

    async def fake_get_market(ticker):
        seen["ticker"] = ticker
        m = MagicMock()
        # Only the BARE ticker resolves; the prefixed form returns no result.
        m.result = "no" if ticker == "KXHIGHNY-26JUN22-B74.5" else None
        return m

    client = MagicMock()
    client.get_market = fake_get_market
    tracker = Tracker(store, kalshi_client=client, executor=MagicMock(), risk_manager=MagicMock())
    await tracker.sweep(now=datetime(2026, 6, 23, 0, 0, 0))

    assert seen["ticker"] == "KXHIGHNY-26JUN22-B74.5"  # prefix stripped
    assert store.pnl_summary()["closed_count"] == 1     # NO won → settled



@pytest.mark.asyncio
async def test_skips_long_dated_market():
    """A market resolving in 400 days with max_days_to_resolution=90 → no candidate."""
    strategy = MakerLongshotStrategy(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.05,
        max_days_to_resolution=90.0,
    )
    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    market = make_market(yes_price=0.08, category="Sports", close_time=far_future)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.94))
    assert candidates == [], f"Expected no candidates for 400-day market, got {candidates}"


@pytest.mark.asyncio
async def test_emits_near_term_market():
    """A qualifying longshot resolving in 30 days (within 90-day cap) → emits candidate."""
    strategy = MakerLongshotStrategy(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.05,
        max_days_to_resolution=90.0,
    )
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(yes_price=0.08, category="Sports", close_time=near_term)
    candidates = await strategy.scan([market], make_ctx(no_ask=0.94))
    assert len(candidates) == 1, f"Expected 1 candidate for 30-day market, got {candidates}"
    assert candidates[0].side == "NO"
    assert candidates[0].strategy == "maker_longshot"


# ── Config tests ──────────────────────────────────────────────────────────────

def test_maker_longshot_config_defaults():
    """MakerLongshotCfg has expected defaults (loosened: max_yes_price=0.20, max_days=30)."""
    from utils.config_loader import MakerLongshotCfg
    cfg = MakerLongshotCfg()
    assert cfg.mode == "paper"
    assert cfg.min_structural_score == 0.02
    assert cfg.min_yes_price == 0.05
    assert cfg.max_yes_price == 0.20          # loosened from 0.15; NO band [0.80, 0.95]
    assert cfg.price_improvement_cents == 1
    assert cfg.order_ttl_minutes == 60.0
    assert cfg.skip_categories == []
    assert cfg.max_days_to_resolution == 30.0  # tightened from 90 to focus on near-term


def test_directional_config_has_maker_longshot():
    """DirectionalConfig exposes maker_longshot attribute with correct defaults."""
    from utils.config_loader import DirectionalConfig
    cfg = DirectionalConfig()
    assert hasattr(cfg, "maker_longshot")
    assert cfg.maker_longshot.mode == "paper"
    assert cfg.maker_longshot.max_yes_price == 0.20  # loosened from 0.15


def test_maker_longshot_mode_typo_raises_config_error(tmp_path):
    """Typo'd mode in maker_longshot block raises ConfigError."""
    from utils.config_loader import load_config, ConfigError
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "maker_longshot": {"mode": "papper"},  # typo
        }
    }
    config_file.write_text(yaml.dump(data))
    with pytest.raises(ConfigError, match="mode"):
        load_config(str(config_file))


def test_maker_longshot_config_loaded_from_yaml(tmp_path):
    """maker_longshot block in YAML is parsed into MakerLongshotCfg."""
    from utils.config_loader import load_config
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "maker_longshot": {
                "mode": "paper",
                "min_structural_score": 0.05,
                "max_yes_price": 0.10,
                "price_improvement_cents": 2,
                "order_ttl_minutes": 90.0,
                "skip_categories": ["Sports"],
            },
        }
    }
    config_file.write_text(yaml.dump(data))
    cfg = load_config(str(config_file))
    ml = cfg.directional.maker_longshot
    assert ml.min_structural_score == 0.05
    assert ml.max_yes_price == 0.10
    assert ml.price_improvement_cents == 2
    assert ml.order_ttl_minutes == 90.0
    assert ml.skip_categories == ["Sports"]


def test_maker_longshot_live_mode_accepted(tmp_path):
    """mode=live is accepted without raising ConfigError."""
    from utils.config_loader import load_config
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "maker_longshot": {"mode": "live"},
        }
    }
    config_file.write_text(yaml.dump(data))
    cfg = load_config(str(config_file))
    assert cfg.directional.maker_longshot.mode == "live"


# ── Engine tests ──────────────────────────────────────────────────────────────

def make_engine_config(sc_min_edge=3, ml_max_yes=0.15, ml_min_score=0.02):
    sc = SimpleNamespace(mode="paper", min_edge_cents=sc_min_edge, skip_categories=[])
    ai = SimpleNamespace(
        mode="paper", min_confidence=0.7, min_edge_pct=0.05,
        kelly_fraction=0.25, stop_loss_pct=0.30, take_profit_pct=0.50,
        max_hold_hours=72.0,
    )
    ml = SimpleNamespace(
        mode="paper",
        min_structural_score=ml_min_score,
        min_yes_price=0.05,
        max_yes_price=ml_max_yes,
        price_improvement_cents=1,
        order_ttl_minutes=60.0,
        skip_categories=[],
        max_days_to_resolution=9999.0,
    )
    caps = SimpleNamespace(total_exposure=30.0, max_position=8.0, max_open=4)
    return SimpleNamespace(
        db_path=":memory:",
        scan_interval_seconds=60,
        markets_per_cycle=50,
        category_exclude=[],
        min_volume=100,
        caps=caps,
        safe_compounder=sc,
        ai_directional=ai,
        maker_longshot=ml,
    )


class FakeKalshiClientEngine:
    def __init__(self, markets=None, no_ask=0.94):
        self._markets = markets or []
        self._no_ask = no_ask
        self.place_order_calls = []

    async def _get(self, endpoint, params=None):
        def _serialize_close_time(m):
            ct = getattr(m, "close_time", None)
            if ct is None:
                return None
            return ct.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        nested = [
            {
                "ticker": m.ticker,
                "event_ticker": getattr(m, "event_ticker", m.ticker),
                "series_ticker": getattr(m, "series_ticker", m.ticker),
                "title": m.title,
                "status": "open",
                "close_time": _serialize_close_time(m),
            }
            for m in self._markets
        ]
        return {"events": [{"markets": nested, "event_ticker": "FAKE"}], "cursor": None}

    async def get_orderbook_unified(self, ticker):
        market_yes = next((m.yes_price for m in self._markets if m.ticker == ticker), 0.08)
        ob = SimpleNamespace()
        yes_side = SimpleNamespace()
        yes_side.best_bid = round(market_yes - 0.01, 4)
        yes_side.best_ask = round(market_yes + 0.01, 4)
        yes_side.mid_price = market_yes
        no_side = SimpleNamespace()
        no_side.best_ask = self._no_ask
        no_side.best_bid = self._no_ask - 0.01
        no_side.mid_price = self._no_ask - 0.005
        ob.yes = yes_side
        ob.no = no_side
        return ob

    async def get_market(self, ticker):
        for m in self._markets:
            if m.ticker == ticker or m.ticker in ticker:
                return m
        return None

    async def place_order(self, **kwargs):
        self.place_order_calls.append(kwargs)
        return SimpleNamespace(order_id="korder_engine_test")

    async def get_balance(self):
        return 100.0


class FakeRiskManager:
    class state:
        kill_switch_triggered = False

    def check_directional_order(self, order, open_count, directional_exposure, max_position, max_total, max_open):
        return True


def test_engine_builds_maker_longshot_strategy():
    """DirectionalEngine constructs MakerLongshotStrategy as a third strategy."""
    from core.directional.engine import DirectionalEngine

    cfg = make_engine_config()
    client = FakeKalshiClientEngine(markets=[])
    engine = DirectionalEngine(cfg, client, None, FakeRiskManager())

    strategy_names = [s.name for s, _ in engine._strategies]
    assert "maker_longshot" in strategy_names


@pytest.mark.asyncio
async def test_engine_run_once_paper_records_maker_position():
    """run_once with a Sports longshot market in paper mode records a maker_longshot position.

    With the corrected structural_score(1 - yes_mid, "NO", category) call,
    structural_score(0.92, "NO", "Sports") ≈ 0.10, which clears the default
    min_structural_score=0.02. No threshold workaround needed.

    The market uses event_ticker="KXNFL-..." so that categorize() returns "Sports",
    matching how the engine's scanner assigns category via categorize(event_ticker).
    """
    from core.directional.engine import DirectionalEngine

    cfg = make_engine_config(sc_min_edge=999, ml_min_score=0.02, ml_max_yes=0.15)
    # Longshot NFL (Sports) market: yes=0.08 → structural_score(0.92, NO, Sports) ≈ 0.10 > 0.02
    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = make_market(ticker="KXNFL-25JAN15-TBD", yes_price=0.08, category="Sports", close_time=near_term)
    market.event_ticker = "KXNFL-25JAN15"
    market.series_ticker = "KXNFL"
    client = FakeKalshiClientEngine(markets=[market], no_ask=0.94)
    engine = DirectionalEngine(cfg, client, None, FakeRiskManager())

    await engine.run_once()

    positions = engine.store.open_positions()
    maker_positions = [p for p in positions if p.strategy == "maker_longshot"]
    assert len(maker_positions) >= 1, f"Expected at least one maker_longshot position, got: {[p.strategy for p in positions]}"
    assert maker_positions[0].mode == "paper"
    assert maker_positions[0].status == "open"
    assert client.place_order_calls == [], "paper mode must not call place_order"


@pytest.mark.asyncio
async def test_engine_safe_compounder_still_works():
    """Adding maker_longshot doesn't break SafeCompounder strategy."""
    from core.directional.engine import DirectionalEngine

    # Low SC threshold so SC fires; high maker score threshold so it doesn't
    cfg = make_engine_config(sc_min_edge=1, ml_min_score=0.99)
    market = make_market(ticker="KX-SC-COMPAT", yes_price=0.05, category="Finance")
    client = FakeKalshiClientEngine(markets=[market], no_ask=0.07)
    engine = DirectionalEngine(cfg, client, None, FakeRiskManager())

    await engine.run_once()

    positions = engine.store.open_positions()
    sc_positions = [p for p in positions if p.strategy == "safe_compounder"]
    assert len(sc_positions) >= 1
