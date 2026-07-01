# Climate Markets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the directional paper bot to trade more Kalshi `Climate and Weather`
families (hourly temperature now; low-temp/precip/monthly/tornado as follow-ons) via a
pluggable provider that emits a calibrated `P(YES)` feeding one edge layer that does
both longshot-NO and directional bets.

**Architecture:** A `core/directional/climate/` module: each family is a
`ClimateProvider` with `match(market)` → `ParsedClimate` and `async probability(...)`
→ `ClimateSignal(p_yes, confidence)`. A `registry` finds the provider for a market;
`edge.py` turns `(p_yes, price)` into `DirectionalCandidate`s; a `climate_paper`
`Strategy` drives it. Strikes are read from the market's structured
`strike_type`/`floor_strike`/`cap_strike` fields (no per-series regex). All paper.

**Tech Stack:** Python 3.12, httpx (async), dataclasses, pytest (asyncio-auto). NWS
forecast via existing `core/weather.py`. No new runtime deps.

## Global Constraints

- **PAPER ONLY.** Every candidate is placed with `mode="paper"`. `ClimateCfg` MUST
  define `mode: str = "paper"` (a strategy cfg without `mode` crashes `run_once` —
  see commit `3d1a504`).
- **Never crash the cycle.** Every `provider.probability()` and the strategy `scan`
  must return/skip on any error, never raise (settlement runs in the same loop).
- **Test command:** `export PYTHONPYCACHEPREFIX=/tmp/clim && .venv-dev/bin/pytest <path> -q -p no:cacheprovider` (repo `__pycache__` is root-owned).
- **Disabled by default.** Every family ships behind a config flag defaulting to off.
- **Reuse, don't duplicate:** sizing/placement via the existing `decider`/`executor`;
  settlement via the existing `tracker` `kalshi:`/`pmus:` paths.

## File Structure

- Create `core/directional/climate/__init__.py` — package marker.
- Create `core/directional/climate/base.py` — `ParsedClimate`, `ClimateSignal`,
  `ClimateProvider` ABC, strike→interval + Gaussian-interval probability helpers.
- Create `core/directional/climate/edge.py` — `make_candidates(...)`.
- Create `core/directional/climate/registry.py` — `ClimateRegistry`.
- Create `core/directional/climate/providers/__init__.py`
- Create `core/directional/climate/providers/high_temp.py` — daily-high provider.
- Create `core/directional/climate/providers/hourly_temp.py` — hourly-temp provider.
- Create `core/directional/strategies/climate_paper.py` — the driving `Strategy`.
- Modify `utils/config_loader.py` — add `ClimateCfg` + wire into `DirectionalCfg`.
- Modify `core/directional/engine.py` — register `climate_paper`; ctx branch.
- Modify `core/directional/tracker.py` — calibration log on settlement.
- Create tests under `tests/directional/climate/`.

---

### Task 1: Ensure `KalshiMarket` carries strike fields

**Files:**
- Modify: `kalshi_client/models.py` (KalshiMarket dataclass)
- Modify: `kalshi_client/api.py` (the `get_market`/list parser, ~line 50)
- Test: `tests/test_kalshi_strike_fields.py`

**Interfaces:**
- Produces: `KalshiMarket.strike_type: Optional[str]`, `.floor_strike: Optional[float]`, `.cap_strike: Optional[float]` populated from the API JSON.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kalshi_strike_fields.py
from kalshi_client.models import KalshiMarket
def test_kalshi_market_has_strike_fields():
    m = KalshiMarket(ticker="KXHIGHNY-26JUL01-B98.5", event_ticker="KXHIGHNY",
                     series_ticker="KXHIGHNY", title="t", subtitle="",
                     yes_price=0.5, no_price=0.5, status="active", result=None,
                     volume=0, open_interest=0, close_time=None, category="Climate and Weather",
                     strike_type="between", floor_strike=98.0, cap_strike=99.0)
    assert m.strike_type == "between" and m.floor_strike == 98.0 and m.cap_strike == 99.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `export PYTHONPYCACHEPREFIX=/tmp/clim && .venv-dev/bin/pytest tests/test_kalshi_strike_fields.py -q -p no:cacheprovider`
Expected: FAIL — `KalshiMarket` has no `strike_type` (TypeError on unexpected kwarg).

- [ ] **Step 3: Add the fields**

In `kalshi_client/models.py`, add to the `KalshiMarket` dataclass (with defaults so existing call sites are unaffected):

