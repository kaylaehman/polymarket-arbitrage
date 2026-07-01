import pytest
from core.directional.climate.base import ParsedClimate, ClimateSignal
from core.directional.climate.edge import make_candidates

def _p(): return ParsedClimate("high_temp","kalshi:KXHIGHNY-26JUL01-T99","KXHIGHNY",
                                "nyc","2026-07-01","greater",99.0,None,"temp")

def test_longshot_no_when_p_very_low():
    c = make_candidates(_p(), market_price=0.12, signal=ClimateSignal(0.02,0.9,"nws"))
    assert len(c) == 1 and c[0].side == "NO" and c[0].strategy == "climate_paper"

def test_directional_yes_when_model_far_above_price():
    c = make_candidates(_p(), market_price=0.30, signal=ClimateSignal(0.70,0.8,"nws"))
    assert any(x.side == "YES" for x in c)
    yes = [x for x in c if x.side == "YES"][0]
    assert yes.ai_probability == pytest.approx(0.70)
    assert yes.edge == pytest.approx(0.40, abs=1e-9)

def test_no_candidate_inside_band():
    # p≈price, not a longshot -> nothing
    assert make_candidates(_p(), market_price=0.50, signal=ClimateSignal(0.52,0.5,"nws")) == []

def test_dedup_same_side():
    # p tiny AND far below price -> longshot-NO and directional-NO agree -> one candidate
    c = make_candidates(_p(), market_price=0.40, signal=ClimateSignal(0.02,0.9,"nws"))
    assert len([x for x in c if x.side == "NO"]) == 1


def test_no_candidate_priced_at_no_cost():
    # REGRESSION: a NO candidate must carry the NO entry cost (1 - yes_price), NOT
    # the YES price. Downstream (decider sizing, Kelly, executor booking) treats
    # candidate.market_price as the cost of `side`; the old code left it at the YES
    # price, so longshot-NO bucket bets oversized ~1/yes_price and booked phantom P&L.
    c = make_candidates(_p(), market_price=0.12, signal=ClimateSignal(0.02, 0.9, "nws"))
    no = [x for x in c if x.side == "NO"][0]
    assert no.market_price == pytest.approx(0.88)    # 1 - 0.12, not 0.12
    assert no.ai_probability == pytest.approx(0.02)  # ai_probability stays P(YES)
    assert no.edge >= 0                              # magnitude, never negative


def test_yes_candidate_priced_at_yes_cost():
    # YES entry cost is the YES price itself (unchanged).
    c = make_candidates(_p(), market_price=0.30, signal=ClimateSignal(0.70, 0.8, "nws"))
    yes = [x for x in c if x.side == "YES"][0]
    assert yes.market_price == pytest.approx(0.30)


def test_no_directional_at_extreme_price():
    """Directional is suppressed at extreme prices (model-error zone); only the
    longshot-NO tail may fire there."""
    # yes=0.015, model p=0.60 -> would be a huge YES 'edge' but price is extreme.
    c = make_candidates(_p(), market_price=0.015, signal=ClimateSignal(0.60, 0.7, "nws"))
    assert all(x.side != "YES" for x in c)   # no directional YES at yes<0.05
    # A mid-book divergence still fires directionally.
    c2 = make_candidates(_p(), market_price=0.30, signal=ClimateSignal(0.70, 0.8, "nws"))
    assert any(x.side == "YES" for x in c2)
