"""
engines/signal_generation.py

Signal Generation Engine (SRS Part 1 MASTER PIPELINE + Part 4 POSITION
STRUCTURE): the top-level orchestrator that turns "a candidate
symbol+direction survived Fast Filter" into either a persisted `Signal`
(via `SignalRepository`) or a persisted `Rejection` (via `RejectionEngine`)
-- every candidate ends up in exactly one of those two tables, never
neither (SRS Part 14: "nothing should be discarded without explanation").

Orchestration order (cheapest / most-likely-to-reject checks first):
    1. Duplicate-signal guard -- an existing WAITING/ACTIVE signal for
       the same symbol+direction (mirrors the old bot's "three-layer
       duplicate guard" from project history).
    1b. Cooldown guard (opt-in via `RiskConfig.signal_cooldown_minutes`,
        `None` by default) -- too soon after that SAME symbol's most
        recent signal, regardless of direction or how it concluded.
        Distinct from step 1: this still applies once nothing is
        WAITING/ACTIVE anymore, guarding against rapid-fire re-signaling
        right after a symbol's last signal closed or was rejected.
    2. `RiskManagementEngine.assess()` -- portfolio limits, then SL/TP/RR,
       which the rest of the pipeline needs.
    3. `ConfidenceEngine.assess()` -- combines the risk assessment with
       the Bitcoin/Market Health/Coin Trust/Market Structure scores the
       caller already computed.
    4. `RiskManagementEngine.calculate_leverage()` -- deliberately run
       AFTER confidence, not as part of step 2: suggested leverage is now
       a function of the confidence score itself (see
       `engines.risk_management`'s "Leverage note"), so it cannot be
       known until step 3 has produced one. A confidence score that
       clears `ConfidenceConfig.minimum_confidence` but still lands below
       the leverage table's own 75-point floor is rejected here too
       (`RejectionReason.LOW_CONFIDENCE`) -- see that engine's docstring
       for why that floor exists independently of `minimum_confidence`.
    5. On success: persist a `Signal` plus its `SignalScoreComponent`
       breakdown, taken directly from `ConfidenceAssessment.components`
       so the stored explanation is exactly what was actually scored,
       never recomputed or re-derived.

Like `ConfidenceEngine`, this engine takes the upstream engines'
already-computed outputs (a `MarketStructureResult` and three 0-100
floats) as plain data rather than depending on
`BitcoinIntelligenceEngine`/`MarketHealthEngine`/`CoinTrustEngine`/a
Binance client directly -- calling those, in the right order, on live
data is the future Scanner Orchestrator's job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import RejectionReason, Signal, SignalDirection, SignalScoreComponent, TradeStatus
from engines.confidence import ConfidenceAssessment, ConfidenceEngine, MomentumSnapshot
from engines.market_structure import MarketStructureResult
from engines.rejection import RejectionContext, RejectionEngine
from engines.risk_management import RiskAssessment, RiskManagementEngine
from infrastructure.database.repositories.signal_repository import SignalRepository
from system.logging_setup import get_logger

_logger = get_logger("trading")

# Statuses that make an existing signal count as "still live" for the duplicate-signal guard.
_LIVE_SIGNAL_STATUSES: tuple[TradeStatus, ...] = (TradeStatus.WAITING, TradeStatus.ACTIVE)


class SignalGenerationEngine:
    """Orchestrates Risk Management + Confidence into a persisted Signal, or a persisted Rejection."""

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        risk_engine: Optional[RiskManagementEngine] = None,
        confidence_engine: Optional[ConfidenceEngine] = None,
        signal_repository: Optional[SignalRepository] = None,
        rejection_engine: Optional[RejectionEngine] = None,
    ) -> None:
        self._config = config or get_config()
        self._risk_engine = risk_engine or RiskManagementEngine(config=self._config)
        self._confidence_engine = confidence_engine or ConfidenceEngine(config=self._config)
        self._signal_repository = signal_repository or SignalRepository()
        self._rejection_engine = rejection_engine or RejectionEngine()

    def generate(
        self,
        symbol: str,
        direction: SignalDirection,
        entry_price: float,
        atr: float,
        structure: MarketStructureResult,
        bitcoin_score: float,
        market_health_score: float,
        coin_trust_score: float,
        smart_money_score: float,
        structural_target: Optional[float] = None,
        momentum: Optional[MomentumSnapshot] = None,
        recent_move_pct: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Signal]:
        """
        Run the full candidate-to-signal (or candidate-to-rejection)
        pipeline for one symbol+direction.

        `smart_money_score`: `SmartMoneyEngine.score_for_direction()`'s
            0-100 output (Module 23), passed straight through to
            `ConfidenceEngine.assess()` -- required like the other three
            context scores, since `SmartMoneyEngine` itself always
            returns a real (neutral 50.0 on missing data) float, never
            `None` -- see that engine's docstring.

        `momentum`: latest RSI/MACD/ADX/volume_ratio reading for this
            symbol, passed straight through to `ConfidenceEngine.assess()`
            -- see that engine's docstring. `None` (the default) is safe:
            confidence scoring just skips the adjustment.

        `recent_move_pct`: signed percent price change over the recent
            lookback window, passed straight through to
            `RiskManagementEngine.assess()`'s chase-prevention guard --
            see that method's docstring. `None` (the default) skips the
            guard.

        `now` is injectable (defaults to the real current time) purely so
        the cooldown check is testable without sleeping in tests -- same
        pattern as `execution_modes.live.LiveRunner.run_one_cycle()`.

        Returns:
            The persisted `Signal` if every check passed, else `None`
            (a `Rejection` was persisted instead; check the logs/DB for
            why via `RejectionRepository`, not this return value).
        """
        now = now or datetime.now(timezone.utc)

        duplicate_reason = self._check_duplicate(symbol, direction)
        if duplicate_reason is not None:
            self._rejection_engine.record(symbol, duplicate_reason, RejectionContext(direction=direction))
            return None

        if self._check_cooldown(symbol, now):
            self._rejection_engine.record(symbol, RejectionReason.SIGNAL_COOLDOWN, RejectionContext(direction=direction))
            return None

        risk = self._risk_engine.assess(
            symbol=symbol, direction=direction, entry_price=entry_price, atr=atr,
            structural_target=structural_target, recent_move_pct=recent_move_pct,
        )
        if not risk.approved:
            self._record_rejection(symbol, direction, risk.rejection_reason, risk, bitcoin_score, market_health_score, coin_trust_score, smart_money_score, confidence=None)
            return None

        confidence = self._confidence_engine.assess(
            symbol=symbol, direction=direction, structure=structure, risk=risk,
            bitcoin_score=bitcoin_score, market_health_score=market_health_score, coin_trust_score=coin_trust_score,
            smart_money_score=smart_money_score, momentum=momentum,
        )
        if not confidence.approved:
            self._record_rejection(symbol, direction, confidence.rejection_reason, risk, bitcoin_score, market_health_score, coin_trust_score, smart_money_score, confidence=confidence)
            breakdown = " ".join(f"{c.category}={c.points:.1f}/{c.max_points:.0f}" for c in confidence.components)
            _logger.info(
                "Signal rejected: %s %s confidence=%.1f (need >= %.1f) reason=%s | %s",
                symbol, direction.value, confidence.confidence_score, self._config.confidence.minimum_confidence,
                confidence.rejection_reason.value if confidence.rejection_reason else "unknown", breakdown,
            )
            return None

        leverage = self._risk_engine.calculate_leverage(confidence.confidence_score)
        if leverage <= 0:
            # Cleared ConfidenceConfig.minimum_confidence but not the
            # leverage table's own (lower) 75-point floor -- see
            # engines.risk_management's "Leverage note".
            self._record_rejection(symbol, direction, RejectionReason.LOW_CONFIDENCE, risk, bitcoin_score, market_health_score, coin_trust_score, smart_money_score, confidence=confidence)
            return None

        signal = self._persist_signal(
            symbol, direction, entry_price, risk, confidence, leverage, bitcoin_score, market_health_score, coin_trust_score
        )
        _logger.info(
            "Signal generated: %s %s entry=%.8f confidence=%.1f (%s) leverage=%dx",
            symbol, direction.value, entry_price, confidence.confidence_score, confidence.grade.value, leverage,
        )
        return signal

    def _check_duplicate(self, symbol: str, direction: SignalDirection) -> Optional[RejectionReason]:
        for status in _LIVE_SIGNAL_STATUSES:
            existing = self._signal_repository.find_by_symbol_and_status(symbol, status)
            if any(signal.direction == direction for signal in existing):
                return RejectionReason.DUPLICATE_SIGNAL
        return None

    def _check_cooldown(self, symbol: str, now: datetime) -> bool:
        """
        True if `symbol`'s most recent signal (any direction, any
        outcome) is still within `RiskConfig.signal_cooldown_minutes` of
        `now`. Always False when the config is unset (`None`, the
        default) -- see that field's docstring in config/schema.py.
        """
        cooldown_minutes = self._config.risk.signal_cooldown_minutes
        if cooldown_minutes is None:
            return False
        most_recent = self._signal_repository.find_most_recent_by_symbol(symbol)
        if most_recent is None:
            return False
        elapsed = now - most_recent.created_at
        return elapsed.total_seconds() < cooldown_minutes * 60

    def _record_rejection(
        self,
        symbol: str,
        direction: SignalDirection,
        reason: Optional[RejectionReason],
        risk: RiskAssessment,
        bitcoin_score: float,
        market_health_score: float,
        coin_trust_score: float,
        smart_money_score: float,
        confidence: Optional[ConfidenceAssessment],
    ) -> None:
        self._rejection_engine.record(
            symbol,
            reason or RejectionReason.INVALID_DATA,
            RejectionContext(
                direction=direction,
                confidence_score=confidence.confidence_score if confidence is not None else None,
                risk_score=risk.risk_score,
                bitcoin_score=bitcoin_score,
                coin_trust_score=coin_trust_score,
                market_health_score=market_health_score,
                smart_money_score=smart_money_score,
            ),
        )

    def _persist_signal(
        self,
        symbol: str,
        direction: SignalDirection,
        entry_price: float,
        risk: RiskAssessment,
        confidence: ConfidenceAssessment,
        leverage: int,
        bitcoin_score: float,
        market_health_score: float,
        coin_trust_score: float,
    ) -> Signal:
        signal = Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=risk.stop_loss,
            take_profit_1=risk.take_profit_1,
            risk_reward_ratio=risk.risk_reward_ratio,
            confidence_score=confidence.confidence_score,
            confidence_grade=confidence.grade,
            leverage=leverage,
            coin_trust_score=coin_trust_score,
            risk_score=risk.risk_score,
            bitcoin_score=bitcoin_score,
            market_score=market_health_score,
            status=TradeStatus.WAITING,
        )
        score_components = [
            SignalScoreComponent(signal_id=0, category=c.category, points=c.points) for c in confidence.components
        ]
        return self._signal_repository.create(signal, score_components)
