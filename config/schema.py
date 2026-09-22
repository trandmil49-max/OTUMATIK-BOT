"""
config/schema.py

Typed, validated schema for every configuration category defined in
SRS Part 19 (CONFIGURATION ENGINE / STRATEGY PROFILE ENGINE / SYSTEM
MANAGEMENT):

    General, Scanner, Risk, Confidence, Coin Trust, Bitcoin, Market Health,
    Telegram, Database, Reports, Logging, Performance, API, Security.

Design rules this module follows (all traceable to the SRS):
  * "Never hardcode values ... Everything should be adjustable without
    modifying source code" -> every tunable lives here as a typed field,
    never inline in engine code.
  * "If configuration is missing, load safe defaults. Never crash because
    of missing settings" -> every field has a sensible default.
  * "Validate every parameter. Reject negative values, invalid ranges,
    impossible combinations" -> Pydantic field/model validators enforce
    this at load time, not at first use deep inside some engine.
  * "Configuration should contain Version, Creation Date, Last Update,
    Compatibility" -> ConfigMetadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────

class StrategyProfileName(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    PROFESSIONAL = "professional"


class RunMode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _lower_str(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _upper_str(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


# ─────────────────────────────────────────────────────────────────────────
# GENERAL  (Part 19: Bot Name, Version, Timezone, Language, Auto Restart,
#           Debug Mode, Paper Trading / Live Mode)
# ─────────────────────────────────────────────────────────────────────────

class GeneralConfig(BaseModel):
    bot_name: str = "Binance Futures Analysis Platform"
    version: str = "8.0.0"
    timezone: str = "Europe/Istanbul"
    language: str = "tr"
    run_mode: RunMode = RunMode.PAPER
    debug_mode: bool = False
    auto_restart: bool = True

    @field_validator("run_mode", mode="before")
    @classmethod
    def _normalize_run_mode(cls, v: Any) -> Any:
        return _lower_str(v)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        import zoneinfo

        try:
            zoneinfo.ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001 - we want to re-raise as ValueError
            raise ValueError(f"Invalid IANA timezone: {v!r}") from exc
        return v


# ─────────────────────────────────────────────────────────────────────────
# SCANNER  (Part 19: Fast/Deep Scan Interval, Max Workers, Max API Requests,
#           Retry Count, Cache Lifetime, Coin Refresh Interval)
# Fast-filter thresholds (Part 6 / Part 16 Stage 1) live here too since they
# are scanner-tuning knobs, not risk/trading knobs.
# ─────────────────────────────────────────────────────────────────────────

class ScannerConfig(BaseModel):
    fast_scan_interval_seconds: int = Field(default=30, gt=0)
    deep_scan_interval_seconds: int = Field(default=120, gt=0)
    max_concurrent_workers: int = Field(default=10, gt=0, le=100)
    max_api_requests_per_minute: int = Field(default=1000, gt=0)
    retry_count: int = Field(default=3, ge=0, le=10)
    cache_lifetime_seconds: int = Field(default=300, gt=0)
    coin_refresh_interval_seconds: int = Field(default=3600, gt=0)

    # Stage 1 "ultra fast filter" thresholds
    min_24h_quote_volume_usdt: float = Field(default=5_000_000, ge=0)
    min_history_candles: int = Field(default=200, ge=50)
    max_spread_pct: float = Field(default=0.15, gt=0, le=5)

    # Caps how many Stage-1 survivors proceed to the (expensive, per-symbol
    # REST-call-heavy) Stage 2 deep analysis in one cycle. `None` (default)
    # means no cap -- every survivor is analyzed, today's behavior.
    # Deliberately optional/opt-in rather than a fixed number: capping
    # blindly by default would silently change signal coverage for every
    # existing deployment; this only takes effect once explicitly set
    # (MAX_SYMBOLS_TO_ANALYZE). See engines/scanner_orchestrator.py's
    # run_scan_cycle() for where this is applied, and note it truncates
    # rather than reorders -- symbols are kept in whatever order Stage 1
    # returned them in, not re-sorted by volume or any other quality
    # signal (no such prioritization was specified).
    max_symbols_to_analyze: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _deep_scan_not_faster_than_fast_scan(self) -> "ScannerConfig":
        if self.deep_scan_interval_seconds < self.fast_scan_interval_seconds:
            raise ValueError(
                "deep_scan_interval_seconds must be >= fast_scan_interval_seconds "
                "(deep analysis is strictly more expensive than the fast filter)"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────
# RISK  (Part 19: Minimum RR, Max Active/Long/Short Trades, Max Same
#        Sector/Coin, Break Even / Dynamic TP / Dynamic SL toggles)
# ─────────────────────────────────────────────────────────────────────────

class RiskConfig(BaseModel):
    min_risk_reward: float = Field(default=2.0, ge=1.0, le=10.0)
    good_risk_reward: float = Field(default=2.5, ge=1.0, le=10.0)
    excellent_risk_reward: float = Field(default=3.0, ge=1.0, le=10.0)

    # Overall portfolio ceiling (SRS Part 17) -- kept generous by default
    # (well above the dynamic cap below's realistic range) precisely so
    # `dynamic_position_cap_*` is the one that actually governs REAL
    # trading day to day; this stays as a true outer safety backstop
    # (e.g. against a burst of paper-mode signals), not a second cap
    # fighting the dynamic one.
    max_active_trades: int = Field(default=20, ge=1, le=50)
    max_long_trades: int = Field(default=5, ge=0, le=50)
    max_short_trades: int = Field(default=5, ge=0, le=50)
    max_same_sector_exposure: int = Field(default=2, ge=1, le=20)
    max_same_coin_exposure: int = Field(default=1, ge=1, le=5)

    # Dynamic position cap for REAL trading only (platform owner's
    # explicit request): with a small starting balance, opening many
    # concurrent positions means each one is too small to survive
    # exchange fees (paid on both entry AND exit), so TradeExecutionEngine
    # additionally caps concurrent REAL positions well below
    # `max_active_trades`, scaling up as the account grows. At the
    # default settings: balance <= $10 -> 5 positions, $20 -> 6, $30 -> 7,
    # and so on (never exceeding `max_active_trades` itself, which
    # remains the overall ceiling used for signal-quality/paper-mode
    # portfolio limits regardless of real balance -- see
    # RiskManagementEngine.check_portfolio_limits()).
    dynamic_position_cap_enabled: bool = True
    dynamic_position_cap_base_balance: float = Field(default=10.0, gt=0)
    dynamic_position_cap_base_count: int = Field(default=5, ge=1, le=50)
    dynamic_position_cap_balance_step: float = Field(default=10.0, gt=0)

    dynamic_tp_enabled: bool = True
    dynamic_sl_enabled: bool = True
    # Profit-lock trailing exit (platform owner's explicit request,
    # autonomous trading pivot): once a trade moves into profit, track
    # the best price seen and close early -- before TP1, without
    # waiting -- if price retraces `trailing_stop_atr_multiple` times the
    # ORIGINAL entry-time ATR (recovered as
    # abs(entry_price - initial_stop_loss) / atr_stop_loss_multiplier,
    # not a freshly re-fetched live ATR -- see
    # PositionMonitorEngine._check_trailing_stop()'s docstring for why).
    # Defaults ON and tight (1x) per the platform owner's explicit
    # "zararı minimuma indirelim" priority: lock in a partial gain rather
    # than risk giving it all back waiting for a TP that may not come.
    trailing_stop_enabled: bool = True
    trailing_stop_atr_multiple: float = Field(default=1.0, gt=0, le=10)

    # Fixed percentage of available balance risked per trade (the dollar
    # amount lost if the stop is hit), not a fixed dollar amount -- this is
    # what makes position sizing scale gently with balance ($12 -> cents at
    # risk, $100 -> a few dollars at risk) with a single parameter.
    risk_per_trade_percent: float = Field(default=3.0, gt=0, le=100)

    # Chase-prevention guard: reject a signal if price has already moved
    # more than this many ATRs in the SAME direction as the signal over the
    # lookback window (see engines.indicators.recent_price_change_pct).
    # Movement in the OPPOSITE direction is never penalized -- it reads as
    # a reversal, not a chase.
    max_recent_move_atr_multiple: float = Field(default=3.0, gt=0, le=20)

    atr_stop_loss_multiplier: float = Field(default=1.5, gt=0, le=10)
    # Sanity cap on the ATR-derived stop distance, independent of RR math:
    # a signal can pass min_risk_reward with a huge stop as long as TP1 is
    # proportionally huge too, but a stop this wide is not tradeable in
    # practice (the position needed to keep risk-per-trade constant becomes
    # tiny, and a stop that far away no longer reflects a real invalidation
    # level -- it reflects ATR having spiked). Ported from sinyal_kanali_2's
    # MAX_STOP_DISTANCE_PCT (hardcoded 0.08 there); COLDE-BOT had no
    # equivalent guard before this -- found at the platform owner's
    # explicit request to audit for "saçma sapan" (nonsensical) stops.
    max_stop_distance_pct: float = Field(default=0.08, gt=0, le=1.0)
    signal_lifetime_hours: int = Field(default=24, ge=1, le=168)

    # Minimum time after a symbol's most recent signal (any direction, any
    # outcome -- closed or rejected) before a new signal for that same
    # symbol may be generated. `None` (default) means no cooldown, today's
    # behavior -- opt-in via SIGNAL_COOLDOWN_MINUTES, same reasoning as
    # ScannerConfig.max_symbols_to_analyze above: this must not silently
    # change existing deployments' signal frequency. Deliberately
    # direction-agnostic (a LONG stop-out followed immediately by a SHORT
    # re-entry on the same coin is exactly the rapid-fire re-signaling
    # pattern a cooldown exists to prevent) -- see
    # engines/signal_generation.py's _check_cooldown().
    signal_cooldown_minutes: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _rr_bands_ordered(self) -> "RiskConfig":
        if not (self.min_risk_reward <= self.good_risk_reward <= self.excellent_risk_reward):
            raise ValueError(
                "risk/reward bands must satisfy min_risk_reward <= good_risk_reward "
                "<= excellent_risk_reward"
            )
        return self

    @model_validator(mode="after")
    def _dynamic_sl_required(self) -> "RiskConfig":
        # SRS Rule 5 ("NO FIXED TP OR SL") is non-negotiable — this cannot be
        # switched off by any profile.
        if not self.dynamic_sl_enabled or not self.dynamic_tp_enabled:
            raise ValueError(
                "dynamic_sl_enabled and dynamic_tp_enabled cannot be disabled: "
                "SRS Rule 5 forbids fixed TP/SL under any strategy profile"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────
# CONFIDENCE  (Part 19: Minimum Confidence, Institutional/Excellent/Very
#              Strong/Strong grade bands — values mirror the worked example
#              bands given in SRS Part 3 / Part 9: 95/90/85/80)
# ─────────────────────────────────────────────────────────────────────────

class ConfidenceConfig(BaseModel):
    minimum_confidence: float = Field(default=80.0, ge=0, le=100)
    strong_grade: float = Field(default=80.0, ge=0, le=100)
    very_strong_grade: float = Field(default=85.0, ge=0, le=100)
    excellent_grade: float = Field(default=90.0, ge=0, le=100)
    institutional_grade: float = Field(default=95.0, ge=0, le=100)

    @model_validator(mode="after")
    def _grade_bands_ordered(self) -> "ConfidenceConfig":
        if not (
            self.strong_grade
            <= self.very_strong_grade
            <= self.excellent_grade
            <= self.institutional_grade
        ):
            raise ValueError(
                "confidence grade bands must satisfy strong_grade <= very_strong_grade "
                "<= excellent_grade <= institutional_grade"
            )
        if self.minimum_confidence > self.strong_grade:
            raise ValueError(
                "minimum_confidence cannot exceed strong_grade "
                "(anything below minimum_confidence is rejected outright)"
            )
        return self

    def grade_for(self, score: float) -> str:
        """Map a confidence score to its SRS Part 3 quality label."""
        if score < self.minimum_confidence:
            return "REJECTED"
        if score >= self.institutional_grade:
            return "INSTITUTIONAL_GRADE"
        if score >= self.excellent_grade:
            return "EXCELLENT"
        if score >= self.very_strong_grade:
            return "VERY_STRONG"
        return "STRONG"


# ─────────────────────────────────────────────────────────────────────────
# BITCOIN  (Part 19: BTC Health Threshold, Trend/Dominance/Volatility
#           Weight, Confirmation Required)
# ─────────────────────────────────────────────────────────────────────────

class BitcoinConfig(BaseModel):
    health_threshold: float = Field(default=60.0, ge=0, le=100)
    trend_weight: float = Field(default=0.30, ge=0, le=1)
    volatility_weight: float = Field(default=0.20, ge=0, le=1)
    confirmation_required: bool = True

    # BTC/USDT dominance (CoinGecko) and DXY (Yahoo Finance), ported from
    # sinyal_kanali_2's MacroClient into BitcoinIntelligenceEngine
    # .score_for_direction() -- a soft nudge on a candidate OTHER symbol's
    # directional score, not part of health_score's blend above (dominance
    # reflects capital ROTATION between BTC/alts/stables, a different
    # concept from "is BTC's own trend/volatility healthy"). Units are
    # POINTS on score_for_direction()'s 0-100 scale, not a 0-1 blend ratio
    # -- dominance_weight was never wired into any blend (see this file's
    # previous state / infrastructure/database/schema.py migration 4), so
    # redefining its unit when finally wiring it in changes no live
    # behavior. btc/usdt dominance are asymmetric (reward-only, matching
    # sinyal_kanali_2 exactly: only added when aligned, never subtracted
    # when conflicting); DXY is symmetric (+/-), also matching.
    dominance_weight: float = Field(default=5.0, ge=0, le=30)
    usdt_dominance_weight: float = Field(default=5.0, ge=0, le=30)
    dxy_weight: float = Field(default=6.0, ge=0, le=30)
    # Percentage-point dead zone for classifying dominance Rising/Falling
    # between two consecutive scans (CoinGecko's /global is a snapshot, not
    # a series -- see infrastructure/macro/models.py). Ported unchanged
    # from sinyal_kanali_2's hardcoded MacroEngine._dominance_trend default.
    dominance_dead_zone_pct: float = Field(default=0.10, ge=0, le=5)


# ─────────────────────────────────────────────────────────────────────────
# SMART MONEY  (Module 23: Binance's official Top Trader Long/Short Ratio,
#               account+position -- see engines/smart_money.py)
# ─────────────────────────────────────────────────────────────────────────

class SmartMoneyConfig(BaseModel):
    # How much of the category's score comes from the by-ACCOUNT-COUNT
    # ratio versus the by-POSITION-SIZE ratio -- see engines/smart_money.py
    # module docstring's "two ratios, not one blended number" note. Not
    # required to sum to 1.0 (Pydantic does not enforce that here) since
    # `_alignment_score` already bounds each side to 0-100 before the
    # weights are applied -- a deliberate choice so a profile CAN weigh
    # both sides down together (e.g. 0.4/0.4) if it wants this whole
    # category to matter less, without a separate on/off flag.
    account_ratio_weight: float = Field(default=0.5, ge=0, le=1)
    position_ratio_weight: float = Field(default=0.5, ge=0, le=1)
    # Binance's documented period enum for /futures/data/topLongShort*
    # ("5m"/"15m"/"30m"/"1h"/"2h"/"4h"/"6h"/"12h"/"1d"). 15m matches this
    # platform's own primary scan timeframe (`ScannerOrchestrator
    # ._STRUCTURE_TIMEFRAME`).
    period: str = Field(default="15m")


# ─────────────────────────────────────────────────────────────────────────
# MARKET HEALTH  (Part 19: Market Health Threshold, Funding/OI/Liquidity/
#                 Spread/Trend Weight)
# ─────────────────────────────────────────────────────────────────────────

class MarketHealthConfig(BaseModel):
    health_threshold: float = Field(default=55.0, ge=0, le=100)
    funding_weight: float = Field(default=0.15, ge=0, le=1)
    open_interest_weight: float = Field(default=0.15, ge=0, le=1)
    liquidity_weight: float = Field(default=0.20, ge=0, le=1)
    spread_weight: float = Field(default=0.15, ge=0, le=1)
    trend_weight: float = Field(default=0.35, ge=0, le=1)


# ─────────────────────────────────────────────────────────────────────────
# COIN TRUST  (Part 19 names this category; Part 7 defines its behaviour —
#              trust score bonus must stay small, historical success must
#              never dominate a fresh signal's evaluation)
# ─────────────────────────────────────────────────────────────────────────

class CoinTrustConfig(BaseModel):
    min_trades_for_trust_score: int = Field(default=10, ge=1)
    max_trust_bonus: float = Field(default=5.0, ge=0, le=20)
    max_trust_penalty: float = Field(default=10.0, ge=0, le=30)
    new_coin_default_trust_score: float = Field(default=50.0, ge=0, le=100)


# ─────────────────────────────────────────────────────────────────────────
# TELEGRAM  (Part 19: Bot Token, Chat ID, Notification Level, Report toggles)
# ─────────────────────────────────────────────────────────────────────────

class TelegramConfig(BaseModel):
    bot_token: str = Field(default="", repr=False)
    chat_id: str = Field(default="", repr=False)
    notification_level: str = "normal"
    send_daily_reports: bool = True
    send_weekly_reports: bool = True
    send_monthly_reports: bool = True
    send_health_reports: bool = True
    max_messages_per_minute: int = Field(default=20, gt=0, le=30)


# ─────────────────────────────────────────────────────────────────────────
# REPORTS  (Part 19: Daily/Weekly/Monthly schedule, Export formats)
# ─────────────────────────────────────────────────────────────────────────

class ReportConfig(BaseModel):
    daily_report_time: str = "23:55"  # HH:MM, bot's configured timezone
    weekly_report_day: str = "sunday"
    monthly_report_day: int = Field(default=1, ge=1, le=28)
    export_csv: bool = True
    export_excel: bool = False
    export_json: bool = True
    export_pdf: bool = False
    min_trades_for_recommendation: int = Field(default=20, ge=1)

    @field_validator("daily_report_time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        try:
            hour, minute = v.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"daily_report_time must be HH:MM, got {v!r}") from exc
        return v

    @field_validator("weekly_report_day")
    @classmethod
    def _validate_weekday(cls, v: str) -> str:
        valid = {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        }
        v_norm = v.strip().lower()
        if v_norm not in valid:
            raise ValueError(f"weekly_report_day must be one of {sorted(valid)}, got {v!r}")
        return v_norm


# ─────────────────────────────────────────────────────────────────────────
# DATABASE  (Part 19: Path, Backup Interval, Retention Policy, Compression,
#            Auto Vacuum)
# ─────────────────────────────────────────────────────────────────────────

class DatabaseConfig(BaseModel):
    path: str = "data/platform.db"
    backup_interval_hours: int = Field(default=24, ge=1)
    retention_policy_days: Optional[int] = None  # None => never delete (SRS: "Do not delete historical data")
    enable_wal_mode: bool = True
    auto_vacuum: bool = True
    compression_enabled: bool = False

    @field_validator("retention_policy_days")
    @classmethod
    def _validate_retention(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 30:
            raise ValueError(
                "retention_policy_days, if set, must be >= 30. The SRS mandates "
                "long-term data retention ('historical data becomes more valuable "
                "over time') — leave unset (null) to never delete."
            )
        return v


# ─────────────────────────────────────────────────────────────────────────
# LOGGING  (Part 19: Log Level, Log Rotation, Max Log Size, Archive Logs)
# ─────────────────────────────────────────────────────────────────────────

class LoggingConfig(BaseModel):
    level: LogLevel = LogLevel.INFO
    rotate_max_bytes: int = Field(default=10_485_760, gt=0)  # 10 MB
    rotate_backup_count: int = Field(default=5, ge=1)
    archive_logs: bool = True
    log_directory: str = "logs"

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, v: Any) -> Any:
        return _upper_str(v)


# ─────────────────────────────────────────────────────────────────────────
# PERFORMANCE  (Part 19: Max CPU/RAM/Queue Size/Scan Time)
# ─────────────────────────────────────────────────────────────────────────

class PerformanceConfig(BaseModel):
    max_cpu_percent: float = Field(default=80.0, gt=0, le=100)
    max_ram_mb: int = Field(default=1024, gt=0)
    max_queue_size: int = Field(default=1000, gt=0)
    max_scan_duration_seconds: int = Field(default=600, gt=0)


# ─────────────────────────────────────────────────────────────────────────
# API  (Part 19 names this category without example fields; scope is
#       reasoned from Part 6/18: Binance credentials + connection tuning.
#       Platform pivoted from signal-only to autonomous trading -- real
#       order placement now requires trade/withdrawal-capable keys.)
# ─────────────────────────────────────────────────────────────────────────

class APIConfig(BaseModel):
    binance_api_key: str = Field(default="", repr=False)
    binance_api_secret: str = Field(default="", repr=False)
    binance_base_url: str = "https://fapi.binance.com"
    # Second exchange, added when the platform owner chose to route real
    # order execution to BingX (no mandatory IP-allowlist for Futures
    # trading, unlike Binance) while keeping every market-data feed
    # (candles, funding, open interest, Smart Money top-trader ratio) on
    # Binance -- see infrastructure.bingx.client's module docstring and
    # TradeExecutionEngine's for the full reasoning. `bingx_base_url`
    # defaults to production; point it at BingX's own VST (Virtual
    # Simulated Trading) demo host, https://open-api-vst.bingx.com, to
    # test with demo money first, exactly like Binance's testnet.
    bingx_api_key: str = Field(default="", repr=False)
    bingx_api_secret: str = Field(default="", repr=False)
    bingx_base_url: str = "https://open-api.bingx.com"
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_retries: int = Field(default=3, ge=0, le=10)

    # Second, independent safety switch for live order placement: even in
    # RunMode.LIVE, TradeExecutionEngine will never place a real order
    # unless this is ALSO True. Requires deliberately setting BOTH the run
    # mode AND this flag -- one alone is never enough. Env: TRADING_ENABLED.
    trading_enabled: bool = False


# ─────────────────────────────────────────────────────────────────────────
# SECURITY  (Part 19 names this category without example fields; scope is
#            reasoned from Part 18 SECURITY section.)
# ─────────────────────────────────────────────────────────────────────────

class SecurityConfig(BaseModel):
    mask_secrets_in_logs: bool = True
    allowed_config_sources: list[str] = Field(default_factory=lambda: ["yaml", "env"])


# ─────────────────────────────────────────────────────────────────────────
# METADATA  (Part 19 VERSION CONTROL: Version, Creation Date, Last Update,
#            Compatibility)
# ─────────────────────────────────────────────────────────────────────────

class ConfigMetadata(BaseModel):
    schema_version: str = "1.0.0"
    strategy_profile: StrategyProfileName = StrategyProfileName.BALANCED
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("strategy_profile", mode="before")
    @classmethod
    def _normalize_profile(cls, v: Any) -> Any:
        return _lower_str(v)


# ─────────────────────────────────────────────────────────────────────────
# ROOT CONFIG
# ─────────────────────────────────────────────────────────────────────────

class PlatformConfig(BaseModel):
    """
    The single, validated, centralized configuration object for the entire
    platform. Every engine reads its settings from an instance of this model
    — never from environment variables or files directly (SRS Part 19:
    "Every module should read its parameters from configuration. Never
    duplicate configuration.").
    """

    metadata: ConfigMetadata = Field(default_factory=ConfigMetadata)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    bitcoin: BitcoinConfig = Field(default_factory=BitcoinConfig)
    smart_money: SmartMoneyConfig = Field(default_factory=SmartMoneyConfig)
    market_health: MarketHealthConfig = Field(default_factory=MarketHealthConfig)
    coin_trust: CoinTrustConfig = Field(default_factory=CoinTrustConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    reports: ReportConfig = Field(default_factory=ReportConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    model_config = {"frozen": False, "validate_assignment": True}

    def safe_dict(self) -> dict[str, Any]:
        """
        Serialize the config to a plain dict with every secret masked.
        This is what gets written to logs and — once the Database module
        exists — into the `config_snapshots` table (SRS Part 12).
        """
        data = self.model_dump(mode="json")
        if self.security.mask_secrets_in_logs:
            for key in ("bot_token", "chat_id"):
                if data.get("telegram", {}).get(key):
                    data["telegram"][key] = "***MASKED***"
            for key in ("binance_api_key", "binance_api_secret"):
                if data.get("api", {}).get(key):
                    data["api"][key] = "***MASKED***"
        return data
