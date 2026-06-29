"""CLI command logic (no network — deps injected). Verifies argparse wiring and
that dry-run-edge asserts execution stays disabled.
"""
import datetime
import pytest

from music_intel.cli import (
    build_argparser, cmd_backfill, cmd_project, cmd_dry_run_edge, cmd_backtest,
)
from music_intel.engine import MusicIntelEngine
from music_intel.alerts import CollectingSink
from music_intel.sources.base import ChartRecord
from music_intel.sources.markets import MarketCandidate

TODAY = datetime.date(2026, 6, 29)


def _rec(artist, title, streams7, rank=1):
    return ChartRecord(source="kworb", chart="hot100", as_of=TODAY, rank=rank,
                       title=title, artist=artist, streams_7day=streams7)


class _Source:
    name = "kworb"; trust_tier = 1
    def __init__(self, recs): self._r = recs
    async def fetch(self, chart, as_of=None): return list(self._r)


class _Store:
    def __init__(self): self.saved = []
    def record_snapshot(self, r): self.saved.append(r)
    def record_projection(self, **k): pass


# ── argparse wiring ──────────────────────────────────────────────────────────

def test_argparser_accepts_all_subcommands():
    p = build_argparser()
    for cmd in ("backfill", "project", "backtest", "dry-run-edge"):
        assert p.parse_args([cmd]).command == cmd

def test_argparser_requires_subcommand():
    with pytest.raises(SystemExit):
        build_argparser().parse_args([])

def test_project_takes_artist_title():
    a = build_argparser().parse_args(["project", "--artist", "Drake", "--title", "X"])
    assert a.artist == "Drake" and a.title == "X"


# ── command behavior ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_stores_every_row():
    store = _Store()
    n = await cmd_backfill(_Source([_rec("A", "x", 5), _rec("B", "y", 4, rank=2)]),
                           store, "hot100", as_of=TODAY)
    assert n == 2 and len(store.saved) == 2

@pytest.mark.asyncio
async def test_project_defaults_to_field_leader():
    recs = [_rec("Taylor Swift", "Fortnight", 30_000_000)] + \
           [_rec(f"F{i}", f"t{i}", 500_000, rank=2 + i) for i in range(9)]
    out = await cmd_project(_Source(recs), "hot100", as_of=TODAY)
    assert "Taylor Swift" in out["target"]

@pytest.mark.asyncio
async def test_project_empty_data_returns_none():
    assert await cmd_project(_Source([]), "hot100", as_of=TODAY) is None

@pytest.mark.asyncio
async def test_dry_run_edge_never_executes():
    recs = [_rec("Taylor Swift", "Fortnight", 30_000_000)] + \
           [_rec(f"F{i}", f"t{i}", 400_000, rank=2 + i) for i in range(9)]
    mkt = MarketCandidate(venue="polymarket", market_id="pm:1",
                          question="Will Taylor Swift be #1?", outcomes=["Yes", "No"],
                          prices=[0.30, 0.70], liquidity=5000.0,
                          close_time=datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc),
                          resolution_text="Hot 100")
    async def discover(): return [mkt]
    eng = MusicIntelEngine([_Source(recs)], discover, alert_sink=CollectingSink())
    sigs = await cmd_dry_run_edge(eng, "hot100", as_of=TODAY)
    assert eng.execution_enabled() is False
    assert len(sigs) == 1 and sigs[0].source == "chart-intel"

def test_backtest_no_weeks_returns_none():
    assert cmd_backtest([]) is None
