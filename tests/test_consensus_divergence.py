import pytest
from core.directional.strategies.consensus_divergence import (
    ConsensusDivergenceStrategy, divergence_side,
)

def test_divergence_side_yes_when_gate_higher():
    assert divergence_side(0.40, 0.20, 0.10) == ("YES", pytest.approx(0.20))

def test_divergence_side_no_when_gate_lower():
    side, edge = divergence_side(0.05, 0.20, 0.10)
    assert side == "NO" and edge == pytest.approx(0.15)

def test_divergence_side_none_when_below_threshold():
    assert divergence_side(0.22, 0.20, 0.10) is None

def test_name():
    s = ConsensusDivergenceStrategy(min_divergence=0.1, skip_categories=[])
    assert s.name == "consensus_divergence"

@pytest.mark.asyncio
async def test_scan_no_gate_data_returns_empty():
    s = ConsensusDivergenceStrategy(min_divergence=0.1, skip_categories=[])
    assert await s.scan([], {"no_ask": lambda t: None}) == []


@pytest.mark.asyncio
async def test_scan_sports_emits_candidate_on_divergence():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXNBA-27-WAS",
                        title="Will the Wizards win the 2027 NBA championship?",
                        yes_sub_title="Washington Wizards", subtitle="", category="Sports",
                        yes_price=0.18,
                        close_time=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=120),
                        to_unified_market_id=lambda: "kalshi:KXNBA-27-WAS")
    class _Sports:
        async def championship_probs(self, t): return {"Washington Wizards": 0.03}
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])
    cands = await s.scan([m], {"no_ask": lambda t: 0.80, "sports": _Sports()})
    assert len(cands) == 1
    assert cands[0].side == "NO"   # gate 0.03 << market 0.18 -> NO underpriced
    assert cands[0].strategy == "consensus_divergence"
    assert cands[0].market_id == "kalshi:KXNBA-27-WAS"


@pytest.mark.asyncio
async def test_scan_sports_no_divergence_no_candidate():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXNBA-27-WAS", title="t", yes_sub_title="Washington Wizards",
                        subtitle="", category="Sports", yes_price=0.18,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=120),
                        to_unified_market_id=lambda: "kalshi:KXNBA-27-WAS")
    class _Sports:
        async def championship_probs(self, t): return {"Washington Wizards": 0.16}  # close to 0.18
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])
    assert await s.scan([m], {"no_ask": lambda t: 0.80, "sports": _Sports()}) == []


@pytest.mark.asyncio
async def test_scan_skips_excluded_category():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXNBA-27-WAS", title="t", yes_sub_title="Washington Wizards",
                        subtitle="", category="Sports", yes_price=0.18,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=120),
                        to_unified_market_id=lambda: "kalshi:KXNBA-27-WAS")
    class _Sports:
        async def championship_probs(self, t): return {"Washington Wizards": 0.03}
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=["Sports"])
    assert await s.scan([m], {"no_ask": lambda t: 0.80, "sports": _Sports()}) == []


@pytest.mark.asyncio
async def test_scan_macro_emits_candidate_on_divergence():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXCPIYOY-26JUL-T3.0", title="Will CPI YoY be above 3.0%?",
                        yes_sub_title="", subtitle="", category="Economics", yes_price=0.50,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=20),
                        to_unified_market_id=lambda: "kalshi:KXCPIYOY-26JUL-T3.0")
    class _Macro:
        async def nowcast(self, indicator): return 4.2   # 4.2% >> 3.0 threshold -> P(YES)~1
    macro_cfg = SimpleNamespace(sigma={"CPIYOY": 0.2})
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[], macro_cfg=macro_cfg)
    cands = await s.scan([m], {"no_ask": lambda t: 0.50, "macro": _Macro()})
    assert len(cands) == 1 and cands[0].side == "YES"
    assert cands[0].strategy == "consensus_divergence"

@pytest.mark.asyncio
async def test_scan_macro_no_macro_cfg_skips():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXCPIYOY-26JUL-T3.0", title="t", yes_sub_title="", subtitle="",
                        category="Economics", yes_price=0.50,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=20),
                        to_unified_market_id=lambda: "kalshi:KXCPIYOY-26JUL-T3.0")
    class _Macro:
        async def nowcast(self, indicator): return 4.2
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])  # no macro_cfg
    assert await s.scan([m], {"no_ask": lambda t: 0.50, "macro": _Macro()}) == []

@pytest.mark.asyncio
async def test_scan_macro_nowcast_none_skips():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXCPIYOY-26JUL-T3.0", title="t", yes_sub_title="", subtitle="",
                        category="Economics", yes_price=0.50,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=20),
                        to_unified_market_id=lambda: "kalshi:KXCPIYOY-26JUL-T3.0")
    class _Macro:
        async def nowcast(self, indicator): return None
    macro_cfg = SimpleNamespace(sigma={"CPIYOY": 0.2})
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[], macro_cfg=macro_cfg)
    assert await s.scan([m], {"no_ask": lambda t: 0.50, "macro": _Macro()}) == []


@pytest.mark.asyncio
async def test_scan_per_game_sports_emits_candidate():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXMLBGAME-26JUN29-BALCWS-BAL", title="Baltimore vs Chicago Winner?",
                        yes_sub_title="Baltimore Orioles", subtitle="", category="Sports", yes_price=0.40,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1),
                        to_unified_market_id=lambda: "kalshi:KXMLBGAME-26JUN29-BALCWS-BAL")
    class _Sports:
        async def championship_probs(self, t): return {}
        async def game_probs(self, t): return {"Baltimore Orioles": 0.554, "Chicago White Sox": 0.446}
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])
    cands = await s.scan([m], {"no_ask": lambda t: 0.6, "sports": _Sports()})
    assert len(cands) == 1
    assert cands[0].side == "YES"   # gate 0.554 vs market 0.40 -> YES +0.154
    assert cands[0].market_id == "kalshi:KXMLBGAME-26JUN29-BALCWS-BAL"

@pytest.mark.asyncio
async def test_scan_per_game_no_team_match_skips():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXMLBGAME-26JUN29-BALCWS-BAL", title="t",
                        yes_sub_title="A's", subtitle="", category="Sports", yes_price=0.40,
                        close_time=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1),
                        to_unified_market_id=lambda: "kalshi:KXMLBGAME-26JUN29-BALCWS-BAL")
    class _Sports:
        async def championship_probs(self, t): return {}
        async def game_probs(self, t): return {"Baltimore Orioles": 0.554, "Chicago White Sox": 0.446}
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])
    # "A's" matches no team -> no candidate
    assert await s.scan([m], {"no_ask": lambda t: 0.6, "sports": _Sports()}) == []
