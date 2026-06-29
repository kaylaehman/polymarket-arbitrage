"""
Configuration Loader
=====================

Loads and validates configuration from YAML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(Exception):
    """Configuration error."""
    pass


@dataclass
class ApiConfig:
    """API configuration."""
    polymarket_rest_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    kalshi_api_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    private_key: str = ""

    # Polymarket wallet type for CLOB signing:
    #   0 = EOA (you hold the private key directly) -- default
    #   1 = email/magic (Polymarket-hosted) proxy wallet
    #   2 = browser (MetaMask) proxy wallet
    # For proxy types (1/2), funder = the wallet address that holds USDC.
    signature_type: int = 0
    funder: str = ""

    # Kalshi trading credentials (RSA key pair created in the Kalshi web UI).
    # Market-data reads need no auth; placing/cancelling orders does.
    kalshi_api_key_id: str = ""      # the key UUID shown in Kalshi settings
    kalshi_private_key: str = ""     # RSA private key PEM (full text, or path via env)

    # Polymarket.US (Ed25519 signed REST API)
    polymarket_us_key_id: str = ""
    polymarket_us_secret_key: str = ""
    polymarket_us_rest_url: str = "https://api.polymarket.us"
    polymarket_us_gateway_url: str = "https://gateway.polymarket.us"

    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class TradingConfig:
    """Trading configuration."""
    markets: list[str] = field(default_factory=list)
    min_edge: float = 0.01
    bundle_arb_enabled: bool = True
    min_spread: float = 0.05
    tick_size: float = 0.01
    mm_enabled: bool = True
    default_order_size: float = 50.0
    min_order_size: float = 5.0
    max_order_size: float = 200.0
    slippage_tolerance: float = 0.02
    order_timeout_seconds: float = 60.0
    # Kelly criterion sizing (FEAT-05) — disabled until signal data validates calibration
    kelly_enabled: bool = False
    kelly_fraction: float = 0.25       # quarter-Kelly for reduced variance
    kelly_max_fraction: float = 0.10   # never risk >10% of bankroll on one market
    # Time-decay edge discounting (FEAT-07)
    time_decay_enabled: bool = False
    skip_if_resolves_within_hours: float = 12.0
    # Max $ committed across BOTH legs of one cross-platform arb trade.
    cross_platform_max_trade_notional: float = 15.0


@dataclass
class RiskConfig:
    """Risk configuration."""
    max_position_per_market: float = 200.0
    max_global_exposure: float = 5000.0
    max_daily_loss: float = 500.0
    max_drawdown_pct: float = 0.10
    trade_only_high_volume: bool = True
    min_24h_volume: float = 10000.0
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    kill_switch_enabled: bool = True
    auto_unwind_on_breach: bool = False


@dataclass
class ModeConfig:
    """Trading mode configuration."""
    trading_mode: str = "dry_run"  # "live" or "dry_run"
    data_mode: str = "real"  # "real" or "simulation" - use simulation for demos
    cross_platform_enabled: bool = True  # Enable cross-platform arbitrage (Polymarket + Kalshi)
    kalshi_enabled: bool = True  # Enable Kalshi market monitoring
    min_match_similarity: float = 0.6  # Minimum similarity score for market matching (0-1)
    # Hard gate: actually PLACE cross-platform orders (vs detect-and-alert only).
    # Even in live mode this stays off until you explicitly enable it.
    cross_platform_execution_enabled: bool = False

    # Kalshi-only operation (for users who can trade Kalshi but not Polymarket):
    # kalshi_native_enabled runs single-venue bundle-arb detection/trading on
    # Kalshi order books (riskless; YES+NO < $1). kalshi_oracle_enabled also takes
    # the DIRECTIONAL Kalshi leg of a cross-platform gap using Polymarket as a
    # price oracle (NOT riskless — carries event risk). Both simulate in dry_run
    # and only place real orders in live mode.
    kalshi_native_enabled: bool = False
    kalshi_oracle_enabled: bool = False
    # Multi-outcome mutually-exclusive Kalshi arb (riskless underround across an
    # event's brackets: buy 1 YES on every leg when sum(yes_ask)+fees < $1).
    # DETECT-ONLY + alert today — surfaces locks to the dashboard/Discord but
    # does NOT place orders (safe multi-leg placement needs all-or-nothing fill
    # handling). Default off; harmless no-op when disabled.
    kalshi_multi_outcome_enabled: bool = False
    # Polymarket.US venue gate (disabled by default)
    polymarket_us_enabled: bool = False
    dry_run_initial_balance: float = 10000.0
    simulate_fills: bool = True
    fill_probability: float = 0.8


@dataclass
class LoggingConfig:
    """Logging configuration."""
    console_level: str = "INFO"
    file_level: str = "DEBUG"
    log_dir: str = "logs"
    main_log_file: str = "bot.log"
    trades_log_file: str = "trades.log"
    opportunities_log_file: str = "opportunities.log"
    max_log_size_mb: int = 50
    backup_count: int = 5


@dataclass
class MonitoringConfig:
    """Monitoring configuration."""
    snapshot_interval: float = 60.0
    heartbeat_interval: float = 30.0
    track_latency: bool = True
    track_fill_rates: bool = True
    # Cross-platform arb monitoring loop
    cross_platform_poll_seconds: float = 30.0   # seconds between full sweeps of matched pairs
    cross_platform_max_pairs: int = 60          # cap on matched pairs polled (rate-limit guard)
    # Kalshi-native bundle-arb loop
    kalshi_poll_seconds: float = 10.0           # seconds between sweeps of watched Kalshi markets
    kalshi_max_markets: int = 100               # cap on Kalshi markets watched (by volume)
    # Kalshi WebSocket real-time feed (Task WS-1)
    kalshi_ws_enabled: bool = True              # use WS feed when available; REST sweep stays as fallback
    ws_staleness_seconds: float = 10.0          # treat WS book as stale if no update for this many seconds
    ws_reconcile_seconds: float = 120.0         # full REST reconcile interval while WS is running


@dataclass
class IntelligenceNewsConfig:
    """News-fetching sub-config for the intelligence layer."""
    lookback_hours: int = 4
    max_articles: int = 5
    cache_ttl_minutes: int = 10
    sources: list = field(default_factory=list)


@dataclass
class IntelligenceClaudeConfig:
    """Claude sub-config for the intelligence layer."""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 512
    timeout_seconds: int = 8


@dataclass
class IntelligenceConfig:
    """AI news-intelligence layer config. Disabled by default (additive)."""
    enabled: bool = False
    mode: str = "filter"            # "filter" | "boost" | "both"
    min_confidence: float = 0.65
    min_edge_boost: float = 0.03
    min_edge_filter: float = 0.10   # adverse gap size before an arb is filtered
    max_position_boost: float = 10.0
    news: IntelligenceNewsConfig = field(default_factory=IntelligenceNewsConfig)
    claude: IntelligenceClaudeConfig = field(default_factory=IntelligenceClaudeConfig)


@dataclass
class DatabaseConfig:
    """Signal database (SQLite) config. Append-only persistence (FEAT-09)."""
    enabled: bool = False
    path: str = "data/signals.db"
    log_signals: bool = True
    log_opportunities: bool = True
    auto_log_outcomes: bool = True   # poll for resolutions (background poller TBD)


@dataclass
class AgentConfig:
    """External agent control API (e.g. OpenClaw). Token via AGENT_API_TOKEN env."""
    enabled: bool = False           # mount the /api/agent control surface
    allow_control: bool = False     # allow pause/kill (read-only when false)


# ── Directional Trading Config ────────────────────────────────────────────────

@dataclass
class DirectionalCaps:
    """Position caps for the directional trading engine."""
    total_exposure: float = 30.0   # max total $ across all open directional positions
    max_position: float = 8.0      # max $ per individual position
    max_open: int = 4              # max simultaneous open positions (global count cap)
    # Count cap for LONGSHOT (non-daily, e.g. macro CPI/cable) open positions so
    # slow multi-week bets don't crowd out fast daily ones (weather). Daily
    # (weather) bets are exempt from this cap. None/negative = no longshot cap.
    max_open_longshot: int = 4


@dataclass
class SafeCompounderCfg:
    """Config for the pure-math Safe Compounder strategy."""
    mode: str = "paper"            # "paper" | "live"
    min_edge_cents: float = 3.0    # minimum edge in cents to emit a candidate
    skip_categories: list = field(default_factory=list)


@dataclass
class AiDirectionalCfg:
    """Config for the AI-directional strategy."""
    mode: str = "paper"            # "paper" | "live"
    min_confidence: float = 0.65
    min_edge_pct: float = 0.05
    kelly_fraction: float = 0.25
    stop_loss_pct: float = 0.30    # fraction of entry price below which to close
    take_profit_pct: float = 0.50  # fraction of entry price above which to close
    max_hold_hours: float = 72.0
    # Efficiency filter: skip markets too far out or in wrong category.
    max_days_to_resolution: float = 45.0  # skip markets closing more than N days out
    categories: list = field(default_factory=list)  # empty = no category restriction


@dataclass
class MakerLongshotCfg:
    """Config for the Maker Longshot (NO-bias resting limit) strategy."""
    mode: str = "paper"                 # "paper" | "live"
    min_structural_score: float = 0.02  # minimum structural_score to emit a candidate
    min_yes_price: float = 0.05         # floor on yes_mid -> skip NO > 0.95 (avoid fat-tail extremes)
    max_yes_price: float = 0.20         # longshot filter: skip if yes_mid > this; NO band [0.80, 0.95]
    price_improvement_cents: int = 1    # cents below no_ask to post the resting limit
    order_ttl_minutes: float = 60.0     # cancel unfilled live orders older than this
    skip_categories: list = field(default_factory=list)
    max_days_to_resolution: float = 30.0  # near-term: only post on longshots resolving within N days


@dataclass
class DirectionalScannerCfg:
    """Scanner sub-config for the directional trading engine.

    priority_series: backtest-validated series to enumerate directly via the
    series-scoped API endpoint, in addition to the /events universe.  The
    /events endpoint is dominated by KXMV parlays and long-dated politics; these
    series are where the 115-trade backtest found ~83 % of longshot edge.

    Set to an empty list to disable priority-series augmentation entirely.
    """
    # Weather city series confirmed to exist on Kalshi (verified against live API).
    # Macro series validated in the 115-trade backtest.
    priority_series: list = field(default_factory=lambda: [
        # Daily-high temperature markets — KXHIGHNY dominant edge source
        "KXHIGHNY",       # New York
        "KXHIGHCHI",      # Chicago
        "KXHIGHLAX",      # Los Angeles
        "KXHIGHMIA",      # Miami
        # Macro / economic release series — backtest-validated
        "KXCPI",          # US CPI month-on-month
        "KXCPIYOY",       # US CPI year-on-year
        "KXCPICORE",      # US Core CPI
        "KXPCECORE",      # US Core PCE
        "KXGDP",          # US GDP
        "KXFEDDECISION",  # FOMC rate decision
    ])
    # Maximum days to resolution for priority-series markets.  Mirrors
    # maker_longshot.max_days_to_resolution; markets closing beyond this are
    # excluded at fetch time (saves probes and prevents far-out wasted positions).
    max_days_to_resolution: float = 30.0


@dataclass
class WeatherCfg:
    """Forecast-gate config for KXHIGH* weather markets in MakerLongshotStrategy.

    When enabled, NO bets on T-type weather markets are gated by the NWS
    forecast high fetched from api.weather.gov (keyless).  Only markets with
    is_above_threshold=True (YES wins if hot) are affected; all other candidates
    pass through unchanged.

    safe_margin_f: min degrees the forecast must be BELOW the threshold for a
        NO bet to be kept.  E.g. threshold=85, forecast=79, safe_margin_f=4.0
        => margin=-6.0 <= -4.0 => KEEP.  If margin > -safe_margin_f => SKIP.
    forecast_horizon_days: only gate markets closing within this many days.
        Beyond the NWS horizon (~7 days) the gate cannot fire.
    require_forecast: if True (default) and forecast is unavailable (NWS error,
        beyond horizon), skip the weather candidate rather than betting blind.
        If False, fall back to the structural NO bet when forecast unavailable.
    """
    enabled: bool = True
    safe_margin_f: float = 4.0
    forecast_horizon_days: int = 7
    require_forecast: bool = True




@dataclass
class PMUSWeatherCfg:
    """Config for the PM.US weather market source in the directional maker."""
    enabled: bool = True          # flip off via directional.pmus_weather.enabled: false
    max_days: float = 30.0        # only fetch markets resolving within this many days
    cache_ttl_seconds: float = 300.0  # PM.US fetch cache TTL

@dataclass
class FinancialCfg:
    """Alpha Vantage gate config for Kalshi financial markets in MakerLongshotStrategy."""
    enabled: bool = True
    min_sigma: float = 2.5
    price_ttl_minutes: int = 240
    vol_ttl_hours: int = 24
    horizon_days: int = 14
    require_data: bool = True
    max_calls_per_day: int = 20
    max_price_age_days: int = 3
    underlyings: dict = field(default_factory=lambda: {
        "BTC": "CURRENCY_EXCHANGE_RATE",
        "ETH": "CURRENCY_EXCHANGE_RATE",
        "WTI": "WTI",
        "EURUSD": "CURRENCY_EXCHANGE_RATE",
    })


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


@dataclass
class SportsCfg:
    """The Odds API consensus gate for Kalshi championship-futures (KXNBA/MLB/NHL/NFL).

    Keeps a NO longshot only when the de-vigged bookmaker consensus win probability
    is <= max_prob. require_data: skip (don't bet blind) when consensus unavailable
    or the team match is ambiguous. Free tier = 500 credits/month; the client caps
    daily calls + caches to stay well under.
    """
    enabled: bool = False
    max_prob: float = 0.10
    require_data: bool = True
    cache_ttl_hours: float = 12.0
    max_calls_per_day: int = 12
    odds_api_key_env: str = "ODDS_API_KEY"


@dataclass
class DirectionalConfig:
    """Directional trading mode config. Disabled by default (additive)."""
    enabled: bool = False
    db_path: str = "data/directional.db"
    # Fix 3: defaults match config.yaml tuning (300s interval, 15 markets).
    scan_interval_seconds: int = 300
    markets_per_cycle: int = 15
    # M1 FIX: explicit field so engine.py reads it properly (not via getattr fallback).
    min_volume: int = 100
    category_exclude: list = field(default_factory=list)
    caps: DirectionalCaps = field(default_factory=DirectionalCaps)
    safe_compounder: SafeCompounderCfg = field(default_factory=SafeCompounderCfg)
    ai_directional: AiDirectionalCfg = field(default_factory=AiDirectionalCfg)
    maker_longshot: MakerLongshotCfg = field(default_factory=MakerLongshotCfg)
    scanner: DirectionalScannerCfg = field(default_factory=DirectionalScannerCfg)
    weather: WeatherCfg = field(default_factory=WeatherCfg)
    financial: FinancialCfg = field(default_factory=FinancialCfg)
    pmus_weather: PMUSWeatherCfg = field(default_factory=PMUSWeatherCfg)
    macro: MacroCfg = field(default_factory=MacroCfg)
    sports: SportsCfg = field(default_factory=SportsCfg)


@dataclass
class AlertsConfig:
    """Push-notification alerts config. Disabled by default (additive).

    Secrets (webhook URL / tokens) are read from environment variables at
    startup — never put them in config.yaml.
    """
    enabled: bool = False
    cooldown_seconds: float = 60.0
    min_severity: str = "info"


@dataclass
class CatalystConfig:
    """Catalyst-calendar targeting config. Disabled by default (additive)."""
    enabled: bool = False
    window_hours: float = 72.0
    # User-editable list of upcoming catalysts. Each entry: {name, date (ISO str), keywords: [...]}
    calendar: list = field(default_factory=list)


@dataclass
class BotConfig:
    """Complete bot configuration."""
    api: ApiConfig = field(default_factory=ApiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    intelligence: IntelligenceConfig = field(default_factory=IntelligenceConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    directional: DirectionalConfig = field(default_factory=DirectionalConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    catalyst: CatalystConfig = field(default_factory=CatalystConfig)
    
    @property
    def is_dry_run(self) -> bool:
        return self.mode.trading_mode.lower() == "dry_run"
    
    @property
    def is_live(self) -> bool:
        return self.mode.trading_mode.lower() == "live"
    
    @property
    def use_simulation(self) -> bool:
        """Use simulated data (for demos/screenshots)."""
        return self.mode.data_mode.lower() == "simulation"


def load_config(config_path: str = "config.yaml") -> BotConfig:
    """
    Load configuration from a YAML file.
    
    Args:
        config_path: Path to the configuration file
        
    Returns:
        BotConfig instance with loaded values
        
    Raises:
        ConfigError: If the config file cannot be loaded or is invalid
    """
    path = Path(config_path)
    
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    
    try:
        with open(path, "r") as f:
            raw_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in config file: {e}")
    
    if raw_config is None:
        raw_config = {}
    
    # Parse sections
    api_data = raw_config.get("api", {})
    trading_data = raw_config.get("trading", {})
    risk_data = raw_config.get("risk", {})
    mode_data = raw_config.get("mode", {})
    logging_data = raw_config.get("logging", {})
    monitoring_data = raw_config.get("monitoring", {})
    intelligence_data = raw_config.get("intelligence", {}) or {}
    database_data = raw_config.get("database", {}) or {}
    
    # Handle environment variable overrides
    api_data = _apply_env_overrides(api_data, {
        "api_key": "POLYMARKET_API_KEY",
        "api_secret": "POLYMARKET_API_SECRET",
        "passphrase": "POLYMARKET_PASSPHRASE",
        "private_key": "POLYMARKET_PRIVATE_KEY",
        "funder": "POLYMARKET_FUNDER",
        "kalshi_api_key_id": "KALSHI_API_KEY_ID",
        "kalshi_private_key": "KALSHI_PRIVATE_KEY",
        "polymarket_us_key_id": "POLYMARKET_US_KEY_ID",
        "polymarket_us_secret_key": "POLYMARKET_US_SECRET_KEY",
    })

    # KALSHI_PRIVATE_KEY may point at a PEM file rather than hold the key inline.
    kalshi_pk = api_data.get("kalshi_private_key", "")
    if kalshi_pk and "BEGIN" not in kalshi_pk and os.path.isfile(kalshi_pk):
        with open(kalshi_pk, "r") as f:
            api_data["kalshi_private_key"] = f.read()
    
    # Build config objects
    config = BotConfig(
        api=_build_dataclass(ApiConfig, api_data),
        trading=_build_dataclass(TradingConfig, trading_data),
        risk=_build_dataclass(RiskConfig, risk_data),
        mode=_build_dataclass(ModeConfig, mode_data),
        logging=_build_dataclass(LoggingConfig, logging_data),
        monitoring=_build_dataclass(MonitoringConfig, monitoring_data),
        intelligence=_build_intelligence_config(intelligence_data),
        database=_build_dataclass(DatabaseConfig, database_data),
        agent=_build_dataclass(AgentConfig, raw_config.get("agent", {}) or {}),
        directional=_build_directional(raw_config.get("directional", {}) or {}),
        alerts=_build_dataclass(AlertsConfig, raw_config.get("alerts", {}) or {}),
        catalyst=_build_catalyst(raw_config.get("catalyst", {}) or {}),
    )
    
    # Validate
    _validate_config(config)
    
    return config


def _apply_env_overrides(data: dict, env_map: dict[str, str]) -> dict:
    """Apply environment variable overrides to config data."""
    result = data.copy()
    for key, env_var in env_map.items():
        env_value = os.environ.get(env_var)
        if env_value:
            result[key] = env_value
    return result


def _build_dataclass(cls, data: dict):
    """Build a dataclass from a dictionary, ignoring unknown keys."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cls)}
    filtered_data = {k: v for k, v in data.items() if k in field_names}
    return cls(**filtered_data)


def _build_intelligence_config(data: dict) -> IntelligenceConfig:
    """Build the nested IntelligenceConfig (with news/claude sub-objects).

    Missing keys and a missing section both fall back to defaults, so existing
    configs without an ``intelligence:`` block still parse cleanly.
    """
    news = _build_dataclass(IntelligenceNewsConfig, data.get("news", {}) or {})
    claude = _build_dataclass(IntelligenceClaudeConfig, data.get("claude", {}) or {})
    top = {k: v for k, v in data.items() if k not in ("news", "claude")}
    return _build_dataclass(IntelligenceConfig, {**top, "news": news, "claude": claude})


def _build_directional(data: dict) -> DirectionalConfig:
    """Build the nested DirectionalConfig.

    Missing keys and a missing block both fall back to defaults, so existing
    configs without a ``directional:`` block still parse cleanly (enabled=False).
    """
    caps = _build_dataclass(DirectionalCaps, data.get("caps", {}) or {})
    safe_compounder = _build_dataclass(SafeCompounderCfg, data.get("safe_compounder", {}) or {})
    ai_directional = _build_dataclass(AiDirectionalCfg, data.get("ai_directional", {}) or {})
    maker_longshot = _build_dataclass(MakerLongshotCfg, data.get("maker_longshot", {}) or {})
    scanner = _build_dataclass(DirectionalScannerCfg, data.get("scanner", {}) or {})
    weather = _build_dataclass(WeatherCfg, data.get("weather", {}) or {})
    financial = _build_dataclass(FinancialCfg, data.get("financial", {}) or {})
    pmus_weather = _build_dataclass(PMUSWeatherCfg, data.get("pmus_weather", {}) or {})
    macro = _build_dataclass(MacroCfg, data.get("macro", {}) or {})
    sports = _build_dataclass(SportsCfg, data.get("sports", {}) or {})
    _sub = ("caps", "safe_compounder", "ai_directional", "maker_longshot", "scanner", "weather", "financial", "pmus_weather", "macro", "sports")
    top = {k: v for k, v in data.items() if k not in _sub}
    return _build_dataclass(DirectionalConfig, {
        **top,
        "caps": caps,
        "safe_compounder": safe_compounder,
        "ai_directional": ai_directional,
        "maker_longshot": maker_longshot,
        "scanner": scanner,
        "weather": weather,
        "financial": financial,
        "pmus_weather": pmus_weather,
        "macro": macro,
        "sports": sports,
    })


def _build_catalyst(data: dict) -> "CatalystConfig":
    """Build CatalystConfig from raw YAML dict.

    The ``calendar`` list is passed through as-is; missing block falls back to
    defaults (enabled=False).
    """
    return _build_dataclass(CatalystConfig, data)


def _validate_config(config: BotConfig) -> None:
    """Validate configuration values."""
    errors = []
    
    # Trading validation
    if config.trading.min_edge < 0 or config.trading.min_edge > 1:
        errors.append("trading.min_edge must be between 0 and 1")
    
    if config.trading.min_spread < 0 or config.trading.min_spread > 1:
        errors.append("trading.min_spread must be between 0 and 1")
    
    if config.trading.tick_size <= 0:
        errors.append("trading.tick_size must be positive")
    
    if config.trading.default_order_size <= 0:
        errors.append("trading.default_order_size must be positive")
    
    # Risk validation
    if config.risk.max_position_per_market <= 0:
        errors.append("risk.max_position_per_market must be positive")
    
    if config.risk.max_global_exposure <= 0:
        errors.append("risk.max_global_exposure must be positive")
    
    if config.risk.max_daily_loss < 0:
        errors.append("risk.max_daily_loss must be non-negative")
    
    if config.risk.max_drawdown_pct < 0 or config.risk.max_drawdown_pct > 1:
        errors.append("risk.max_drawdown_pct must be between 0 and 1")
    
    # Mode validation
    if config.mode.trading_mode.lower() not in ("live", "dry_run"):
        errors.append("mode.trading_mode must be 'live' or 'dry_run'")

    # M4: Directional mode fields must be "paper" or "live" — typos must not pass silently.
    _valid_modes = {"paper", "live"}
    d = config.directional
    if d.ai_directional.mode not in _valid_modes:
        errors.append(
            f"directional.ai_directional.mode must be 'paper' or 'live', got '{d.ai_directional.mode}'"
        )
    if d.safe_compounder.mode not in _valid_modes:
        errors.append(
            f"directional.safe_compounder.mode must be 'paper' or 'live', got '{d.safe_compounder.mode}'"
        )
    if d.maker_longshot.mode not in _valid_modes:
        errors.append(
            f"directional.maker_longshot.mode must be 'paper' or 'live', got '{d.maker_longshot.mode}'"
        )
    # M4: caps must be positive.
    if d.caps.max_position <= 0:
        errors.append("directional.caps.max_position must be positive")
    if d.caps.total_exposure <= 0:
        errors.append("directional.caps.total_exposure must be positive")
    if d.caps.max_open <= 0:
        errors.append("directional.caps.max_open must be positive")
    
    # Live mode checks
    if config.is_live:
        # Polymarket (polymarket.com) execution auto-falls back to simulated when
        # no wallet key is set, so a missing key is allowed (Kalshi-only / read-only
        # live). Only validate the signing params when a key IS provided.
        has_poly_key = bool(config.api.private_key) and config.api.private_key != "YOUR_PRIVATE_KEY_HERE"
        if has_poly_key:
            if config.api.signature_type not in (0, 1, 2):
                errors.append("api.signature_type must be 0 (EOA), 1 (email proxy), or 2 (browser proxy)")
            if config.api.signature_type in (1, 2) and not config.api.funder:
                errors.append("api.funder (USDC-holding address) is required when signature_type is 1 or 2")

        # Require Kalshi creds whenever any live Kalshi trading path is active.
        kalshi_live = config.mode.kalshi_enabled and (
            config.mode.cross_platform_enabled
            or getattr(config.mode, "kalshi_native_enabled", False)
            or getattr(config.mode, "kalshi_oracle_enabled", False)
        )
        if kalshi_live:
            if not config.api.kalshi_api_key_id:
                errors.append("api.kalshi_api_key_id is required for live Kalshi trading")
            if not config.api.kalshi_private_key or "BEGIN" not in config.api.kalshi_private_key:
                errors.append("api.kalshi_private_key must be an RSA PEM for live Kalshi trading")
        # Sanity: live mode with no tradable venue at all.
        if not has_poly_key and not kalshi_live:
            errors.append("live mode but no tradable venue configured (no Polymarket key and no live Kalshi path)")
    
    if errors:
        raise ConfigError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def save_config(config: BotConfig, config_path: str = "config.yaml") -> None:
    """Save configuration to a YAML file."""
    import dataclasses
    
    def to_dict(obj):
        if dataclasses.is_dataclass(obj):
            return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
        return obj
    
    data = to_dict(config)
    
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_default_config() -> BotConfig:
    """Get a default configuration."""
    return BotConfig()