```python
    strike_type: Optional[str] = None     # "greater" | "less" | "between"
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
```

In `kalshi_client/api.py` where the market JSON is parsed into `KalshiMarket` (the `return KalshiMarket(...)` near line 50), add:

```python
        strike_type=data.get("strike_type"),
        floor_strike=(float(data["floor_strike"]) if data.get("floor_strike") is not None else None),
        cap_strike=(float(data["cap_strike"]) if data.get("cap_strike") is not None else None),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_kalshi_strike_fields.py -q -p no:cacheprovider`
Expected: PASS. Also run `tests/test_polymarket_us.py tests/directional/test_tracker.py -q -p no:cacheprovider` — still pass (additive fields).

- [ ] **Step 5: Commit**

```bash
git add kalshi_client/models.py kalshi_client/api.py tests/test_kalshi_strike_fields.py
git commit -m "feat(kalshi): carry strike_type/floor_strike/cap_strike on KalshiMarket"
```

---

### Task 2: `climate/base.py` — types + probability math

**Files:**
- Create: `core/directional/climate/__init__.py` (empty)
- Create: `core/directional/climate/base.py`
- Test: `tests/directional/climate/test_base.py`

**Interfaces:**
- Produces:
  - `@dataclass ParsedClimate(family:str, market_id:str, series:str, geo:str, target:str, strike_type:str, lo:float|None, hi:float|None, kind:str)`
  - `@dataclass ClimateSignal(p_yes:float, confidence:float, source:str, drivers:list)`
  - `class ClimateProvider(ABC)` with `match(market)->ParsedClimate|None` and `async probability(parsed, http, ctx)->ClimateSignal|None`
  - `interval_from_market(strike_type, floor, cap) -> tuple[float|None,float|None]` (lo,hi)
  - `gaussian_interval_prob(lo, hi, mean, sigma) -> float` — P(lo < X <= hi) for X~N(mean,sigma); open-ended side when lo/hi is None.

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/climate/test_base.py
import math, pytest
from core.directional.climate.base import gaussian_interval_prob, interval_from_market

def test_interval_from_market():
    assert interval_from_market("greater", 99.0, None) == (99.0, None)
    assert interval_from_market("less", None, 92.0) == (None, 92.0)
    assert interval_from_market("between", 98.0, 99.0) == (98.0, 99.0)

def test_gaussian_interval_prob_bucket():
    # mean exactly in a tight bucket -> ~the mass within +-0.5 sigma-ish
    p = gaussian_interval_prob(98.0, 99.0, mean=98.5, sigma=3.0)
    assert 0.10 < p < 0.20

def test_gaussian_interval_prob_open_upper():
    # P(X > 99) with mean 90, sigma 3 -> ~0 (far tail)
    assert gaussian_interval_prob(99.0, None, mean=90.0, sigma=3.0) < 0.01
    # P(X > 90) with mean 90 -> ~0.5
    assert abs(gaussian_interval_prob(90.0, None, mean=90.0, sigma=3.0) - 0.5) < 0.02

def test_gaussian_interval_prob_open_lower():
    # P(X <= 92) with mean 90 -> > 0.5
    assert gaussian_interval_prob(None, 92.0, mean=90.0, sigma=3.0) > 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_base.py -q -p no:cacheprovider`
Expected: FAIL — module `core.directional.climate.base` does not exist.

- [ ] **Step 3: Write `base.py`**

```python
"""Climate provider framework: parsed-market + signal types, the provider ABC,
and the forecast→probability math. Pure (no I/O) except provider.probability()."""
from __future__ import annotations
import abc, math
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ParsedClimate:
    family: str            # "high_temp" | "hourly_temp" | ...
    market_id: str         # "kalshi:<ticker>"
    series: str            # e.g. "KXTEMPNYCH"
    geo: str               # station/series key for the forecast source (e.g. "nyc")
    target: str            # ISO date or "YYYY-MM-DDTHH" for hourly / "YYYY-MM" monthly
    strike_type: str       # "greater" | "less" | "between"
    lo: Optional[float]    # interval lower bound (None = open)
    hi: Optional[float]    # interval upper bound (None = open)
    kind: str              # "temp" | "count" | "precip"


@dataclass
class ClimateSignal:
    p_yes: float
    confidence: float
    source: str
    drivers: list = field(default_factory=list)


