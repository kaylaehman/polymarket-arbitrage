# tests/test_macro_data.py
import pytest
from core.macro_data import parse_macro_ticker, MacroMarket

@pytest.mark.parametrize("ticker,series,indicator,thr,mtype", [
    ("KXCPIYOY-26JUN-T3.9",  "KXCPIYOY",  "CPIYOY",  3.9, "threshold"),
    ("KXCPICORE-26JUN-T0.3", "KXCPICORE", "CPICORE", 0.3, "threshold"),
    ("KXCPI-26JUN-T0.0",     "KXCPI",     "CPI",     0.0, "threshold"),
    ("KXPCECORE-26MAY-T0.4", "KXPCECORE", "PCECORE", 0.4, "threshold"),
    ("KXGDP-26Q2-T2.5",      "KXGDP",     "GDP",     2.5, "threshold"),
    ("KXCPIYOY-26JUN-B3.5",  "KXCPIYOY",  "CPIYOY",  3.5, "bucket"),
])
def test_parse_macro_ticker(ticker, series, indicator, thr, mtype):
    m = parse_macro_ticker(ticker)
    assert m is not None
    assert (m.series, m.indicator, m.market_type) == (series, indicator, mtype)
    assert m.threshold == pytest.approx(thr)
    if mtype == "bucket":
        assert m.bucket_lo == pytest.approx(thr)

@pytest.mark.parametrize("ticker", ["", "KXHIGHNY-26JUN29-T79", "KXBTCD-26JUN-T100000", "garbage"])
def test_parse_macro_ticker_rejects_non_macro(ticker):
    assert parse_macro_ticker(ticker) is None

def test_longest_prefix_wins():
    # KXCPI is a prefix of KXCPICORE — must resolve to the more specific series
    assert parse_macro_ticker("KXCPICORE-26JUN-T0.3").indicator == "CPICORE"


# Task 2 tests — gate math
from core.macro_data import macro_margin, macro_threshold_keep, macro_bucket_keep

def test_macro_margin_zscore():
    # threshold 3.9, nowcast 3.2, sigma 0.12 -> z = 0.7/0.12 ≈ 5.83
    assert macro_margin(3.2, 0.12, 3.9) == pytest.approx((3.9 - 3.2) / 0.12, rel=1e-6)

def test_macro_margin_degenerate_sigma_is_neg_inf():
    assert macro_margin(3.2, 0.0, 3.9) == float("-inf")

def test_threshold_keep_far_tail_true():
    # nowcast 3.2 well below threshold 3.9 -> NO("above 3.9") is safe
    assert macro_threshold_keep(3.2, 0.12, 3.9, min_sigma=2.0) is True

def test_threshold_keep_near_threshold_false():
    # nowcast 3.85 just under 3.9, sigma 0.12 -> z≈0.42 < 2.0 -> SKIP
    assert macro_threshold_keep(3.85, 0.12, 3.9, min_sigma=2.0) is False

def test_bucket_keep_nowcast_outside_true():
    # bucket [3.0,3.2], nowcast 3.8 far above hi -> tail -> keep NO
    assert macro_bucket_keep(3.8, 3.0, 3.2, sigma=0.12, min_sigma=2.0) is True

def test_bucket_keep_nowcast_inside_false():
    # nowcast 3.1 inside [3.0,3.2] -> likely outcome -> SKIP NO
    assert macro_bucket_keep(3.1, 3.0, 3.2, sigma=0.12, min_sigma=2.0) is False


# Task 3 tests — MacroNowcastClient
from unittest.mock import AsyncMock, MagicMock
from core.macro_data import MacroNowcastClient

def _resp(json_obj):
    r = MagicMock(); r.json = MagicMock(return_value=json_obj); r.raise_for_status = MagicMock()
    return r

@pytest.mark.asyncio
async def test_gdp_nowcast_from_fred():
    http = MagicMock()
    http.get = AsyncMock(return_value=_resp({"observations": [{"value": "2.7"}]}))
    c = MacroNowcastClient(http=http, fred_api_key="k")
    assert await c.nowcast("GDP") == pytest.approx(2.7)

