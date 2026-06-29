# Macro Nowcast σ-Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `maker_longshot` strategy a macro-knowledge gate so CPI/PCE/GDP NO bets are informed by free Federal Reserve nowcasts, mirroring the existing weather (NWS) and financial (Alpha Vantage) gates.

**Architecture:** New `core/macro_data.py` (pure parser + gate math + a cached `MacroNowcastClient`), wired into `MakerLongshotStrategy.scan()` at the same hook point as the financial gate, with a `directional.macro:` config block. The gate keeps a NO longshot only when the Kalshi bucket/threshold sits ≥ `min_sigma` standard deviations from the Fed nowcast; if the nowcast is unavailable it **skips** (never bets blind). Default disabled.

**Tech Stack:** Python 3.13, `httpx` (async), `dataclasses`, `pytest` (asyncio auto-mode). Mirrors `core/market_data.py`.

## Global Constraints

- Python 3.10+; `httpx` only for HTTP (no `requests`). Copied from project conventions.
- Dataclasses for internal models (not Pydantic).
- All new network calls async, wrapped so they NEVER raise into the maker loop (return `None` → gate skips). Copied from spec "Error handling".
- New config defaults to `enabled: false` — zero behaviour change until flipped. Copied from spec.
- Maker remains paper; no live-money path touched.
- Tests run with: `.venv-dev/bin/pytest <path> -q -p no:cacheprovider` and `export PYTHONPYCACHEPREFIX=/tmp/macro-pyc` (repo `__pycache__`/`.pytest_cache` are root-owned).
- Run all `git` commits exactly as written (repo is a git repo; deploy builds from working tree).

---

### Task 1: Macro ticker parser (`parse_macro_ticker` + `MacroMarket`)

Pure, no network. Foundation for the gate.

