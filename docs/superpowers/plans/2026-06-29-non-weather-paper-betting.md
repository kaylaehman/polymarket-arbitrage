# Non-Weather Paper Betting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paper (dry_run) bot place bets across crypto/forex, sports, macro, and entertainment/music — not just weather — by adding an edge that works on ~50/50 markets and unblocking the categories that are merely dormant or horizon-gated.

**Architecture:** The existing `MakerLongshotStrategy` only bets longshot-NO markets (YES ≤ ~0.15), which today is almost exclusively weather (other categories are ~50/50 or seasonally dormant). We add (B) a new `ConsensusDivergenceStrategy` that places paper bets when an independent knowledge gate (sports consensus odds, macro nowcast) diverges from the market's implied probability — this works on ~50/50 markets. We (A) unblock sports futures + add validated macro series via config, and (C) wire the alert-only `music_intel` signals into the paper directional store.

**Tech Stack:** Python 3.13 (3.12 in container), httpx async, dataclasses, sqlite3, pytest asyncio-auto. Reuses `core/sports_data.py` (`consensus_probs`, `match_team`), `core/macro_data.py` (`MacroNowcastClient`, `parse_macro_ticker`), `music_intel/` engine.

## Global Constraints

- **trading_mode stays `dry_run`** — paper only. NEVER set `live`. (Verbatim: `config.yaml:108 trading_mode: "dry_run"`.)
- All new strategies emit `DirectionalCandidate` objects through the existing `Decider`/`store` paper path — they must NOT call `execution.place_order` directly.
- Strategies implement the `Strategy` ABC: `name` property + `async scan(markets, ctx) -> list[DirectionalCandidate]`.
- Gate clients degrade to None gracefully; a strategy with no gate data returns `[]`, never raises.
- Per-category P&L already buckets via `core/directional/store.py::_category_of`; new categories must map there.
- Tests are fixture-based, no live network. Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest <path> -q -p no:cacheprovider`.

---

## File Structure

- `core/directional/strategies/consensus_divergence.py` — NEW. The ~50/50-market strategy (Phase B core).
- `tests/test_consensus_divergence.py` — NEW. Unit tests for the strategy.
- `core/directional/engine.py` — MODIFY. Register the new strategy; inject gate clients into `ctx`.
- `utils/config_loader.py` — MODIFY. Add `ConsensusDivergenceCfg` dataclass + parse block.
- `config.yaml` — MODIFY. Add `consensus_divergence:` block; widen sports-futures horizon; add macro series.
- `core/directional/strategies/music_paper.py` — NEW (Phase C). Adapter: music_intel signal → DirectionalCandidate.
- `tests/test_music_paper.py` — NEW (Phase C).
- `core/directional/store.py` — MODIFY (Phase C). Map `music`/`crypto`/`sports` tickers in `_category_of`.

---

## Phase A — Unblock dormant/horizon-gated categories (config only)

### Task A1: Raise sports-futures horizon + add validated macro series

**Files:**
- Modify: `config.yaml` (`directional.priority_series` block ~283-315, `max_days_to_resolution` ~315)

**Interfaces:**
- Produces: a `directional.priority_series_sports_max_days` config key consumed by the scanner in Task A2.

- [ ] **Step 1: Add longer-horizon sports series + extra macro series to `priority_series`**

In `config.yaml` under `directional.priority_series:`, after the existing sports block, add (these were confirmed open on Kalshi 2026-06-29):

```yaml
      # In-season per-game / series (daily-ish resolution; admitted by consensus_divergence, not longshot)
      - KXMLBGAME     # MLB per-game money line (84 open within 30d as of 2026-06-29)
      - KXMLB         # MLB series/division futures
      - KXWNBA        # WNBA (in season)
      # Macro extras validated as in-window monthly releases
      - KXU3          # Unemployment rate
      - KXPAYROLLS    # Nonfarm payrolls
```

- [ ] **Step 2: Add a sports-futures horizon override key**

In `config.yaml` under `directional:` (near `max_days_to_resolution: 30`), add:

```yaml
    # Sports CHAMPIONSHIP futures resolve far out; admit them up to this horizon so
    # they're scanned when a season is active (NBA/NHL playoffs). Non-futures keep the 30d cap.
    priority_series_sports_max_days: 220
