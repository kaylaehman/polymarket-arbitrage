"""Tests for music_intel.artist_backtest — Wayback replay of prior years."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from music_intel.artist_backtest import backtest_year, score_backtest, _wayback_html

JAN = "<table><tr><th>Artist</th><th>Streams</th></tr>" \
      "<tr><td><a href='/spotify/artist/ts_songs.html'>Taylor Swift</a></td><td>100000.0</td></tr>" \
      "<tr><td><a href='/spotify/artist/dk_songs.html'>Drake</a></td><td>120000.0</td></tr></table>"
JUN = "<table><tr><th>Artist</th><th>Streams</th></tr>" \
      "<tr><td><a href='/spotify/artist/ts_songs.html'>Taylor Swift</a></td><td>115000.0</td></tr>" \
      "<tr><td><a href='/spotify/artist/dk_songs.html'>Drake</a></td><td>128000.0</td></tr></table>"
# Taylor YTD = 15000 (5000 more than Drake's 8000) -> Taylor should be model #1


def _http():
    h = MagicMock()
    async def _get(url, *a, **k):
        r = MagicMock(); r.raise_for_status = MagicMock()
        if "archive.org/wayback/available" in url:
            ts = k.get("params", {}).get("timestamp", "")
            r.json = MagicMock(return_value={"archived_snapshots":{"closest":{"available":True,
                "url": f"http://web.archive.org/web/{ts}/https://kworb.net/spotify/artists.html"}}})
        elif "0101/" in url:   # Jan snapshot
            r.text = JAN
        else:                  # as-of snapshot
            r.text = JUN
        return r
    h.get = AsyncMock(side_effect=_get); return h


@pytest.mark.asyncio
async def test_backtest_picks_higher_ytd_leader():
    res = await backtest_year(_http(), 2024, as_of_month=6)
    assert res is not None
    assert res["model_top"] == "Taylor Swift"          # higher YTD gain
    assert res["ytd_leader"] == "Taylor Swift"


@pytest.mark.asyncio
async def test_backtest_missing_snapshot_returns_none():
    h = MagicMock()
    async def _get(url, *a, **k):
        r = MagicMock(); r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"archived_snapshots": {}}); r.text = ""
        return r
    h.get = AsyncMock(side_effect=_get)
    assert await backtest_year(h, 2024) is None


def test_score_backtest_correct_and_rank():
    res = {"year":2024,"model_top":"Taylor Swift",
           "ranking":[("Taylor Swift",0.6),("Drake",0.4)],"ytd_leader":"Taylor Swift"}
    s = score_backtest(res, "Taylor Swift")
    assert s["correct"] is True and s["winner_rank"] == 1
    s2 = score_backtest(res, "Drake")
    assert s2["correct"] is False and s2["winner_rank"] == 2
    s3 = score_backtest(res, "Beyonce")
    assert s3["correct"] is False and s3["winner_rank"] is None


def test_summarize_sweep_counts_and_rates():
    from music_intel.artist_backtest import summarize_sweep
    pts = [
        {"year":2024,"month":6,"correct":True,"winner_rank":1},
        {"year":2023,"month":6,"correct":False,"winner_rank":2},
        {"year":2022,"month":6,"correct":True,"winner_rank":1},
        {"year":2022,"month":8,"correct":False,"winner_rank":None},
    ]
    s = summarize_sweep(pts)
    assert s["n"] == 4 and s["hits"] == 2
    assert s["hit_rate"] == pytest.approx(0.5)
    assert s["avg_winner_rank"] == pytest.approx((1+2+1)/3)   # None excluded
    assert s["winner_in_top3"] == pytest.approx(3/4)          # ranks 1,2,1 are <=3; None is not


def test_summarize_sweep_empty():
    from music_intel.artist_backtest import summarize_sweep
    s = summarize_sweep([])
    assert s["n"] == 0 and s["hit_rate"] == 0 and s["avg_winner_rank"] is None


@pytest.mark.asyncio
async def test_backtest_sweep_collects_points(monkeypatch):
    import music_intel.artist_backtest as bt
    async def fake_year(http, year, as_of_month=6, top_n=10, maturity_lambda=0.0):
        if year == 2099: return None   # simulate a data gap
        return {"year":year,"as_of":f"{year}-{as_of_month:02d}","model_top":"Taylor Swift",
                "ranking":[("Taylor Swift",0.8),("Drake",0.2)],"ytd_leader":"Taylor Swift"}
    monkeypatch.setattr(bt, "backtest_year", fake_year)
    pts = await bt.backtest_sweep(MagicMock(), {2024:"Taylor Swift", 2099:"X"}, months=[6])
    assert len(pts) == 1 and pts[0]["year"] == 2024 and pts[0]["correct"] is True