**Files:**
- Create: `core/macro_data.py`
- Test: `tests/test_macro_data.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class MacroMarket(series:str, indicator:str, threshold:float, direction:str, market_type:str, bucket_lo:Optional[float]=None, bucket_hi:Optional[float]=None)`
  - `parse_macro_ticker(ticker: str) -> Optional[MacroMarket]`
  - module constant `MACRO_SERIES: dict[str,str]` mapping Kalshi series → indicator key.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: core.macro_data`.

- [ ] **Step 3: Write minimal implementation**

```python
# core/macro_data.py
"""Macro nowcast knowledge gate for the directional maker.

Mirrors core/market_data.py (financial gate): a pure ticker parser + gate math
plus a cached MacroNowcastClient that pulls free Federal Reserve nowcasts
(Cleveland Fed CPI/PCE, Atlanta Fed GDPNow via FRED). Gate keeps a NO longshot
only when the Kalshi threshold/bucket is >= min_sigma away from the nowcast.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Kalshi series prefix -> indicator key. Order matters for longest-prefix match:
# KXCPI is a prefix of KXCPICORE/KXCPIYOY, so check the specific ones first.
MACRO_SERIES: dict[str, str] = {
    "KXCPICORE": "CPICORE",
    "KXCPIYOY": "CPIYOY",
    "KXPCECORE": "PCECORE",
    "KXGDP": "GDP",
    "KXCPI": "CPI",
}

_SUFFIX_RE = re.compile(r"-([TB])(-?\d+\.?\d*)$")


@dataclass(frozen=True)
class MacroMarket:
    series: str
    indicator: str
    threshold: float
    direction: str  # "above"
    market_type: str  # "threshold" | "bucket"
    bucket_lo: Optional[float] = None
    bucket_hi: Optional[float] = None


def parse_macro_ticker(ticker: str) -> Optional[MacroMarket]:
    """Parse KXCPI*/KXCPIYOY*/KXCPICORE*/KXPCECORE*/KXGDP* tickers. None otherwise."""
    if not ticker:
        return None
    series = next((s for s in MACRO_SERIES if ticker.startswith(s + "-")), None)
    if series is None:
        return None
    m = _SUFFIX_RE.search(ticker)
    if m is None:
        return None
    tb, val_str = m.groups()
    try:
        threshold = float(val_str)
    except ValueError:
        return None
    indicator = MACRO_SERIES[series]
    if tb == "B":
        return MacroMarket(series, indicator, threshold, "above", "bucket", bucket_lo=threshold)
    return MacroMarket(series, indicator, threshold, "above", "threshold")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider`
Expected: PASS (9 cases).

- [ ] **Step 5: Commit**

```bash
git add core/macro_data.py tests/test_macro_data.py
git commit -m "feat(macro): add parse_macro_ticker + MacroMarket"
```

---

### Task 2: Gate math (`macro_margin`, `macro_threshold_keep`, `macro_bucket_keep`)

Pure functions. Decide keep/skip given a nowcast and σ.

**Files:**
- Modify: `core/macro_data.py`
- Test: `tests/test_macro_data.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `macro_margin(nowcast: float, sigma: float, threshold: float) -> float` (z-score; `-inf` if `sigma<=0`).
  - `macro_threshold_keep(nowcast: float, sigma: float, threshold: float, min_sigma: float) -> bool`
  - `macro_bucket_keep(nowcast: float, lo: float, hi: float, sigma: float, min_sigma: float) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_macro_data.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider -k "margin or keep"`
Expected: FAIL — `ImportError: cannot import name 'macro_margin'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to core/macro_data.py

def macro_margin(nowcast: float, sigma: float, threshold: float) -> float:
    """z-score: how many σ the threshold sits above the nowcast.

    Positive z => threshold is ABOVE the nowcast (safe for NO on an 'above' market).
    Degenerate σ routes to SKIP (return -inf so z < any min_sigma).
    """
    if sigma <= 0:
        return float("-inf")
    return (threshold - nowcast) / sigma


def macro_threshold_keep(nowcast: float, sigma: float, threshold: float, min_sigma: float) -> bool:
    """KEEP a NO bet on an 'above-threshold' macro market when the threshold is
    >= min_sigma σ above the nowcast (i.e. the YES outcome is a deep tail)."""
    return macro_margin(nowcast, sigma, threshold) >= min_sigma


def macro_bucket_keep(nowcast: float, lo: float, hi: float, sigma: float, min_sigma: float) -> bool:
    """KEEP a NO bet on a bucket [lo,hi] when the nowcast is >= min_sigma σ OUTSIDE
    the bucket on either side (the bucket is a tail outcome). SKIP if σ<=0."""
    if sigma <= 0:
        return False
    margin = min_sigma * sigma
    return (nowcast <= lo - margin) or (nowcast >= hi + margin)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add core/macro_data.py tests/test_macro_data.py
git commit -m "feat(macro): add gate math (margin + threshold/bucket keep)"
```

---

### Task 3: `MacroNowcastClient` — cached Fed nowcast fetcher

**Files:**
- Modify: `core/macro_data.py`
- Test: `tests/test_macro_data.py`

**Interfaces:**
- Consumes: an async HTTP client (`httpx.AsyncClient`-compatible: `.get(url, params=...) -> resp` with `.json()`, `.raise_for_status()`).
- Produces: `class MacroNowcastClient(http, fred_api_key: Optional[str], ttl_s: int = 21600)` with `async nowcast(indicator: str) -> Optional[float]` (dispatches CPI/CPIYOY/CPICORE/PCECORE→Cleveland Fed, GDP→FRED `GDPNOW`). Returns `None` on any error/missing key.

**Endpoint note (resolve live in Step 0 before coding):**
- GDP: `https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW&api_key=<KEY>&file_type=json&sort_order=desc&limit=1` → `observations[0].value` (float, % annualized).
- CPI/PCE: Cleveland Fed inflation nowcast — confirm the downloadable data URL at https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting (look for a CSV/JSON export link in page source). If no clean machine endpoint exists, fall back to FRED nowcast series if available, else leave CPI/PCE returning `None` (gate then skips those — safe) and flag for follow-up. **Do not block the rest of the plan on this** — the parser/gate/wiring all work with whatever `nowcast()` returns.

- [ ] **Step 0: Verify endpoints live (manual, no code)**

```bash
# GDPNow via FRED (needs a free key from https://fredaccount.stlouisfed.org/apikeys)
curl -s "https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW&api_key=$FRED_API_KEY&file_type=json&sort_order=desc&limit=1" | python3 -m json.tool | head
# Cleveland Fed page — find the data export link
curl -s "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting" | grep -ioE 'href="[^"]*\.(csv|json|xlsx)"' | head
```
Record the working CPI/PCE data URL; use it in Step 3.

- [ ] **Step 1: Write the failing test** (mocked HTTP — no live network in CI)

```python
# append to tests/test_macro_data.py
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

@pytest.mark.asyncio
async def test_nowcast_is_cached(monkeypatch):
    http = MagicMock()
    http.get = AsyncMock(return_value=_resp({"observations": [{"value": "2.7"}]}))
    c = MacroNowcastClient(http=http, fred_api_key="k", ttl_s=9999)
    await c.nowcast("GDP"); await c.nowcast("GDP")
    assert http.get.call_count == 1  # second call served from cache
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider -k nowcast`
Expected: FAIL — `ImportError: cannot import name 'MacroNowcastClient'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to core/macro_data.py
import time

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
# Cleveland Fed CPI/PCE nowcast data URL — set from Step 0 verification.
_CLEVELAND_CPI_URL = ""   # TODO-IN-STEP-0: paste verified URL or leave "" to disable CPI/PCE
_CLEVELAND_PCE_URL = ""


class MacroNowcastClient:
    """Fetches free Fed nowcasts with a TTL cache. Never raises (returns None)."""

    def __init__(self, http: Any, fred_api_key: Optional[str], ttl_s: int = 21600) -> None:
        self._http = http
        self._fred_key = fred_api_key
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, Optional[float]]] = {}

    async def nowcast(self, indicator: str) -> Optional[float]:
        now = time.monotonic()
        hit = self._cache.get(indicator)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]
        if indicator == "GDP":
            val = await self._fred("GDPNOW")
        elif indicator in ("CPI", "CPIYOY", "CPICORE"):
            val = await self._cleveland(_CLEVELAND_CPI_URL, indicator)
        elif indicator == "PCECORE":
            val = await self._cleveland(_CLEVELAND_PCE_URL, indicator)
        else:
            val = None
        self._cache[indicator] = (now, val)
        return val

    async def _fred(self, series_id: str) -> Optional[float]:
        if not self._fred_key:
            return None
        try:
            resp = await self._http.get(_FRED_BASE, params={
                "series_id": series_id, "api_key": self._fred_key,
                "file_type": "json", "sort_order": "desc", "limit": 1,
            })
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            return float(obs[0]["value"]) if obs else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[macro] FRED %s error: %s", series_id, exc)
            return None

    async def _cleveland(self, url: str, indicator: str) -> Optional[float]:
        if not url:
            return None
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            return _parse_cleveland_nowcast(resp, indicator)  # implement per Step-0 format
        except Exception as exc:  # noqa: BLE001
            logger.warning("[macro] Cleveland %s error: %s", indicator, exc)
            return None


def _parse_cleveland_nowcast(resp: Any, indicator: str) -> Optional[float]:
    """Extract the latest nowcast for `indicator` from the Cleveland Fed export.
    Shape depends on the Step-0 verified format (CSV row / JSON field)."""
    return None  # replaced once Step-0 confirms the export shape
```

> Note for implementer: `_parse_cleveland_nowcast` and the two `_CLEVELAND_*_URL` constants are the ONLY things gated on the Step-0 live check. If the export is unavailable, ship them returning `None`/`""` — CPI/PCE candidates will safely skip (gate behaves as "no data"), and GDP still works. Add a follow-up note in the PR/commit.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py -q -p no:cacheprovider`
Expected: PASS (GDP path + cache + error paths green; Cleveland covered by Step-0 follow-up).

- [ ] **Step 5: Commit**

```bash
git add core/macro_data.py tests/test_macro_data.py
git commit -m "feat(macro): MacroNowcastClient (FRED GDPNow + Cleveland Fed cache)"
```

---

### Task 4: Config — `MacroCfg` dataclass + wiring + config.yaml + .env

**Files:**
- Modify: `utils/config_loader.py` (add `MacroCfg`, add to `DirectionalConfig`, build in `_build_directional`)
- Modify: `config.yaml` (add `directional.macro:` block)
- Modify: `.env.example` (document `FRED_API_KEY`)
- Test: `tests/directional/test_config.py`

**Interfaces:**
- Produces: `config.directional.macro` → `MacroCfg(enabled:bool, min_sigma:float, require_data:bool, horizon_days:int, fred_api_key_env:str, sigma:dict)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/directional/test_config.py
def test_macro_cfg_defaults_and_override(tmp_path):
    import yaml
    from utils.config_loader import load_config
    cfg = {
        "mode": {"trading_mode": "dry_run"},
        "directional": {"enabled": True, "macro": {"enabled": True, "min_sigma": 2.0,
                        "sigma": {"CPIYOY": 0.12, "GDP": 0.4}}},
    }
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump(cfg))
    c = load_config(str(p))
    assert c.directional.macro.enabled is True
    assert c.directional.macro.min_sigma == 2.0
    assert c.directional.macro.require_data is True          # default
    assert c.directional.macro.sigma["CPIYOY"] == 0.12

def test_macro_cfg_absent_defaults_disabled(tmp_path):
    import yaml
    from utils.config_loader import load_config
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump({"mode": {"trading_mode": "dry_run"}}))
    c = load_config(str(p))
    assert c.directional.macro.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/test_config.py -q -p no:cacheprovider -k macro`
Expected: FAIL — `AttributeError: 'DirectionalConfig' object has no attribute ... macro`.

- [ ] **Step 3: Write minimal implementation**

In `utils/config_loader.py`, after the `FinancialCfg` dataclass (line ~341) add:

```python
@dataclass
class MacroCfg:
    """Fed-nowcast gate config for Kalshi macro markets (CPI/PCE/GDP)."""
    enabled: bool = False
    min_sigma: float = 2.0
    require_data: bool = True
    horizon_days: int = 45
    fred_api_key_env: str = "FRED_API_KEY"
    sigma: dict = field(default_factory=lambda: {
        "CPI": 0.10, "CPIYOY": 0.12, "CPICORE": 0.10, "PCECORE": 0.10, "GDP": 0.40,
    })
```

In `DirectionalConfig` (line ~362) add the field after `pmus_weather`:

```python
    macro: MacroCfg = field(default_factory=MacroCfg)
```

In `_build_directional` (line ~538) add:

```python
    macro = _build_dataclass(MacroCfg, data.get("macro", {}) or {})
```
add `"macro"` to the `_sub` tuple (line ~539), and add `"macro": macro,` to the returned dict (line ~550).

In `config.yaml`, inside the `directional:` block (next to `financial:`), add:

```yaml
  # Macro nowcast gate (CPI/PCE/GDP) — keeps NO only when the bucket/threshold is
  # >= min_sigma σ from the Federal Reserve nowcast. Default OFF until validated.
  macro:
    enabled: false
    min_sigma: 2.0
    require_data: true        # skip (don't bet blind) when nowcast unavailable
    horizon_days: 45
    sigma:                    # per-indicator surprise σ (percentage points)
      CPI: 0.10
      CPIYOY: 0.12
      CPICORE: 0.10
      PCECORE: 0.10
      GDP: 0.40
```

In `.env.example`, add:

```bash
# Macro nowcast gate (GDPNow via FRED). Free key: https://fredaccount.stlouisfed.org/apikeys
FRED_API_KEY=
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/test_config.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/config_loader.py config.yaml .env.example tests/directional/test_config.py
git commit -m "feat(macro): MacroCfg config block + wiring"
```

---

### Task 5: Maker gate — `_apply_macro_gate` + scan dispatch

**Files:**
- Modify: `core/directional/strategies/maker_longshot.py`
- Test: `tests/directional/test_maker_macro_gate.py`

**Interfaces:**
- Consumes: `parse_macro_ticker`, `macro_threshold_keep`, `macro_bucket_keep` (Task 1-2); `ctx["macro"]` = a `MacroNowcastClient` or None; `self._macro` = `MacroCfg` or None (added to `__init__`).
- Produces: `async _apply_macro_gate(self, market, mm, delta_days, ctx) -> bool`; scan() drops macro candidates that fail it.

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/test_maker_macro_gate.py
import pytest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from core.directional.strategies.maker_longshot import MakerLongshotStrategy
from core.macro_data import MacroMarket

class _MacroCfg:
    enabled = True; min_sigma = 2.0; require_data = True; horizon_days = 45
    sigma = {"CPIYOY": 0.12}

class _FakeMacro:
    def __init__(self, val): self._val = val
    async def nowcast(self, indicator): return self._val

def _mk_market():
    m = SimpleNamespace()
    m.ticker = "KXCPIYOY-26JUN-T3.9"; m.title = "CPI YoY >= 3.9%"; m.category = "macro"
    m.yes_price = 0.06
    m.close_time = datetime.now(timezone.utc) + timedelta(days=10)
    m.to_unified_market_id = lambda: "kalshi:KXCPIYOY-26JUN-T3.9"
    return m

def _strategy():
    return MakerLongshotStrategy(
        min_structural_score=0.0, min_yes_price=0.0, max_yes_price=1.0,
        price_improvement_cents=1, max_days_to_resolution=45,
        macro_cfg=_MacroCfg(),
    )

@pytest.mark.asyncio
async def test_macro_gate_keeps_deep_tail():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    ctx = {"macro": _FakeMacro(3.2)}  # nowcast 3.2, thr 3.9, σ0.12 -> z≈5.8 -> KEEP
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, ctx) is True

@pytest.mark.asyncio
async def test_macro_gate_skips_near_threshold():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    ctx = {"macro": _FakeMacro(3.87)}  # z≈0.25 < 2.0 -> SKIP
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, ctx) is False

@pytest.mark.asyncio
async def test_macro_gate_skips_when_no_data_and_require():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, {"macro": _FakeMacro(None)}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/test_maker_macro_gate.py -q -p no:cacheprovider`
Expected: FAIL — `TypeError: __init__ got an unexpected keyword 'macro_cfg'`.

- [ ] **Step 3: Write minimal implementation**

In `maker_longshot.py` imports (line ~35) add:

```python
from core.macro_data import parse_macro_ticker, macro_threshold_keep, macro_bucket_keep
```

In `MakerLongshotStrategy.__init__` add a param `macro_cfg: Optional[Any] = None` (alongside `financial_cfg`) and store `self._macro = macro_cfg`.

Add the gate method (next to `_apply_financial_gate`, ~line 238):

```python
    async def _apply_macro_gate(self, market, mm, delta_days: float, ctx: dict) -> bool:
        """Return True to KEEP a macro NO candidate; False to SKIP. Mirrors the
        financial gate but uses a Fed nowcast + per-indicator σ."""
        cfg = self._macro
        client = ctx.get("macro")
        if delta_days > cfg.horizon_days:
            return not cfg.require_data
        if client is None:
            return not cfg.require_data
        nowcast = await client.nowcast(mm.indicator)
        if nowcast is None:
            keep = not cfg.require_data
            logger.debug("[macro-gate] %s: no nowcast, require_data=%s -> %s",
                         market.ticker, cfg.require_data, "KEEP" if keep else "SKIP")
            return keep
        sigma = float(cfg.sigma.get(mm.indicator, 0.0))
        if mm.market_type == "bucket" and mm.bucket_hi is not None:
            keep = macro_bucket_keep(nowcast, mm.bucket_lo, mm.bucket_hi, sigma, cfg.min_sigma)
        elif mm.market_type == "bucket":
            # single-sided bucket (lo only): treat like a threshold floor
            keep = macro_threshold_keep(nowcast, sigma, mm.bucket_lo, cfg.min_sigma)
        else:
            keep = macro_threshold_keep(nowcast, sigma, mm.threshold, cfg.min_sigma)
        logger.info("[macro-gate] %s: nowcast=%.3f thr=%.3f σ=%.3f min_sigma=%.1f -> %s",
                    market.ticker, nowcast, mm.threshold, sigma, cfg.min_sigma,
                    "KEEP" if keep else "SKIP")
        return keep
```

In `scan()`, right after the financial-gate block (line ~338), add:

```python
            # Macro nowcast gate — KXCPI*/KXCPIYOY*/KXCPICORE*/KXPCECORE*/KXGDP*
            if self._macro is not None and self._macro.enabled:
                mm = parse_macro_ticker(market.ticker)
                if mm is not None:
                    if not await self._apply_macro_gate(market, mm, delta_days, ctx):
                        continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/test_maker_macro_gate.py tests/directional/test_maker_longshot.py -q -p no:cacheprovider`
Expected: PASS (new gate tests + existing maker tests unaffected — `macro_cfg` defaults None → gate inert).

- [ ] **Step 5: Commit**

```bash
git add core/directional/strategies/maker_longshot.py tests/directional/test_maker_macro_gate.py
git commit -m "feat(macro): wire macro nowcast gate into maker_longshot.scan"
```

---

### Task 6: Engine wiring — build `MacroNowcastClient`, inject into ctx + strategy

**Files:**
- Modify: `core/directional/engine.py`
- Test: `tests/directional/test_engine.py`

**Interfaces:**
- Consumes: `MacroNowcastClient` (Task 3), `config.macro` (Task 4), `MakerLongshotStrategy(macro_cfg=...)` (Task 5).
- Produces: a process-lived `self._macro_client`; `ctx["macro"]` populated each cycle when macro enabled.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/directional/test_engine.py
def test_engine_builds_macro_client_when_enabled(monkeypatch):
    # Minimal: construct engine with macro.enabled and assert _ensure_macro_client sets a client.
    from core.directional.engine import DirectionalEngine
    from utils.config_loader import DirectionalConfig, MacroCfg
    cfg = DirectionalConfig(enabled=True)
    cfg.macro = MacroCfg(enabled=True)
    eng = DirectionalEngine(cfg, kalshi_client=None, intelligence_engine=None, risk_manager=None)
    monkeypatch.setenv("FRED_API_KEY", "k")
    eng._ensure_macro_client()
    assert eng._macro_client is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/test_engine.py -q -p no:cacheprovider -k macro`
Expected: FAIL — `AttributeError: ... _ensure_macro_client`.

- [ ] **Step 3: Write minimal implementation**

In `engine.py __init__` add `self._macro_client = None` (near `self._av_client`, line ~60) and `self._macro_cfg = getattr(config, "macro", None)` (near `self._financial_cfg`, line ~121).

Pass to the strategy (in the `MakerLongshotStrategy(...)` construction, ~line 116) add:
```python
                    macro_cfg=getattr(config, "macro", None),
```

Add the lazy builder (next to `_ensure_av_client`, ~line 169):
```python
    def _ensure_macro_client(self) -> None:
        if self._macro_client is not None:
            return
        if self._macro_cfg is None or not self._macro_cfg.enabled:
            return
        import os
        import httpx
        from core.macro_data import MacroNowcastClient
        key = os.environ.get(getattr(self._macro_cfg, "fred_api_key_env", "FRED_API_KEY"))
        self._macro_client = MacroNowcastClient(
            http=httpx.AsyncClient(timeout=10.0), fred_api_key=key,
        )
```

In `run_once`, where `ctx["av"]` is set (~line 233), add:
```python
        self._ensure_macro_client()
        if self._macro_client is not None:
            sc_ctx = {**sc_ctx, "macro": self._macro_client}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/test_engine.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/directional/engine.py tests/directional/test_engine.py
git commit -m "feat(macro): build + inject MacroNowcastClient in directional engine"
```

---

### Task 7: Live smoke + paper enablement

**Files:** none (operational). Validates end-to-end against live data.

- [ ] **Step 1: Full changed-area test sweep**

Run: `.venv-dev/bin/pytest tests/test_macro_data.py tests/directional/test_maker_macro_gate.py tests/directional/test_config.py tests/directional/test_engine.py tests/directional/test_maker_longshot.py -q -p no:cacheprovider`
Expected: ALL PASS.

- [ ] **Step 2: Live smoke (manual)** — with `FRED_API_KEY` exported, classify current Kalshi macro markets:

```bash
cd ~/docker/polymarket-arbitrage
set -a; . ./.env; set +a
export PYTHONPYCACHEPREFIX=/tmp/macro-pyc
.venv-dev/bin/python - <<'PY'
import asyncio, httpx
from kalshi_client.api import KalshiClient
from core.macro_data import MacroNowcastClient, parse_macro_ticker, macro_margin
async def main():
    async with httpx.AsyncClient(timeout=10) as h:
        mc = MacroNowcastClient(http=h, fred_api_key=__import__("os").environ.get("FRED_API_KEY"))
        print("GDP nowcast:", await mc.nowcast("GDP"))
        print("CPIYOY nowcast:", await mc.nowcast("CPIYOY"))
        async with KalshiClient(dry_run=True) as c:
            mks,_ = await c.list_markets(status="open", series_ticker="KXCPIYOY", limit=50)
            for m in mks[:8]:
                mm = parse_macro_ticker(m.ticker)
                if mm: print(m.ticker, "->", mm.indicator, mm.threshold)
asyncio.run(main())
PY
```
Expected: a real GDP nowcast float; CPIYOY nowcast float (or None if Cleveland endpoint still pending); macro tickers parse.

- [ ] **Step 3: Enable in paper + deploy**

Set `directional.macro.enabled: true` in `config.yaml`, add `FRED_API_KEY` to `.env`, then:
```bash
sudo -n docker compose build && sudo -n docker compose up -d
until curl -s --max-time 5 -o /dev/null -w "%{http_code}" http://localhost:8899/health | grep -q 200; do sleep 2; done
sudo -n docker compose logs --since 3m | grep -i "macro-gate" | head
```
Expected: `[macro-gate]` KEEP/SKIP lines on CPI/PCE/GDP candidates; no tracebacks. Watch the macro category verdict accumulate net-of-fees vs the prior blind bets.

- [ ] **Step 4: Commit enablement**

```bash
git add config.yaml
git commit -m "feat(macro): enable macro nowcast gate in paper"
```

---

## Self-Review

**Spec coverage:** data layer (Task 3) ✓; surprise σ (Task 4 config `sigma` + Task 2 math) ✓; gate logic T-type + bucket (Task 2, 5) ✓; config block + FRED_API_KEY (Task 4) ✓; engine wiring (Task 6) ✓; graceful degradation/skip-when-no-data (Task 2/3/5 tests) ✓; testing (every task) ✓; paper rollout + verdict watch (Task 7) ✓. FOMC/Sports/media explicitly out of scope ✓.

**Placeholder scan:** The only deferred item is the Cleveland Fed CPI/PCE export URL + `_parse_cleveland_nowcast`, isolated to Task 3 Step 0 with concrete verification commands and a safe fallback (returns None → gate skips → GDP still works). This is a genuine live-verification step, not a hidden TODO; flagged in the spec as the known risk.

**Type consistency:** `parse_macro_ticker → MacroMarket` (Task 1) consumed in Task 5; `macro_threshold_keep/macro_bucket_keep` signatures (Task 2) match Task 5 calls; `MacroNowcastClient.nowcast(indicator)` (Task 3) matches `ctx["macro"]` usage (Task 5) and engine injection (Task 6); `MacroCfg` fields (Task 4) match `self._macro` reads (Task 5) and engine `_macro_cfg` reads (Task 6). Consistent.