```

- [ ] **Step 3: Verify config parses**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/python -c "from utils.config_loader import load_config; c=load_config(); print('series:', len(c.directional.priority_series)); print('sports_days:', getattr(c.directional,'priority_series_sports_max_days','MISSING'))"`
Expected: prints the new series count (≥ original+5) and `sports_days: 220` (after Task A2 adds the field; before A2 it prints `MISSING` — that's fine here).

- [ ] **Step 4: Commit**

```bash
git add config.yaml
git commit -m "feat(directional): add in-season sports + macro series, sports-futures horizon key (paper)"
```

### Task A2: Honor the sports-futures horizon in the scanner

**Files:**
- Modify: `core/directional/scanner.py` (priority-series fetch, the `_priority_series_max_days` filter ~290-300)
- Modify: `utils/config_loader.py` (`DirectionalCaps`/directional cfg dataclass — add `priority_series_sports_max_days: float = 30.0`)
- Test: `tests/test_scanner_sports_horizon.py` (NEW)

**Interfaces:**
- Consumes: `config.directional.priority_series_sports_max_days` (float, from A1).
- Produces: scanner admits series whose ticker starts with a sports-futures prefix (`KXNBA`,`KXNHL`,`KXMLBWS`) using the sports horizon; all other series use the default `_priority_series_max_days`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scanner_sports_horizon.py
import datetime
from core.directional.scanner import _within_horizon  # helper added in Step 3

def test_sports_future_admitted_under_long_horizon():
    close = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=120)
    # NBA championship future 120d out: rejected at 30d, admitted at 220d
    assert _within_horizon("KXNBA-27", close, default_days=30, sports_days=220) is True

def test_non_sports_keeps_default_horizon():
    close = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=120)
    assert _within_horizon("KXCPI-26JUL", close, default_days=30, sports_days=220) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_scanner_sports_horizon.py -q -p no:cacheprovider`
Expected: FAIL — `cannot import name '_within_horizon'`.

- [ ] **Step 3: Add the helper + use it in the priority-series filter**

In `core/directional/scanner.py`, add near the top-level helpers:

```python
_SPORTS_FUTURE_PREFIXES = ("KXNBA", "KXNHL", "KXMLBWS")

def _within_horizon(series_ticker, close_time, *, default_days, sports_days):
    """Sports championship futures get the longer horizon; everything else the default."""
    import datetime as _dt
    if close_time is None:
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    ct = close_time if close_time.tzinfo else close_time.replace(tzinfo=_dt.timezone.utc)
    days = (ct - now).total_seconds() / 86400.0
    if days < 0:
        return False
    limit = sports_days if series_ticker.upper().startswith(_SPORTS_FUTURE_PREFIXES) else default_days
    return days <= limit
```

Then in the priority-series loop (where it currently compares against `self._priority_series_max_days`), replace the inline day check with:

```python
            if not _within_horizon(series, m.close_time,
                                   default_days=self._priority_series_max_days,
                                   sports_days=self._priority_series_sports_max_days):
                continue
```

Add `self._priority_series_sports_max_days = priority_series_sports_max_days` in `__init__` and a constructor kwarg `priority_series_sports_max_days: float = 30.0`. In `utils/config_loader.py` add `priority_series_sports_max_days: float = 30.0` to the directional config dataclass, and pass it through where the scanner is constructed in `engine.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_scanner_sports_horizon.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add core/directional/scanner.py utils/config_loader.py core/directional/engine.py tests/test_scanner_sports_horizon.py
git commit -m "feat(directional): sports-futures get longer scan horizon (paper)"
```

---

## Phase B — ConsensusDivergence strategy (the real non-weather diversifier)

The thesis: on a ~50/50 market, an independent gate (sports book consensus, macro nowcast) gives probability `p_gate`. If the market's implied YES prob `p_mkt` diverges by more than `min_divergence` AND the gate is confident, place a PAPER bet on the cheaper side. Works where the longshot strategy can't.

### Task B1: Strategy skeleton + name + no-data safety

**Files:**
- Create: `core/directional/strategies/consensus_divergence.py`
- Test: `tests/test_consensus_divergence.py`

**Interfaces:**
- Produces:
  - `class ConsensusDivergenceStrategy(Strategy)` with
    `__init__(self, *, min_divergence: float, max_yes_price: float = 0.95, min_yes_price: float = 0.05, skip_categories: list[str], sports_cfg=None, macro_cfg=None)`
  - `name -> "consensus_divergence"`
  - `async scan(markets, ctx) -> list[DirectionalCandidate]`
  - module helper `def divergence_side(p_gate: float, p_mkt: float, min_divergence: float) -> tuple[str, float] | None`
    returning `("YES"|"NO", edge)` or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consensus_divergence.py
import pytest
from core.directional.strategies.consensus_divergence import (
    ConsensusDivergenceStrategy, divergence_side,
)

def test_divergence_side_yes_when_gate_higher():
    # gate says 0.40, market prices 0.20 -> YES underpriced by 0.20
    assert divergence_side(0.40, 0.20, 0.10) == ("YES", pytest.approx(0.20))

def test_divergence_side_no_when_gate_lower():
    # gate says 0.05, market prices 0.20 -> NO underpriced by 0.15
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
    # ctx without sports/macro clients -> nothing to compare -> []
    assert await s.scan([], {"no_ask": lambda t: None}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py -q -p no:cacheprovider`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# core/directional/strategies/consensus_divergence.py
