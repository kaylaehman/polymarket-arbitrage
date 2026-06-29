from core.directional.store import category_for_market_id

def test_pm_market_id_is_music():
    assert category_for_market_id("pm:12345") == "music"

def test_kalshi_weather_still_weather():
    assert category_for_market_id("kalshi:KXHIGHNY-26JUN29-B83.5") == "weather"