@pytest.mark.asyncio
async def test_gdp_nowcast_missing_key_returns_none():
    c = MacroNowcastClient(http=MagicMock(), fred_api_key=None)
    assert await c.nowcast("GDP") is None

@pytest.mark.asyncio
async def test_nowcast_http_error_returns_none():
    http = MagicMock(); http.get = AsyncMock(side_effect=RuntimeError("boom"))
    c = MacroNowcastClient(http=http, fred_api_key="k")
    assert await c.nowcast("GDP") is None


from core.macro_data import _parse_cleveland_nowcast

def _cleveland_resp(series_data):
    payload = [{"dataset": [{"seriesname": "CPI Inflation",
                             "data": [{"value": ""}, {"value": "0.21"}, {"value": "0.27"}]},
                            {"seriesname": "Core CPI Inflation",
                             "data": [{"value": "0.30"}]}]}]
    r = MagicMock(); r.json = MagicMock(return_value=payload)
    return r

def test_parse_cleveland_cpi_latest_value():
    assert _parse_cleveland_nowcast(_cleveland_resp(None), "CPI") == pytest.approx(0.27)

def test_parse_cleveland_core_cpi():
    assert _parse_cleveland_nowcast(_cleveland_resp(None), "CPICORE") == pytest.approx(0.30)

def test_parse_cleveland_cpiyoy_unavailable_returns_none():
    # CPIYOY has no MoM series -> None (gate safely skips)
    assert _parse_cleveland_nowcast(_cleveland_resp(None), "CPIYOY") is None


# ── CPIYOY year-over-year nowcast (assembled from FRED index + Cleveland MoM) ──
from core.macro_data import cpi_yoy_nowcast

def test_cpi_yoy_nowcast_math():
    # latest CPI index 320.0, MoM nowcast +0.3% -> projected 320.96;
    # index 12 months before the release month = 310.0 -> YoY = (320.96/310 - 1)*100
    yoy = cpi_yoy_nowcast(latest_index=320.0, prior_year_index=310.0, mom_nowcast_pct=0.3)
    assert yoy == pytest.approx((320.0 * 1.003 / 310.0 - 1.0) * 100.0, rel=1e-9)

def test_cpi_yoy_nowcast_zero_prior_is_safe():
    # degenerate prior index -> None-safe upstream; function guards div-by-zero
    assert cpi_yoy_nowcast(320.0, 0.0, 0.3) is None


@pytest.mark.asyncio
async def test_cpiyoy_nowcast_assembled(monkeypatch):
    """nowcast('CPIYOY') assembles FRED CPIAUCSL history + Cleveland MoM."""
    from core.macro_data import MacroNowcastClient
    import core.macro_data as md
    # FRED CPIAUCSL: latest 2026-05 = 320.0, 2025-06 (12mo before June release) = 310.0
    fred_obs = {"observations": [
        {"date": "2025-05-01", "value": "309.0"},
        {"date": "2025-06-01", "value": "310.0"},
        {"date": "2026-04-01", "value": "319.0"},
        {"date": "2026-05-01", "value": "320.0"},
    ]}
    cleveland = [{"dataset": [{"seriesname": "CPI Inflation",
                              "data": [{"value": "0.30"}]}]}]
    def _resp(j):
        r = MagicMock(); r.json = MagicMock(return_value=j); r.raise_for_status = MagicMock()
        return r
    async def fake_get(url, params=None):
        if "stlouisfed" in url:
            return _resp(fred_obs)
        return _resp(cleveland)
    http = MagicMock(); http.get = AsyncMock(side_effect=fake_get)
    c = MacroNowcastClient(http=http, fred_api_key="k")
    val = await c.nowcast("CPIYOY")
    # release month = 2026-06; prior = 2025-06 = 310.0; latest = 320.0; mom 0.30
    assert val == pytest.approx((320.0 * 1.003 / 310.0 - 1.0) * 100.0, rel=1e-6)
