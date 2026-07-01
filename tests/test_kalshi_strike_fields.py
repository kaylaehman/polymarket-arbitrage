from kalshi_client.models import KalshiMarket

def test_kalshi_market_has_strike_fields():
    m = KalshiMarket(ticker="KXHIGHNY-26JUL01-B98.5", event_ticker="KXHIGHNY",
                     series_ticker="KXHIGHNY", title="t", subtitle="",
                     yes_price=0.5, no_price=0.5, status="active", result=None,
                     volume=0, open_interest=0, close_time=None, category="Climate and Weather",
                     strike_type="between", floor_strike=98.0, cap_strike=99.0)
    assert m.strike_type == "between" and m.floor_strike == 98.0 and m.cap_strike == 99.0
