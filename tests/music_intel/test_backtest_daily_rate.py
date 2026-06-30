"""M1: the backtest must use the snapshot's own 'Daily' column as the forward rate,
not the average YTD rate. This is what down-weights a faded viral one-hit spike
(huge YTD delta, but a LOW current daily rate) — matching the live model, which
reads kworb's Daily column directly.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from music_intel.artist_backtest import backtest_year
from music_intel.sources.ytd import _parse_daily, _parse_totals

# Two artists, as-of June:
#   Viral One     — YTD delta 2000 (one viral song), but current Daily only 2.0 (faded)
#   Sustained Star— YTD delta 1200, but current Daily 20.0 (broad catalog, still hot)
# Avg-YTD-rate model -> picks Viral One (WRONG). Daily-rate model -> picks Sustained Star.
JAN = ("<table><tr><th>Artist</th><th>Streams</th><th>Daily</th></tr>"
       "<tr><td><a href='/spotify/artist/v.html'>Viral One</a></td><td>0</td><td>0.5</td></tr>"
       "<tr><td><a href='/spotify/artist/s.html'>Sustained Star</a></td><td>10000</td><td>18.0</td></tr></table>")
SNAP = ("<table><tr><th>Artist</th><th>Streams</th><th>Daily</th></tr>"
        "<tr><td><a href='/spotify/artist/v.html'>Viral One</a></td><td>2000</td><td>2.0</td></tr>"
        "<tr><td><a href='/spotify/artist/s.html'>Sustained Star</a></td><td>11200</td><td>20.0</td></tr></table>")


def test_parse_daily_reads_second_numeric_column():
    daily = _parse_daily(SNAP)
    assert daily["Viral One"] == pytest.approx(2.0)
    assert daily["Sustained Star"] == pytest.approx(20.0)
    # streams parser still reads the FIRST numeric (unchanged)
    totals = _parse_totals(SNAP)
    assert totals["Viral One"] == pytest.approx(2000.0)


def test_parse_daily_empty_when_no_daily_column():
    # Old-style 2-column snapshot (artist, streams) -> no daily values
    two_col = ("<table><tr><th>Artist</th><th>Streams</th></tr>"
               "<tr><td><a href='/spotify/artist/x.html'>X</a></td><td>5</td></tr></table>")
    assert _parse_daily(two_col) == {}


def _http():
    h = MagicMock()
    async def _get(url, *a, **k):
        r = MagicMock(); r.raise_for_status = MagicMock()
        if "archive.org/wayback/available" in url:
            ts = k.get("params", {}).get("timestamp", "")
            r.json = MagicMock(return_value={"archived_snapshots": {"closest": {
                "available": True,
                "url": f"http://web.archive.org/web/{ts}/https://kworb.net/spotify/artists.html"}}})
        elif "0101/" in url:
            r.text = JAN
        else:
            r.text = SNAP
        return r
    h.get = AsyncMock(side_effect=_get); return h


@pytest.mark.asyncio
async def test_backtest_uses_daily_rate_not_avg_ytd():
    res = await backtest_year(_http(), 2024, as_of_month=6)
    assert res is not None
    # Viral One is the YTD-delta leader...
    assert res["ytd_leader"] == "Viral One"
    # ...but the Daily-column forward rate makes Sustained Star the model #1.
    assert res["model_top"] == "Sustained Star"
