"""
core/models.py

Plain domain dataclasses and enums for the entire platform (Module 3).

These are deliberately `@dataclass`, not Pydantic models: `core/` must
stay dependency-light and framework-free so it can never import
`infrastructure/` (the hexagonal boundary documented in
PROJECT_STATUS.md's DESIGN DECISIONS #1). Validation of *user-facing*
settings belongs to `config/schema.py`; these are internal data carriers
produced and consumed by engines and repositories, not something a human
edits by hand.

Every field here traces back to a concrete SRS requirement -- see each
class's docstring for the specific Part. Timestamps are always
timezone-aware `datetime` objects in UTC; repositories are responsible
for converting to/from SQLite's TEXT storage (SQLite has no native
datetime type -- see PROJECT_STATUS.md's DATABASE section) so nothing
above the repository layer ever has to think about that conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────


class SignalDirection(str, Enum):
    """SRS Part 11: every signal message states Direction (LONG / SHORT)."""

    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, Enum):
    """
    SRS Part 10 (POSITION MONITOR / TRADE MANAGEMENT ENGINE): "Each trade
    should always have one status." Listed there in exactly this order.
    """

    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    STOP_LOSS = "STOP_LOSS"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    # Profit-lock early exit (platform owner's explicit request): the
    # trade moved into profit but never reached TP1 before price
    # retraced by a volatility-scaled distance from the best price seen
    # -- closed early to bank the partial gain rather than risk giving it
    # all back waiting for a TP that may never come. Distinct from
    # TP1_HIT (reached the full target) and STOP_LOSS (lost money) --
    # see PositionMonitorEngine._check_trailing_stop().
    TRAILING_STOP_EXIT = "TRAILING_STOP_EXIT"


class ConfidenceGrade(str, Enum):
    """
    SRS Part 3/9 quality labels. Values match
    `config.schema.ConfidenceConfig.grade_for()`'s return strings exactly,
    so the future Confidence Engine (Module 12) can assign
    `ConfidenceGrade(config.confidence.grade_for(score))` directly.
    """

    REJECTED = "REJECTED"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"
    EXCELLENT = "EXCELLENT"
    INSTITUTIONAL_GRADE = "INSTITUTIONAL_GRADE"


class CoinClassification(str, Enum):
    """SRS Part 7: COIN CLASSIFICATION."""

    ULTRA_HIGH_QUALITY = "ULTRA_HIGH_QUALITY"
    HIGH_QUALITY = "HIGH_QUALITY"
    MEDIUM_QUALITY = "MEDIUM_QUALITY"
    HIGH_RISK = "HIGH_RISK"
    SPECULATIVE = "SPECULATIVE"
    NEW_LISTING = "NEW_LISTING"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"


class ReportType(str, Enum):
    """SRS Part 15: REPORT TYPES."""

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    COIN_PERFORMANCE = "COIN_PERFORMANCE"
    FILTER_PERFORMANCE = "FILTER_PERFORMANCE"
    MARKET = "MARKET"
    BITCOIN = "BITCOIN"
    TRADE_REVIEW = "TRADE_REVIEW"


class RejectionReason(str, Enum):
    """
    SRS Part 14: REJECTION REASONS -- "Every rejection must contain at
    least one reason", drawn from this exact enumerated list.

    Not explicitly named in PROJECT_STATUS.md's planned dataclass list,
    but required by SRS Part 14's mandatory closed set of reasons; added
    here per Part 23's "Final Implementation Authority" (fills a gap
    without removing/weakening anything already decided, and is
    documented here as required).
    """

    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    LOW_VOLUME = "LOW_VOLUME"
    WEAK_TREND = "WEAK_TREND"
    WEAK_MOMENTUM = "WEAK_MOMENTUM"
    WEAK_MARKET_STRUCTURE = "WEAK_MARKET_STRUCTURE"
    BITCOIN_CONFLICT = "BITCOIN_CONFLICT"
    POOR_RISK_REWARD = "POOR_RISK_REWARD"
    LARGE_SPREAD = "LARGE_SPREAD"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    WEAK_COIN_TRUST = "WEAK_COIN_TRUST"
    FUNDING_CONFLICT = "FUNDING_CONFLICT"
    OPEN_INTEREST_CONFLICT = "OPEN_INTEREST_CONFLICT"
    INVALID_DATA = "INVALID_DATA"
    API_ERROR = "API_ERROR"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    EXPIRED_OPPORTUNITY = "EXPIRED_OPPORTUNITY"
    # Added during Module 10 (Risk Management Engine): SRS Part 17 portfolio-level
    # checks (max concurrent trades, max same-symbol exposure) have no fitting
    # value in the original set above. Purely additive -- does not change any
    # existing member's name or value, so every prior reference remains valid.
    PORTFOLIO_RISK_LIMIT = "PORTFOLIO_RISK_LIMIT"
    CORRELATION_RISK = "CORRELATION_RISK"
    # Added for RiskConfig.signal_cooldown_minutes (opt-in, see
    # config/schema.py): distinct from DUPLICATE_SIGNAL, which guards
    # against an already-WAITING/ACTIVE signal for the same symbol right
    # now, not a too-soon-after-the-last-one re-signal once nothing is
    # active anymore. Purely additive, same pattern as the two reasons
    # immediately above.
    SIGNAL_COOLDOWN = "SIGNAL_COOLDOWN"
    # Added for RiskConfig.max_recent_move_atr_multiple (autonomous trading
    # pivot): chase-prevention guard, distinct from HIGH_VOLATILITY (an
    # abnormally wide ATR right now) -- this instead looks BACKWARD over the
    # recent candle window for a same-direction move that already ran too far
    # before the signal fired. Purely additive, same pattern as the reasons
    # immediately above.
    OVEREXTENDED_MOVE = "OVEREXTENDED_MOVE"


# ─────────────────────────────────────────────────────────────────────────
# COIN DISCOVERY (SRS Part 7)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Coin:
    """A single Binance USDT-M Futures symbol tracked by the platform."""

    symbol: str
    base_asset: str
    quote_asset: str = "USDT"
    status: str = "TRADING"
    is_active: bool = True
    first_seen_at: datetime = field(default_factory=lambda: _utc_now())
    last_seen_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class CoinProfile:
    """
    SRS Part 7 COIN PROFILE SYSTEM: "Every symbol must have its own
    profile ... The bot must learn those differences."
    """

    symbol: str
    liquidity_score: float = 0.0
    volatility_score: float = 0.0
    trend_reliability_score: float = 0.0
    spread_quality_score: float = 0.0
    historical_stability_score: float = 0.0
    average_daily_volume_usdt: float = 0.0
    average_atr_percent: float = 0.0
    average_trend_length_candles: float = 0.0
    average_pullback_size_percent: float = 0.0
    average_fake_breakout_frequency: float = 0.0
    average_success_rate_percent: float = 0.0
    classification: CoinClassification = CoinClassification.NEW_LISTING
    coin_trust_score: float = 50.0  # SRS Part 19 default: new_coin_default_trust_score
    updated_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class CoinStatistics:
    """
    SRS Part 12 COIN STATISTICS + Part 7 COIN PERFORMANCE DATABASE.

    `current_streak` is signed: positive N means N consecutive wins,
    negative N means N consecutive losses, 0 means no trades yet.
    """

    symbol: str
    total_signals: int = 0
    winning_signals: int = 0
    losing_signals: int = 0
    tp1_count: int = 0
    sl_count: int = 0
    average_rr: float = 0.0
    average_confidence: float = 0.0
    average_duration_seconds: float = 0.0
    win_rate_percent: float = 0.0
    current_streak: int = 0
    longest_winning_streak: int = 0
    longest_losing_streak: int = 0
    updated_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# SIGNALS (SRS Part 4 position structure + Part 9 scoring + Part 12 storage)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Signal:
    """
    A generated trade signal.

    SRS Part 4 (POSITION STRUCTURE) requires the core position to contain
    "only" Entry Price, Stop Loss, TP1, Confidence Score, and RR.
    Part 11 (TELEGRAM SIGNAL MESSAGE) and Part 12 (SIGNAL STORAGE) require
    a richer field set for persistence/presentation. Per
    PROJECT_STATUS.md's resolved ambiguity, updated for the single-TP
    model: the five core fields (`entry_price`, `stop_loss`,
    `take_profit_1`, `confidence_score`, `risk_reward_ratio`) are the
    core trade state;
    everything else here is presentation/analytics enrichment, never
    additional core trading logic.
    """

    symbol: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    risk_reward_ratio: float
    confidence_score: float
    confidence_grade: ConfidenceGrade
    leverage: int = 1
    coin_trust_score: Optional[float] = None
    risk_score: Optional[float] = None
    bitcoin_score: Optional[float] = None
    market_score: Optional[float] = None
    status: TradeStatus = TradeStatus.WAITING
    trade_result: Optional[str] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class SignalScoreComponent:
    """
    SRS Part 9 EXPLAINABLE DECISION: "Every signal should store internally
    why it was accepted ... Trend +18, Structure +15 ... Penalty -3."
    One row per contributing category for one signal.
    """

    signal_id: int
    category: str
    points: float
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# TRADES (SRS Part 10 Position Monitor + Part 12 Trade Storage)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Trade:
    """
    An active-or-completed trade opened from an accepted `Signal`.

    Combines Part 10's "Active Trade Database" fields (tracked while the
    trade is open) with Part 12's "Trade Storage" fields (finalized once
    the trade closes). `current_stop_loss` is mutable (break-even /
    trailing stop moves it); `initial_stop_loss` never changes after
    creation and is kept for post-trade risk review (Part 13: "Was Stop
    Loss too tight?").
    """

    signal_id: int
    symbol: str
    direction: SignalDirection
    entry_price: float
    entry_time: datetime
    initial_stop_loss: float
    current_stop_loss: float
    take_profit_1: float
    confidence_score: float
    leverage: int = 1
    coin_trust_score: Optional[float] = None
    risk_score: Optional[float] = None
    bitcoin_score: Optional[float] = None
    status: TradeStatus = TradeStatus.ACTIVE
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    realized_pnl_percent: Optional[float] = None
    max_favorable_excursion_percent: Optional[float] = None
    max_adverse_excursion_percent: Optional[float] = None
    exit_reason: Optional[str] = None
    duration_seconds: Optional[int] = None
    tp1_hit_at: Optional[datetime] = None
    tp1_exit_price: Optional[float] = None
    # Real Binance order IDs (autonomous trading pivot), for reconciliation
    # -- letting TradeExecutionEngine/an ops script look up an order's
    # actual status on the exchange later. `None` for a PAPER-mode trade
    # (no real order was ever placed) or before the entry order confirms.
    entry_order_id: Optional[int] = None
    stop_order_id: Optional[int] = None
    take_profit_order_id: Optional[int] = None
    # Highest (LONG) / lowest (SHORT) price observed since entry, but only
    # while the trade has been in profit at least once -- None until then.
    # Drives the trailing-stop profit lock (see
    # PositionMonitorEngine._check_trailing_stop()); never set/read for a
    # trade that has never moved into profit.
    best_price_since_entry: Optional[float] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())

    @property
    def is_closed(self) -> bool:
        """True once the trade has reached a terminal status."""
        return self.status in {
            TradeStatus.TP1_HIT,
            TradeStatus.STOP_LOSS,
            TradeStatus.EXPIRED,
            TradeStatus.CANCELLED,
            TradeStatus.ERROR,
            TradeStatus.TRAILING_STOP_EXIT,
        }

    @property
    def tp1_pnl_percent(self) -> Optional[float]:
        """
        Partial profit locked in at TP1, or None if TP1 hasn't fired yet.
        Deliberately mirrors PositionMonitorEngine._realized_pnl_percent()'s
        formula exactly rather than calling it (that method lives on the
        engine, not the model, to keep this module free of engine
        imports) -- if one changes, so must the other.
        """
        if self.tp1_exit_price is None:
            return None
        direction_sign = 1.0 if self.direction == SignalDirection.LONG else -1.0
        return ((self.tp1_exit_price - self.entry_price) / self.entry_price) * 100.0 * direction_sign


# ─────────────────────────────────────────────────────────────────────────
# REJECTION & MISSED OPPORTUNITY (SRS Part 14)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Rejection:
    """
    SRS Part 14 SIGNAL REJECTION DATABASE: "Store every rejected signal
    ... Every rejection must have a reason. Nothing should be discarded
    without explanation."
    """

    symbol: str
    primary_reason: RejectionReason
    direction: Optional[SignalDirection] = None
    confidence_score: Optional[float] = None
    risk_score: Optional[float] = None
    bitcoin_score: Optional[float] = None
    coin_trust_score: Optional[float] = None
    market_health_score: Optional[float] = None
    smart_money_score: Optional[float] = None
    secondary_reason: Optional[RejectionReason] = None
    rejected_filters: list[str] = field(default_factory=list)
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class MissedOpportunity:
    """
    SRS Part 14 MISSED OPPORTUNITY ENGINE: "Sometimes a rejected signal
    becomes a successful move later. Track these events ... Do NOT change
    strategy. Use it for analysis only."
    """

    symbol: str
    rejected_at: datetime
    reject_reason: RejectionReason
    price_at_rejection: float
    subsequent_max_move_percent: float
    evaluation_window_hours: int
    rejection_id: Optional[int] = None
    confidence_score_at_rejection: Optional[float] = None
    market_condition: Optional[str] = None
    bitcoin_condition: Optional[str] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# MARKET / BITCOIN STATISTICS (SRS Part 8 + Part 12)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class BtcStatisticsSnapshot:
    """SRS Part 12 BITCOIN STATISTICS: historical Bitcoin health snapshots."""

    snapshot_time: datetime
    trend: str
    health_score: float
    volatility_score: float
    price: float
    funding_rate: Optional[float] = None
    open_interest_usdt: Optional[float] = None
    # BTC/USDT dominance (CoinGecko) and DXY (Yahoo Finance), ported from
    # sinyal_kanali_2's MacroClient. Raw *_pct values are kept (not just the
    # classified trend) because dominance trend classification needs the
    # PREVIOUS snapshot's raw pct to compare against (CoinGecko's endpoint is
    # a snapshot, not a series) -- see MacroDataClient / BitcoinIntelligenceEngine
    # ._dominance_trend. DXY needs no such history: its own SMA20-vs-SMA50
    # relationship is self-contained per snapshot.
    btc_dominance_pct: Optional[float] = None
    usdt_dominance_pct: Optional[float] = None
    btc_dominance_trend: Optional[str] = None  # "Rising" | "Falling" | "Flat" | "Unknown"
    usdt_dominance_trend: Optional[str] = None  # same set
    dxy_trend: Optional[str] = None  # "Bullish" | "Bearish" | "Mixed" | "Unknown"
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class MarketStatisticsSnapshot:
    """SRS Part 12 MARKET STATISTICS + Part 8 MARKET STATES."""

    snapshot_time: datetime
    market_health_score: float
    market_state: str
    average_liquidity_score: Optional[float] = None
    average_volatility_score: Optional[float] = None
    trend_quality_score: Optional[float] = None
    average_spread_percent: Optional[float] = None
    average_funding_rate: Optional[float] = None
    total_open_interest_usdt: Optional[float] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# REPORTING & ANALYTICS (SRS Part 12/14/15)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Report:
    """
    SRS Part 15 PROFESSIONAL REPORTING ENGINE. `content` holds the
    report-type-specific statistics as a plain JSON-serializable dict
    rather than one column per possible statistic, since the exact
    content shape differs per `ReportType` (Daily vs. Bitcoin Report vs.
    Filter Report all report very different fields) and the SRS explicitly
    allows/encourages new report types (Part 23: future expansion).
    `turkish_analysis` / `turkish_recommendations` satisfy SRS Rule 8's
    Turkish-language requirement for bot self-analysis and recommendations.
    """

    report_type: ReportType
    period_start: datetime
    period_end: datetime
    content: dict[str, Any] = field(default_factory=dict)
    turkish_analysis: Optional[str] = None
    turkish_recommendations: Optional[str] = None
    overall_grade: Optional[str] = None
    id: Optional[int] = None
    generated_at: datetime = field(default_factory=lambda: _utc_now())
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class FilterPerformance:
    """
    SRS Part 14 FILTER PERFORMANCE / FILTER SCORE: "Every filter receives
    Performance Score, Reliability Score, Contribution Score, Historical
    Accuracy. Never remove filters automatically."
    """

    filter_name: str
    period_start: datetime
    period_end: datetime
    trades_rejected: int = 0
    saved_losses_count: int = 0
    contribution_score: Optional[float] = None
    success_rate_percent: Optional[float] = None
    reliability_score: Optional[float] = None
    id: Optional[int] = None
    computed_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# SYSTEM / PRODUCTION (SRS Part 12/18)
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class BotHealthSnapshot:
    """SRS Part 12/18 BOT HEALTH: point-in-time system health snapshot."""

    snapshot_time: datetime
    status: str = "HEALTHY"  # HEALTHY | WARNING | CRITICAL (SRS Part 18 HEALTH CHECK)
    cpu_percent: Optional[float] = None
    ram_mb: Optional[float] = None
    average_scan_duration_seconds: Optional[float] = None
    average_api_response_ms: Optional[float] = None
    database_size_mb: Optional[float] = None
    restart_count: int = 0
    error_count: int = 0
    retry_count: int = 0
    active_trades_count: int = 0
    symbols_scanned_count: int = 0
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class ErrorEvent:
    """
    Persisted record of a CRITICAL/FATAL `PlatformError` (SRS Rule 11:
    "NEVER HIDE ERRORS"). Per PROJECT_STATUS.md's design decision, only
    CRITICAL/FATAL severities are persisted here -- INFO/WARNING/ERROR
    stay in Module 2's file-based category logs, which already satisfy
    the SRS's "API Logs / Telegram Logs / System Logs" tables so this
    table is not a duplicate of that.
    """

    occurred_at: datetime
    severity: str
    category: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    resolved: bool = False
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


@dataclass
class ConfigSnapshot:
    """
    SRS Part 19 VERSION CONTROL: "Configuration should contain Version,
    Creation Date, Last Update, Compatibility." `config_json` stores
    `PlatformConfig.safe_dict()` (secrets already masked) so a snapshot
    is always safe to store and later inspect.
    """

    captured_at: datetime
    strategy_profile: str
    schema_version: str
    config_json: dict[str, Any]
    reason: Optional[str] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: _utc_now())


# ─────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    """Single source of "now" for every dataclass default -- always UTC, always tz-aware."""
    from datetime import timezone

    return datetime.now(timezone.utc)
