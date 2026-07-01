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