"""ConsensusDivergence — paper directional bets on ~50/50 markets where an
independent knowledge gate (sports book consensus, macro nowcast) diverges from
the market's implied probability. Complements MakerLongshot (which only fires on
longshot-NO markets, ~weather-only). PAPER only — emits DirectionalCandidate.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy

logger = logging.getLogger(__name__)


def divergence_side(p_gate: float, p_mkt: float, min_divergence: float):
    """Return ("YES"|"NO", edge) if |p_gate - p_mkt| >= min_divergence, else None."""
    diff = p_gate - p_mkt
    if abs(diff) < min_divergence:
        return None
    return ("YES", diff) if diff > 0 else ("NO", -diff)


class ConsensusDivergenceStrategy(Strategy):
    def __init__(self, *, min_divergence: float, max_yes_price: float = 0.95,
                 min_yes_price: float = 0.05, skip_categories: list,
                 sports_cfg: Any = None, macro_cfg: Any = None) -> None:
        self._min_div = min_divergence
        self._max_yes = max_yes_price
        self._min_yes = min_yes_price
        self._skip = set(skip_categories or [])
        self._sports_cfg = sports_cfg
        self._macro_cfg = macro_cfg

    @property
    def name(self) -> str:
        return "consensus_divergence"

    async def scan(self, markets: list, ctx: dict) -> list:
        return []  # filled in B2/B3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py -q -p no:cacheprovider`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add core/directional/strategies/consensus_divergence.py tests/test_consensus_divergence.py
git commit -m "feat(directional): ConsensusDivergence strategy skeleton + divergence_side"
```

### Task B2: Sports divergence path

**Files:**
- Modify: `core/directional/strategies/consensus_divergence.py`
- Modify: `tests/test_consensus_divergence.py`

**Interfaces:**
- Consumes: `ctx["sports"]` — an object with `async championship_probs(ticker) -> dict[str,float]` (the `SportsOddsClient`, already injected for MakerLongshot); `core.sports_data.kalshi_series_to_odds`, `match_team`.
- Produces: sports markets emit a `DirectionalCandidate` when the de-vigged consensus prob diverges from `yes_mid`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py::test_scan_sports_emits_candidate_on_divergence -q -p no:cacheprovider`
Expected: FAIL — `scan` returns `[]`.

- [ ] **Step 3: Implement the sports path in `scan`**

Replace the `scan` body. For each market: skip if category in `self._skip`; compute `yes_mid = float(getattr(m,"yes_price",0) or 0)`; skip unless `self._min_yes <= yes_mid <= self._max_yes`. If `kalshi_series_to_odds(m.ticker)` is truthy and `ctx.get("sports")`: `probs = await ctx["sports"].championship_probs(m.ticker)`; `p_gate = match_team(getattr(m,"yes_sub_title","") or "", probs)`; if `p_gate is None`: continue. `res = divergence_side(p_gate, yes_mid, self._min_div)`; if None continue; else build a `DirectionalCandidate` (mirror the fields MakerLongshot sets — `market_id=m.to_unified_market_id()`, `side=res[0]`, `strategy=self.name`, an `edge=res[1]`, plus the price/size fields the Decider expects). Append. Wrap the gate call in try/except → continue on error.

Read `core/directional/strategies/maker_longshot.py` for the exact `DirectionalCandidate` construction it uses and mirror those field names.

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py -q -p no:cacheprovider`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add core/directional/strategies/consensus_divergence.py tests/test_consensus_divergence.py
git commit -m "feat(directional): ConsensusDivergence sports path (consensus vs market)"
```

### Task B3: Macro divergence path

**Files:**
- Modify: `core/directional/strategies/consensus_divergence.py`
- Modify: `tests/test_consensus_divergence.py`

**Interfaces:**
- Consumes: `ctx["macro"]` — `MacroNowcastClient` with `async nowcast(market: MacroMarket) -> tuple[float,float] | None` returning `(nowcast, sigma)`; `core.macro_data.parse_macro_ticker`, `macro_margin`. A normal CDF maps `(nowcast - threshold)/sigma` → `p_gate`.
- Produces: macro threshold markets emit a candidate when the nowcast-implied prob diverges from `yes_mid`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_scan_macro_emits_candidate_on_divergence():
    from types import SimpleNamespace
    import datetime
    m = SimpleNamespace(ticker="KXCPIYOY-26JUL-T3.0",
                        title="Will CPI YoY be above 3.0%?", yes_sub_title="", subtitle="",
                        category="Economics", yes_price=0.50,
                        close_time=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=20),
                        to_unified_market_id=lambda: "kalshi:KXCPIYOY-26JUL-T3.0")
    class _Macro:
        async def nowcast(self, mm): return (4.2, 0.2)  # nowcast 4.2% >> 3.0 threshold -> YES ~1
    s = ConsensusDivergenceStrategy(min_divergence=0.10, skip_categories=[])
    cands = await s.scan([m], {"no_ask": lambda t: 0.50, "macro": _Macro()})
    assert len(cands) == 1 and cands[0].side == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py::test_scan_macro_emits_candidate_on_divergence -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Add the macro branch to `scan`**

After the sports branch, add: if `parse_macro_ticker(m.ticker)` and `ctx.get("macro")`: `mm = parse_macro_ticker(m.ticker)`; `nc = await ctx["macro"].nowcast(mm)`; if `nc is None`: continue; `nowcast, sigma = nc`; if `sigma <= 0`: continue. Compute `p_gate` via the standard normal CDF of `(nowcast - mm.threshold)/sigma` using `0.5*(1+math.erf(z/2**0.5))` (threshold-above markets; for `below` markets use `1 - p_gate`). `res = divergence_side(p_gate, yes_mid, self._min_div)`; build candidate as in B2. Wrap in try/except.

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence.py -q -p no:cacheprovider`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add core/directional/strategies/consensus_divergence.py tests/test_consensus_divergence.py
git commit -m "feat(directional): ConsensusDivergence macro path (nowcast vs market)"
```

### Task B4: Config + engine registration

**Files:**
- Modify: `utils/config_loader.py` (add `ConsensusDivergenceCfg`)
- Modify: `config.yaml` (add `consensus_divergence:` block)
- Modify: `core/directional/engine.py` (register strategy; ensure `ctx["sports"]`/`ctx["macro"]` are injected per-cycle)
- Test: `tests/test_consensus_divergence_wiring.py` (NEW)

**Interfaces:**
- Consumes: `config.consensus_divergence` with fields `enabled: bool`, `min_divergence: float`, `max_yes_price: float`, `min_yes_price: float`, `skip_categories: list`.
- Produces: when `enabled`, the engine appends `ConsensusDivergenceStrategy` to `self._strategies` and the per-cycle `ctx` contains `sports`/`macro` clients.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consensus_divergence_wiring.py
from utils.config_loader import load_config

def test_consensus_divergence_config_parses():
    c = load_config()
    cd = getattr(c, "consensus_divergence", None)
    assert cd is not None
    assert hasattr(cd, "min_divergence") and hasattr(cd, "enabled")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence_wiring.py -q -p no:cacheprovider`
Expected: FAIL — `consensus_divergence` is None.

- [ ] **Step 3: Add config dataclass + yaml block + engine registration**

In `utils/config_loader.py`, mirror the `MacroCfg` pattern: add a `ConsensusDivergenceCfg` dataclass (`enabled: bool = True`, `min_divergence: float = 0.12`, `max_yes_price: float = 0.95`, `min_yes_price: float = 0.05`, `skip_categories: list = field(default_factory=list)`), build it with `_build_dataclass(ConsensusDivergenceCfg, data.get("consensus_divergence", {}) or {})`, and attach as `consensus_divergence` on the returned `BotConfig`.

In `config.yaml` add:

```yaml
# Consensus-divergence: paper directional bets on ~50/50 markets where a knowledge
# gate (sports book consensus / macro nowcast) disagrees with the market price.
# Complements maker_longshot (longshot-NO only). PAPER only.
consensus_divergence:
  enabled: true
  min_divergence: 0.12        # required |gate_prob - market_prob| to act
  max_yes_price: 0.95
  min_yes_price: 0.05
  skip_categories: []
```

In `core/directional/engine.py`, after the `maker_longshot` registration block, add:

```python
        cd_cfg = getattr(config, "consensus_divergence", None)
        if cd_cfg is not None and getattr(cd_cfg, "enabled", False):
            from core.directional.strategies.consensus_divergence import ConsensusDivergenceStrategy
            self._strategies.append(
                (ConsensusDivergenceStrategy(
                    min_divergence=cd_cfg.min_divergence,
                    max_yes_price=cd_cfg.max_yes_price,
                    min_yes_price=cd_cfg.min_yes_price,
                    skip_categories=list(getattr(cd_cfg, "skip_categories", [])),
                    sports_cfg=getattr(config, "sports", None),
                    macro_cfg=getattr(config, "macro", None),
                ), cd_cfg)
            )
```

Verify the per-cycle `ctx` already includes `sports`/`macro` (MakerLongshot uses them); if `run_once` builds `ctx` without them, add `ctx["sports"] = self._sports_client` and `ctx["macro"] = self._macro_client` where `ctx` is assembled.

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_consensus_divergence_wiring.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/config_loader.py config.yaml core/directional/engine.py tests/test_consensus_divergence_wiring.py
git commit -m "feat(directional): register ConsensusDivergence + config block (paper)"
```

---

## Phase C — Wire music_intel into the paper loop

### Task C1: Music → paper candidate adapter

**Files:**
- Create: `core/directional/strategies/music_paper.py`
- Test: `tests/test_music_paper.py`
- Modify: `core/directional/store.py` (`_category_of`: map music/crypto/sports tickers)

**Interfaces:**
- Consumes: `music_intel.engine.MusicIntelEngine.run_once(chart) -> RunResult` with `.signals: list[ChartSignal]` (fields `market_id`, `side`, `model_prob`, `market_prob`, `net_edge`, `target`).
- Produces: `class MusicPaperStrategy(Strategy)` (`name="music_paper"`) whose `scan` ignores the Kalshi `markets` list and instead runs the music engine, converting each `ChartSignal` to a `DirectionalCandidate` tagged `category="music"`. Still PAPER; still never executes (music engine `execution_enabled()` stays False).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_music_paper.py
import pytest
from core.directional.strategies.music_paper import MusicPaperStrategy

class _Sig:
    def __init__(self): 
        self.market_id="pm:1"; self.side="YES"; self.model_prob=0.8
        self.market_prob=0.3; self.net_edge=0.5; self.target="Artist - Song"; self.question="Q"
class _Res:
    signals=[_Sig()]
class _Eng:
    @staticmethod
    def execution_enabled(): return False
    async def run_once(self, chart, as_of=None): return _Res()

@pytest.mark.asyncio
async def test_music_paper_converts_signal_to_candidate():
    s = MusicPaperStrategy(engine=_Eng(), charts=["spotify_us_daily"])
    cands = await s.scan([], {"no_ask": lambda t: None})
    assert len(cands) == 1 and cands[0].side == "YES"
    assert cands[0].market_id == "pm:1"

@pytest.mark.asyncio
async def test_music_paper_refuses_if_execution_enabled():
    class _Bad(_Eng):
        @staticmethod
        def execution_enabled(): return True
    s = MusicPaperStrategy(engine=_Bad(), charts=["spotify_us_daily"])
    # safety guard: if something flipped execution on, music strategy emits nothing
    assert await s.scan([], {"no_ask": lambda t: None}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_music_paper.py -q -p no:cacheprovider`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the adapter**

```python
# core/directional/strategies/music_paper.py
"""Adapter: run the alert-only music_intel engine and convert its ChartSignals
into PAPER DirectionalCandidates (category="music"). Never executes."""
from __future__ import annotations

import logging
from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy

logger = logging.getLogger(__name__)


class MusicPaperStrategy(Strategy):
    def __init__(self, *, engine, charts: list) -> None:
        self._engine = engine
        self._charts = charts

    @property
    def name(self) -> str:
        return "music_paper"

    async def scan(self, markets: list, ctx: dict) -> list:
        # Hard safety: music module must stay alert-only. If execution ever got
        # flipped on, refuse to source paper candidates from it.
        if self._engine.execution_enabled():
            logger.warning("[music_paper] execution_enabled True -> emitting nothing")
            return []
        out = []
        for chart in self._charts:
            try:
                res = await self._engine.run_once(chart)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[music_paper] run_once(%s) failed: %s", chart, exc)
                continue
            for sig in res.signals:
                out.append(DirectionalCandidate(
                    market_id=sig.market_id, side=sig.side, strategy=self.name,
                    edge=sig.net_edge,
                ))
        return out
```

Adjust the `DirectionalCandidate(...)` kwargs to match the real dataclass fields (read `core/directional/models.py`); fill price/size fields the Decider needs the same way MakerLongshot does.

In `core/directional/store.py::_category_of`, add mappings so these bucket correctly: tickers starting `pm:` whose id came from music → `"music"`; also add `("KXBTC","crypto")`, `("KXETH","crypto")`, `("KXMLB","sports")`, `("KXNBA","sports")`, `("KXNHL","sports")`, `("KXWTI","commodity")`, `("KXEURUSD","forex")` to the prefix table.

- [ ] **Step 4: Run test to verify it passes**

Run: `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/test_music_paper.py -q -p no:cacheprovider`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add core/directional/strategies/music_paper.py tests/test_music_paper.py core/directional/store.py
git commit -m "feat(directional): music_intel -> paper candidate adapter (category=music, never executes)"
```

### Task C2: Register MusicPaper behind a config flag (default off)

**Files:**
- Modify: `utils/config_loader.py`, `config.yaml`, `core/directional/engine.py`

- [ ] **Step 1:** Add `music_paper: {enabled: false, charts: ["spotify_us_daily"]}` to `config.yaml` and a `MusicPaperCfg` dataclass; register in `engine.py` only when `enabled` (construct a `MusicIntelEngine` with kworb source + `discover_polymarket`, `store=None`, `alert_sink=None`). Default OFF so it's opt-in.
- [ ] **Step 2:** Run `export PYTHONPYCACHEPREFIX=/tmp/nwb-pyc && .venv-dev/bin/pytest tests/ -q -p no:cacheprovider -k "music or consensus or scanner_sports"` — expect all green.
- [ ] **Step 3:** Commit: `git commit -am "feat(directional): config-gated music_paper strategy (default off)"`

---

## Phase D — Verify in the deployed paper bot

- [ ] **Step 1:** Rebuild/restart the container: `cd ~/docker/polymarket-arbitrage && (docker compose up -d --build || sudo docker compose up -d --build)`.
- [ ] **Step 2:** After one scan cycle (~a few minutes), check new categories appear in the paper store:

```bash
.venv-dev/bin/python -c "
import sqlite3; from collections import Counter
c=sqlite3.connect('file:data/directional.db?mode=ro',uri=True)
def cat(m):
    t=m.upper()
    for k,v in [('KXHIGH','weather'),('KXCPI','macro'),('KXNBA','sports'),('KXNHL','sports'),('KXMLB','sports'),('KXBTC','crypto'),('KXETH','crypto'),('KXWTI','commodity'),('KXEURUSD','forex'),('PM:','music')]:
        if k in t: return v
    return 'other'
rows=c.execute('SELECT market_id, strategy FROM directional_signals').fetchall()
print('signals by strategy:', dict(Counter(s for _,s in rows)))
print('signals by category:', dict(Counter(cat(m) for m,_ in rows)))
"
```
Expected: `consensus_divergence` (and `music_paper` if enabled) appear under `signals by strategy`, and non-weather categories grow.

- [ ] **Step 2 (note):** Sports futures only materialize when a season is active (NBA/NHL playoffs) — until then sports volume comes from in-season per-game/series via consensus_divergence. Log this expectation; do not treat low immediate sports volume as a bug.

---

## Self-Review notes

- **Spec coverage:** crypto/forex (B macro+sports cover macro/sports; crypto/forex ~50/50 markets are admitted by consensus_divergence only where a gate exists — crypto has NO free directional gate, so crypto coverage comes from bundle/multi-outcome arb already enabled, NOT this plan. CALL OUT to user: a crypto-specific directional gate is out of scope/needs a data source). Sports ✓ (B2 + A). Macro ✓ (B3 + A). Music ✓ (C).
- **Honest limitation:** crypto/forex/commodity get diversification only via the *existing* arb strategies (bundle/multi-outcome), not a new directional edge — there's no free probabilistic gate for them analogous to sports odds / macro nowcast. Flag this to the user before building.
- **No live trading anywhere** — every new path emits paper DirectionalCandidates; music guard refuses if execution flips on.