def interval_from_market(strike_type: Optional[str], floor: Optional[float],
                         cap: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Map Kalshi (strike_type, floor_strike, cap_strike) -> (lo, hi) for the YES region."""
    st = (strike_type or "").lower()
    if st == "greater":
        return (floor, None)
    if st == "less":
        return (None, cap)
    if st == "between":
        return (floor, cap)
    return (floor, cap)  # best-effort fallback


def _norm_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def gaussian_interval_prob(lo: Optional[float], hi: Optional[float],
                           mean: float, sigma: float) -> float:
    """P(lo < X <= hi) for X ~ Normal(mean, sigma). None bound = open on that side."""
    p_hi = _norm_cdf(hi, mean, sigma) if hi is not None else 1.0
    p_lo = _norm_cdf(lo, mean, sigma) if lo is not None else 0.0
    return max(0.0, min(1.0, p_hi - p_lo))


class ClimateProvider(abc.ABC):
    family: str = "climate"

    @abc.abstractmethod
    def match(self, market: Any) -> Optional[ParsedClimate]:
        """Return ParsedClimate if this provider handles the market, else None."""

    @abc.abstractmethod
    async def probability(self, parsed: ParsedClimate, http: Any,
                          ctx: dict) -> Optional[ClimateSignal]:
        """Return calibrated P(YES) signal, or None to skip. Must never raise."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_base.py -q -p no:cacheprovider`
Expected: PASS (4 tests). Create `tests/directional/climate/__init__.py` (empty) if collection complains.

- [ ] **Step 5: Commit**

```bash
git add core/directional/climate/__init__.py core/directional/climate/base.py tests/directional/climate/
git commit -m "feat(climate): provider ABC + forecast-interval probability math"
```

---

### Task 3: `climate/edge.py` — probability → candidates

**Files:**
- Create: `core/directional/climate/edge.py`
- Test: `tests/directional/climate/test_edge.py`

**Interfaces:**
- Consumes: `ParsedClimate`, `ClimateSignal` (Task 2); `DirectionalCandidate` (`core.directional.models`).
- Produces: `make_candidates(parsed, market_price, signal, *, longshot_floor=0.05, min_edge=0.10, strategy="climate_paper") -> list[DirectionalCandidate]`

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/climate/test_edge.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_edge.py -q -p no:cacheprovider`
Expected: FAIL — `edge` module missing.

- [ ] **Step 3: Write `edge.py`**

```python
"""Turn a calibrated P(YES) + market price into 0..2 DirectionalCandidates:
longshot-NO (very-unlikely tail) and/or directional (model vs price divergence)."""
from __future__ import annotations
from typing import List
from core.directional.models import DirectionalCandidate
from core.directional.climate.base import ParsedClimate, ClimateSignal


def make_candidates(parsed: ParsedClimate, market_price: float, signal: ClimateSignal,
                    *, longshot_floor: float = 0.05, min_edge: float = 0.10,
                    strategy: str = "climate_paper") -> List[DirectionalCandidate]:
    p = signal.p_yes
    out: dict[str, DirectionalCandidate] = {}   # side -> candidate (dedups)

    def add(side: str, reasoning: str):
        if side in out:
            return
        out[side] = DirectionalCandidate(
            market_id=parsed.market_id, title=parsed.series, category="Climate and Weather",
            side=side, market_price=market_price, ai_probability=p,
            confidence=signal.confidence, edge=(p - market_price), strategy=strategy,
            reasoning=reasoning,
        )

    # Directional: model diverges from price by >= min_edge.
    if p - market_price >= min_edge:
        add("YES", f"model p={p:.2f} > price {market_price:.2f} ({signal.source})")
    elif market_price - p >= min_edge:
        add("NO", f"model p={p:.2f} < price {market_price:.2f} ({signal.source})")

    # Longshot-NO: YES is very unlikely.
    if p <= longshot_floor:
        add("NO", f"longshot: p(YES)={p:.3f} <= {longshot_floor}")

    return list(out.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_edge.py -q -p no:cacheprovider`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add core/directional/climate/edge.py tests/directional/climate/test_edge.py
git commit -m "feat(climate): edge layer — p_yes+price -> longshot-NO/directional candidates"
```

---

### Task 4: `ClimateCfg` + `registry` + `climate_paper` strategy + engine wiring (disabled)

**Files:**
- Modify: `utils/config_loader.py` (add `ClimateCfg`; add field to `DirectionalCfg`)
- Create: `core/directional/climate/registry.py`
- Create: `core/directional/strategies/climate_paper.py`
- Modify: `core/directional/engine.py` (register strategy; ctx branch)
- Test: `tests/directional/climate/test_registry.py`, `tests/directional/test_climate_paper.py`

**Interfaces:**
- Consumes: `ClimateProvider` (Task 2), `make_candidates` (Task 3), `Strategy` (`core.directional.strategies.base`), `DirectionalCandidate`.
- Produces:
  - `ClimateCfg(enabled:bool=False, mode:str="paper", longshot_floor:float=0.05, min_edge:float=0.10, hourly_temp_enabled:bool=False, high_temp_enabled:bool=False)`
  - `ClimateRegistry(providers:list).match(market) -> tuple[ClimateProvider, ParsedClimate] | None`
  - `ClimatePaperStrategy(registry, cfg)` with `name=="climate_paper"` and `async scan(markets, ctx)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/directional/climate/test_registry.py
from core.directional.climate.registry import ClimateRegistry
from core.directional.climate.base import ClimateProvider, ParsedClimate
class _Stub(ClimateProvider):
    family="stub"
    def match(self, m):
        return ParsedClimate("stub","kalshi:X","X","nyc","2026-07-01","greater",1.0,None,"temp") if getattr(m,"ticker","")=="X" else None
    async def probability(self, parsed, http, ctx): return None
def test_registry_matches_first_provider():
    reg = ClimateRegistry([_Stub()])
    m = type("M",(),{"ticker":"X"})()
    assert reg.match(m)[1].series == "X"
    assert reg.match(type("M",(),{"ticker":"Y"})()) is None
```

```python
# tests/directional/test_climate_paper.py
import pytest
from core.directional.strategies.climate_paper import ClimatePaperStrategy
from core.directional.climate.registry import ClimateRegistry
from core.directional.climate.base import ClimateProvider, ParsedClimate, ClimateSignal

class _Prov(ClimateProvider):
    family="t"
    def match(self, m):
        return ParsedClimate("t","kalshi:"+m.ticker,m.ticker,"nyc","2026-07-01","greater",99.0,None,"temp")
    async def probability(self, parsed, http, ctx):
        return ClimateSignal(0.02, 0.9, "nws")   # very-low -> longshot NO

class _Cfg:
    enabled=True; mode="paper"; longshot_floor=0.05; min_edge=0.10

@pytest.mark.asyncio
async def test_climate_paper_emits_candidate():
    strat = ClimatePaperStrategy(ClimateRegistry([_Prov()]), _Cfg())
    mkt = type("M",(),{"ticker":"KXHIGHNY-26JUL01-T99","yes_price":0.12,"no_price":0.88})()
    out = await strat.scan([mkt], {"http": None})
    assert len(out) == 1 and out[0].side == "NO" and out[0].strategy == "climate_paper"

@pytest.mark.asyncio
async def test_climate_paper_disabled_returns_empty():
    cfg = _Cfg(); cfg.enabled = False
    strat = ClimatePaperStrategy(ClimateRegistry([_Prov()]), cfg)
    assert await strat.scan([type("M",(),{"ticker":"X","yes_price":0.1})()], {}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_registry.py tests/directional/test_climate_paper.py -q -p no:cacheprovider`
Expected: FAIL — `registry`/`climate_paper` modules missing.

- [ ] **Step 3a: Write `registry.py`**

```python
from __future__ import annotations
from typing import Any, List, Optional, Tuple
from core.directional.climate.base import ClimateProvider, ParsedClimate


class ClimateRegistry:
    def __init__(self, providers: List[ClimateProvider]):
        self._providers = providers

    def match(self, market: Any) -> Optional[Tuple[ClimateProvider, ParsedClimate]]:
        for p in self._providers:
            try:
                parsed = p.match(market)
            except Exception:
                parsed = None
            if parsed is not None:
                return (p, parsed)
        return None
```

- [ ] **Step 3b: Write `climate_paper.py`**

```python
"""climate_paper strategy: for each liquid market, find a climate provider, get a
calibrated P(YES), and emit longshot-NO / directional candidates. PAPER only.
Never raises into the engine cycle."""
from __future__ import annotations
import logging
from typing import Any, List
from core.directional.strategies.base import Strategy
from core.directional.models import DirectionalCandidate
from core.directional.climate.edge import make_candidates

logger = logging.getLogger(__name__)


class ClimatePaperStrategy(Strategy):
    def __init__(self, registry, cfg):
        self._registry = registry
        self._cfg = cfg

    @property
    def name(self) -> str:
        return "climate_paper"

    async def scan(self, markets: List[Any], ctx: dict) -> List[DirectionalCandidate]:
        if not getattr(self._cfg, "enabled", False):
            return []
        http = ctx.get("http")
        out: List[DirectionalCandidate] = []
        for m in markets:
            try:
                hit = self._registry.match(m)
                if hit is None:
                    continue
                provider, parsed = hit
                signal = await provider.probability(parsed, http, ctx)
                if signal is None:
                    continue
                yes_price = float(getattr(m, "yes_price", 0.0) or 0.0)
                out.extend(make_candidates(
                    parsed, yes_price, signal,
                    longshot_floor=self._cfg.longshot_floor,
                    min_edge=self._cfg.min_edge,
                ))
            except Exception as exc:  # never break the cycle
                logger.warning("[climate_paper] %s error: %s", getattr(m, "ticker", "?"), exc)
        return out
```

- [ ] **Step 3c: Add `ClimateCfg` to `utils/config_loader.py`**

Add (near `ConsensusDivergenceCfg`):

```python
@dataclass
class ClimateCfg:
    """Climate-markets directional strategy (Kalshi 'Climate and Weather'). PAPER only."""
    enabled: bool = False
    mode: str = "paper"          # placed under this; never live
    longshot_floor: float = 0.05
    min_edge: float = 0.10
    high_temp_enabled: bool = False
    hourly_temp_enabled: bool = False
```

Add to the `DirectionalCfg` dataclass: `climate: ClimateCfg = field(default_factory=ClimateCfg)`
and in the loader function (near the other `_build_dataclass(...)` calls):
`climate = _build_dataclass(ClimateCfg, data.get("climate", {}) or {})`, passing
`climate=climate` into the constructed `DirectionalCfg`.

- [ ] **Step 3d: Wire into `core/directional/engine.py`**

After the other `self._strategies.append((...))` blocks, add:

```python
        clim_cfg = getattr(config, "climate", None)
        if clim_cfg is not None and clim_cfg.enabled:
            from core.directional.climate.registry import ClimateRegistry
            from core.directional.climate.providers.high_temp import HighTempProvider
            from core.directional.climate.providers.hourly_temp import HourlyTempProvider
            providers = []
            if clim_cfg.high_temp_enabled:
                providers.append(HighTempProvider())
            if clim_cfg.hourly_temp_enabled:
                providers.append(HourlyTempProvider())
            from core.directional.strategies.climate_paper import ClimatePaperStrategy
            self._strategies.append((ClimatePaperStrategy(ClimateRegistry(providers), clim_cfg), clim_cfg))
```

In the `scan` dispatch (the `if strategy.name == ...` ladder near line 381), add a branch:

```python
            elif strategy.name == "climate_paper":
                ctx = sc_ctx
                strategy_markets = maker_markets
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_registry.py tests/directional/test_climate_paper.py tests/directional/test_engine.py -q -p no:cacheprovider`
Expected: PASS (engine tests still green — climate disabled by default).

- [ ] **Step 5: Commit**

```bash
git add core/directional/climate/registry.py core/directional/strategies/climate_paper.py utils/config_loader.py core/directional/engine.py tests/directional/climate/test_registry.py tests/directional/test_climate_paper.py
git commit -m "feat(climate): registry + climate_paper strategy + engine wiring (disabled)"
```

---

### Task 5: `high_temp` provider (wrap known-good daily-high)

**Files:**
- Create: `core/directional/climate/providers/__init__.py` (empty)
- Create: `core/directional/climate/providers/high_temp.py`
- Test: `tests/directional/climate/test_high_temp.py`

**Interfaces:**
- Consumes: `ClimateProvider`, `ParsedClimate`, `ClimateSignal`, `interval_from_market`, `gaussian_interval_prob` (Task 2); `core.weather.forecast_high`, `core.weather.STATIONS`/`KXHIGH` series→city map.
- Produces: `HighTempProvider()` — matches `kalshi:KXHIGH*` (and `KXHOUHIGH`/`KX*HIGH` variants present in discovery), `probability` integrates N(forecast_high, σ) over the market interval.

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/climate/test_high_temp.py
import pytest
from unittest.mock import AsyncMock, patch
from core.directional.climate.providers.high_temp import HighTempProvider

def _mkt(ticker, st, floor, cap):
    return type("M",(),{"ticker":ticker,"strike_type":st,"floor_strike":floor,
                        "cap_strike":cap,"yes_price":0.1,"category":"Climate and Weather"})()

def test_match_high_temp_bucket():
    p = HighTempProvider().match(_mkt("KXHIGHNY-26JUL01-B98.5","between",98.0,99.0))
    assert p is not None and p.family=="high_temp" and p.lo==98.0 and p.hi==99.0
    assert p.target=="2026-07-01"

def test_match_rejects_non_high():
    assert HighTempProvider().match(_mkt("KXTORNADO-26JUN-425","greater",425.0,None)) is None

@pytest.mark.asyncio
async def test_probability_far_above_forecast_is_low():
    prov = HighTempProvider()
    parsed = prov.match(_mkt("KXHIGHNY-26JUL01-T99","greater",99.0,None))
    with patch("core.directional.climate.providers.high_temp.forecast_high",
               new=AsyncMock(return_value=88.0)):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is not None and sig.p_yes < 0.01   # P(high>99) when forecast 88
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_high_temp.py -q -p no:cacheprovider`
Expected: FAIL — provider missing.

- [ ] **Step 3: Write `high_temp.py`**

> Implementer note: confirm the exact city-extraction helper in `core/weather.py`
> (the existing `parse_*` for `KXHIGH*` and its series→station map). Reuse it for
> `geo`; do not re-implement the station table. `forecast_high(series, date, http)`
> returns the forecast °F or None.

```python
from __future__ import annotations
import re
from datetime import datetime
from typing import Any, Optional
from core.directional.climate.base import (
    ClimateProvider, ParsedClimate, ClimateSignal,
    interval_from_market, gaussian_interval_prob,
)
from core.weather import forecast_high  # existing NWS daily-high forecaster

_SIGMA_F = 3.5   # NWS next-day high-temp forecast error (°F); widen via calibration
# KXHIGH<CITY> and <CITY>HIGH / KX<CITY>HIGH variants seen in discovery
_TICKER = re.compile(r"^(KX)?([A-Z]+)?HIGH([A-Z]*)-(\d{2}[A-Z]{3}\d{2})-")

def _parse_date(yymmmdd: str) -> str:
    return datetime.strptime(yymmmdd, "%y%b%d").strftime("%Y-%m-%d")

class HighTempProvider(ClimateProvider):
    family = "high_temp"

    def match(self, market: Any) -> Optional[ParsedClimate]:
        ticker = getattr(market, "ticker", "")
        if "HIGH" not in ticker:
            return None
        m = _TICKER.match(ticker)
        if not m:
            return None
        series = ticker.split("-", 1)[0]
        try:
            date_iso = _parse_date(m.group(4))
        except ValueError:
            return None
        lo, hi = interval_from_market(getattr(market, "strike_type", None),
                                      getattr(market, "floor_strike", None),
                                      getattr(market, "cap_strike", None))
        return ParsedClimate("high_temp", "kalshi:" + ticker, series,
                             series, date_iso, getattr(market, "strike_type", "") or "",
                             lo, hi, "temp")

    async def probability(self, parsed: ParsedClimate, http: Any, ctx: dict) -> Optional[ClimateSignal]:
        try:
            fc = await forecast_high(parsed.series, parsed.target, http=http)
        except Exception:
            return None
        if fc is None:
            return None
        p = gaussian_interval_prob(parsed.lo, parsed.hi, mean=float(fc), sigma=_SIGMA_F)
        return ClimateSignal(p_yes=p, confidence=0.7, source="nws-high",
                             drivers=[("forecast_high", float(fc)), ("sigma", _SIGMA_F)])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_high_temp.py -q -p no:cacheprovider`
Expected: PASS (3 tests). If `forecast_high`'s real signature differs (e.g. takes a
city key not the series), adapt the call and the `geo` field; the test mocks it so the
math is validated regardless.

- [ ] **Step 5: Commit**

```bash
git add core/directional/climate/providers/__init__.py core/directional/climate/providers/high_temp.py tests/directional/climate/test_high_temp.py
git commit -m "feat(climate): high_temp provider (Gaussian over forecast; validates framework)"
```

---

### Task 6: `hourly_temp` provider (`KXTEMP<CITY>H`, real open markets)

**Files:**
- Create: `core/directional/climate/providers/hourly_temp.py`
- Test: `tests/directional/climate/test_hourly_temp.py`

**Interfaces:**
- Consumes: Task 2 helpers; an NWS **hourly** forecaster. If `core/weather.py` has no
  hourly fetch, add `async def forecast_hour(series, iso_hour, http) -> float|None` to
  `core/weather.py` reusing the existing gridpoint lookup (NWS
  `/gridpoints/{office}/{x},{y}/forecast/hourly`), and import it here.
- Produces: `HourlyTempProvider()` — matches `kalshi:KXTEMP<CITY>H-YYMMMDDHH-...`,
  parses date+hour, integrates N(hourly_forecast, σ) over the interval.

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/climate/test_hourly_temp.py
import pytest
from unittest.mock import AsyncMock, patch
from core.directional.climate.providers.hourly_temp import HourlyTempProvider

def _mkt(ticker, st, floor, cap):
    return type("M",(),{"ticker":ticker,"strike_type":st,"floor_strike":floor,"cap_strike":cap,"yes_price":0.1})()

def test_match_parses_date_and_hour():
    p = HourlyTempProvider().match(_mkt("KXTEMPNYCH-26JUN3017-T92.99","greater",92.99,None))
    assert p is not None and p.family=="hourly_temp"
    assert p.target=="2026-06-30T17" and p.geo=="KXTEMPNYCH" and p.lo==92.99

@pytest.mark.asyncio
async def test_probability_uses_hourly_forecast():
    prov = HourlyTempProvider()
    parsed = prov.match(_mkt("KXTEMPNYCH-26JUN3017-T92.99","greater",92.99,None))
    with patch("core.directional.climate.providers.hourly_temp.forecast_hour",
               new=AsyncMock(return_value=85.0)):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is not None and sig.p_yes < 0.02   # P(temp>92.99) when hourly fc 85
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_hourly_temp.py -q -p no:cacheprovider`
Expected: FAIL — provider missing.

- [ ] **Step 3: Write `hourly_temp.py`** (and `forecast_hour` in `core/weather.py` if absent)

```python
from __future__ import annotations
import re
from datetime import datetime
from typing import Any, Optional
from core.directional.climate.base import (
    ClimateProvider, ParsedClimate, ClimateSignal,
    interval_from_market, gaussian_interval_prob,
)
from core.weather import forecast_hour   # add to core/weather.py if missing

_SIGMA_F = 2.5   # hourly temp forecast error (°F); calibrate
# KXTEMP<CITY>H-YYMMMDDHH-...  (date is YYMMMDD, then 2-digit hour)
_TICKER = re.compile(r"^(KXTEMP[A-Z]+H)-(\d{2}[A-Z]{3}\d{2})(\d{2})-")

class HourlyTempProvider(ClimateProvider):
    family = "hourly_temp"

    def match(self, market: Any) -> Optional[ParsedClimate]:
        ticker = getattr(market, "ticker", "")
        m = _TICKER.match(ticker)
        if not m:
            return None
        try:
            date_iso = datetime.strptime(m.group(2), "%y%b%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
        target = f"{date_iso}T{m.group(3)}"
        lo, hi = interval_from_market(getattr(market, "strike_type", None),
                                      getattr(market, "floor_strike", None),
                                      getattr(market, "cap_strike", None))
        return ParsedClimate("hourly_temp", "kalshi:" + ticker, m.group(1),
                             m.group(1), target, getattr(market, "strike_type", "") or "",
                             lo, hi, "temp")

    async def probability(self, parsed: ParsedClimate, http: Any, ctx: dict) -> Optional[ClimateSignal]:
        try:
            fc = await forecast_hour(parsed.series, parsed.target, http=http)
        except Exception:
            return None
        if fc is None:
            return None
        p = gaussian_interval_prob(parsed.lo, parsed.hi, mean=float(fc), sigma=_SIGMA_F)
        return ClimateSignal(p_yes=p, confidence=0.7, source="nws-hourly",
                             drivers=[("forecast_hour", float(fc)), ("sigma", _SIGMA_F)])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/climate/test_hourly_temp.py -q -p no:cacheprovider`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add core/directional/climate/providers/hourly_temp.py core/weather.py tests/directional/climate/test_hourly_temp.py
git commit -m "feat(climate): hourly_temp provider + NWS hourly forecaster"
```

---

### Task 7: Calibration logging on settlement

**Files:**
- Modify: `core/directional/store.py` (add `climate_calibration` table + `record_calibration`)
- Modify: `core/directional/tracker.py` (log prediction vs outcome when a climate position settles)
- Test: `tests/directional/test_climate_calibration.py`

**Interfaces:**
- Consumes: store + tracker resolution paths (existing).
- Produces: `DirectionalStore.record_calibration(market_id, strategy, p_yes, outcome_yes:int)`; rows queryable for the future reliability report.

- [ ] **Step 1: Write the failing test**

```python
# tests/directional/test_climate_calibration.py
from core.directional.store import DirectionalStore
def test_record_and_read_calibration(tmp_path):
    s = DirectionalStore(str(tmp_path/"d.db")); s.init_schema()
    s.record_calibration("kalshi:KXTEMPNYCH-26JUN3017-T92.99", "climate_paper", 0.02, 0)
    rows = s._conn.execute("SELECT strategy,p_yes,outcome_yes FROM climate_calibration").fetchall()
    assert len(rows) == 1 and rows[0]["strategy"] == "climate_paper"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/directional/test_climate_calibration.py -q -p no:cacheprovider`
Expected: FAIL — `record_calibration` / table missing.

- [ ] **Step 3: Implement**

In `core/directional/store.py` schema init, add:

```sql
CREATE TABLE IF NOT EXISTS climate_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT, strategy TEXT, p_yes REAL, outcome_yes INTEGER,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

and method:

```python
    def record_calibration(self, market_id, strategy, p_yes, outcome_yes):
        self._conn.execute(
            "INSERT INTO climate_calibration(market_id,strategy,p_yes,outcome_yes) VALUES(?,?,?,?)",
            (market_id, strategy, float(p_yes), int(outcome_yes)),
        )
        self._conn.commit()
```

In `core/directional/tracker.py`, in each resolution method (`_check_resolution` kalshi
path, `_check_pmus_resolution`), after `update_position(... status="closed" ...)`, when
`pos.strategy == "climate_paper"`, compute `outcome_yes = 1 if (market result == "yes")
else 0` and call `self._store.record_calibration(pos.market_id, pos.strategy,
predicted_p, outcome_yes)`. The predicted `p_yes` is the candidate's `ai_probability`
stored at placement — read it from the position row (add it to the stored signal if not
present). Wrap in try/except (never block settlement).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/directional/test_climate_calibration.py tests/directional/test_store.py tests/directional/test_tracker.py -q -p no:cacheprovider`
Expected: PASS (settlement tests still green).

- [ ] **Step 5: Commit**

```bash
git add core/directional/store.py core/directional/tracker.py tests/directional/test_climate_calibration.py
git commit -m "feat(climate): log predicted-vs-outcome on settlement for calibration"
```

---

## Follow-on (separate plan — same provider interface, mechanical)

These reuse Tasks 2–4 unchanged; each is one provider file + tests + a config flag:
- **low_temp** (`KXLOW*`/`KXLOWT*`): NWS **min**-temp forecaster (mirror Task 5).
- **precip** (`KXRAIND*`): NWS PoP → `p_yes` directly (no Gaussian); `KXRAINHOLIDAY`
  multi-outcome handled separately if its encoding differs.
- **tornado** (`KXTORNADO`): SPC national monthly climatology + MTD count → Poisson
  tail `P(total > N)`. New `core/climate_sources/spc.py`.
- **monthly_anomaly** (`KXHMONTHRANGE`): NOAA normals + MTD mean projection. New
  `core/climate_sources/noaa_normals.py`.
- **reliability report** in `core/directional/validation.py`: bin `climate_calibration`
  rows (predicted-P band vs actual frequency) per family; surface in the digest.

## Self-Review

- **Spec coverage:** §3 architecture → Tasks 2–4. §4 high/hourly models → Tasks 5–6
  (low/precip/Tier-2 → Follow-on). §6 paper-safety/`ClimateCfg.mode` → Task 4. §7
  error handling → try/except in Tasks 4–6. §8 calibration → Task 7 (report in
  Follow-on). §9 testing → every task is TDD. §11 strike fields → Task 1.
- **Placeholder scan:** none — every code step has complete code. The two
  implementer notes (forecast_high city key in Task 5; tracker predicted-p source in
  Task 7) are verification pointers with the fallback specified, not missing code.
- **Type consistency:** `ParsedClimate`/`ClimateSignal`/`make_candidates`/
  `ClimateProvider` signatures match across Tasks 2→3→4→5→6. `DirectionalCandidate`
  fields match `core/directional/models.py` (verified). `ClimateCfg.mode` present
  (Global Constraints).
