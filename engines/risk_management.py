"""
engines/risk_management.py

Risk Management Engine (SRS Part 4 POSITION STRUCTURE + Part 17 ADVANCED
RISK MANAGEMENT). Owns every number in the "core six" position fields
except Confidence Score: Stop Loss, TP1, and RR are all computed
here from ATR, never from a fixed percentage (SRS Rule 5: "NO FIXED TP
OR SL" -- enforced at the config layer too, see `RiskConfig`'s
`_dynamic_sl_required` validator, which makes disabling this
impossible).

Take-profit design note: TP1 is placed at
`stop_distance * min_risk_reward` -- but placing
TP1 there UNCONDITIONALLY would make the risk/reward check tautological
(it would always equal exactly `min_risk_reward` by construction, never
actually rejecting anything). `assess()` therefore accepts an optional
`structural_target` (the nearest opposing swing level from
`engines.market_structure.MarketStructureEngine`) and clamps TP1 to it
when it is CLOSER than the policy-band target -- i.e. "don't promise a
reward the market's own structure doesn't support, even if the RR
formula would allow it". This is what makes the resulting RR check a
genuine filter once Module 15 (Signal Generation) wires Market Structure
output through, while still degrading gracefully to a pure ATR/RR-tier
calculation when no structural level is available (e.g. these unit tests).

Leverage note (revised -- capital preservation over aggressive returns,
per the platform owner's explicit instruction): suggested leverage is no
longer a function of volatility. It is now a direct function of the
CONFIDENCE SCORE `engines.confidence.ConfidenceEngine` produces, on the
theory that leverage is a bet on how *certain* a setup is, not how calm
the market happens to be right now. Concretely, capped at 10x (never
higher, regardless of confidence) and tiered:

    confidence >= 95  -> 10x   (institutional grade)
    confidence >= 90  ->  8x   (excellent)
    confidence >= 85  ->  7x   (very strong)
    confidence >= 80  ->  5x   (strong)
    confidence >= 75  ->  3x   (below the platform's own STRONG floor,
                                 but still worth a small, defensive
                                 leverage figure rather than none)
    confidence <  75  ->  0    (ineligible -- see below)

These break points actually READ `ConfidenceConfig`'s own
strong_grade/very_strong_grade/excellent_grade/institutional_grade at
call time (80/85/90/95 by default, but every non-BALANCED strategy
profile moves at least one of them -- e.g. `professional.yaml` raises
institutional_grade to 96 and excellent_grade to 93) rather than a fixed
copy of the default numbers, so a signal's leverage always tracks the
SAME grade bands that decided its `ConfidenceGrade` label, whichever
profile is active. Getting this wrong is not cosmetic: under the old
hardcoded 95/90/85/80 tiers, a confidence of 95.5 under the `professional`
profile was graded EXCELLENT (institutional_grade=96 there, not yet
reached) but still received the full 10x meant for INSTITUTIONAL_GRADE --
exactly backwards for the one profile whose entire point is tighter
capital exposure, not looser.

One band below `strong_grade` is an explicit floor -- a fixed 5 points
lower (mirroring the 5-point spacing between the other four tiers),
clamped at 0: `calculate_leverage()` returns `0` (never a real leverage
value) below it, and `SignalGenerationEngine` treats that as "do not
generate this signal" -- a hard safety net that holds even if some future
strategy profile ever configures `minimum_confidence` below `strong_grade`
(every profile shipped today sets them equal, which already makes this
floor unreachable in practice: `ConfidenceEngine` rejects anything below
`minimum_confidence` before `calculate_leverage()` is ever called. That is
fine -- it is a defensive backstop, not something meant to fire routinely).

Because leverage now depends on the confidence score, and
`ConfidenceEngine.assess()` itself depends on this engine's own
`RiskAssessment` (specifically `risk_score`), leverage can no longer be
computed inside `assess()` -- confidence isn't known yet at that point in
the pipeline. `RiskAssessment` therefore no longer carries a `leverage`
field at all; `calculate_leverage(confidence_score)` is called
separately, by `SignalGenerationEngine`, once confidence has actually
been scored. Kept as a method on THIS engine (rather than moving to
`ConfidenceEngine`) because leverage is fundamentally a capital-exposure
decision, not a scoring one -- `engines.confidence` decides how good a
setup is; this engine decides what to do about it. The five break points
above remain local, documented constants rather than new `RiskConfig`
fields, following the same "informational, not exchange-bound" reasoning
the original version of this note gave, and the same pattern as
`engines.bitcoin_intelligence`'s ADX threshold and
`engines.market_health`'s heuristic scales.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import RejectionReason, SignalDirection, Trade
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.exceptions import RiskManagementError
from system.logging_setup import get_logger

_logger = get_logger("trading")


@dataclass(frozen=True)
class RiskAssessment:
    """Everything `RiskManagementEngine.assess()` produces for one candidate signal."""

    approved: bool
    risk_score: float
    stop_loss: float
    take_profit_1: float
    risk_reward_ratio: float
    rejection_reason: Optional[RejectionReason] = None
    detail: Optional[str] = None


class RiskManagementEngine:
    """Computes dynamic SL/TP/RR/leverage/risk-score, and enforces portfolio-level exposure limits."""

    # Leverage granted at/above each ConfidenceConfig grade band -- capital
    # preservation over aggressive returns; never above 10x. The actual
    # score floors are read from self._config.confidence at call time (see
    # _leverage_tiers()), not hardcoded here, so they track whichever
    # strategy profile is active -- see the module docstring's "Leverage
    # note" for the full rationale.
    _LEVERAGE_AT_INSTITUTIONAL_GRADE = 10
    _LEVERAGE_AT_EXCELLENT_GRADE = 8
    _LEVERAGE_AT_VERY_STRONG_GRADE = 7
    _LEVERAGE_AT_STRONG_GRADE = 5
    _LEVERAGE_AT_FLOOR_BAND = 3
    # How far below strong_grade the defensive floor band sits -- mirrors
    # the 5-point spacing between the other four tiers' default values.
    _FLOOR_BAND_SPREAD = 5.0
    # Sentinel returned by calculate_leverage() below every tier's floor --
    # never a real leverage value. Callers (SignalGenerationEngine) must
    # treat this as "reject the candidate", not "use 0x leverage".
    _LEVERAGE_INELIGIBLE = 0
    # calculate_risk_score()'s band floors, aligned to the three RiskConfig
    # RR tiers: below min_risk_reward scores 0 (should already be rejected
    # before this is ever displayed); min_risk_reward itself scores 60;
    # good_risk_reward scores 80; excellent_risk_reward and above scores 100.
    _SCORE_AT_MIN_TIER = 60.0
    _SCORE_AT_GOOD_TIER = 80.0
    _SCORE_AT_EXCELLENT_TIER = 100.0

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        trade_repository: Optional[TradeRepository] = None,
    ) -> None:
        self._config = config or get_config()
        self._trade_repository = trade_repository or TradeRepository()

    def assess(
        self,
        symbol: str,
        direction: SignalDirection,
        entry_price: float,
        atr: float,
        structural_target: Optional[float] = None,
        recent_move_pct: Optional[float] = None,
    ) -> RiskAssessment:
        """
        Run the full risk assessment for one candidate signal: portfolio
        limits first (cheapest check, and no point sizing a trade that
        can't be opened), then the chase-prevention guard, then SL/TP/RR/
        leverage/risk-score.

        Args:
            symbol: the candidate trade's symbol (for portfolio exposure checks).
            direction: LONG or SHORT.
            entry_price: proposed entry price.
            atr: the current ATR value (same units as price, e.g. from
                `engines.indicators.atr()`'s latest non-NaN reading).
            structural_target: nearest opposing swing level (resistance
                for a LONG, support for a SHORT), if available -- see
                module docstring for why this matters.
            recent_move_pct: signed percent price change over the recent
                lookback window, from `engines.indicators
                .recent_price_change_pct()` -- `None` (no data yet, or
                too little candle history) simply skips the guard rather
                than rejecting.

        Raises:
            RiskManagementError: `entry_price` or `atr` is not positive --
                there is no meaningful SL/TP to compute from invalid inputs.
        """
        portfolio_violation = self.check_portfolio_limits(direction, symbol)
        if portfolio_violation is not None:
            return RiskAssessment(
                approved=False, risk_score=0.0, stop_loss=0.0, take_profit_1=0.0,
                risk_reward_ratio=0.0,
                rejection_reason=portfolio_violation, detail="Portfolio risk limits reached",
            )

        if entry_price <= 0 or atr <= 0:
            raise RiskManagementError(
                "Cannot assess risk with a non-positive entry price or ATR",
                context={"symbol": symbol, "entry_price": entry_price, "atr": atr},
            )

        if recent_move_pct is not None:
            # Same-direction move only: a LONG signal is only "chasing" if
            # price already rose (positive); a SHORT signal only if price
            # already fell (negative). Movement in the OPPOSITE direction is
            # never penalized here, however large -- it reads as a reversal
            # setting up, not a chase (see module/RiskConfig docstrings).
            same_direction_move_pct = recent_move_pct if direction == SignalDirection.LONG else -recent_move_pct
            atr_pct_of_price = (atr / entry_price) * 100.0
            max_allowed_pct = self._config.risk.max_recent_move_atr_multiple * atr_pct_of_price
            if same_direction_move_pct > max_allowed_pct:
                return RiskAssessment(
                    approved=False, risk_score=0.0, stop_loss=0.0, take_profit_1=0.0,
                    risk_reward_ratio=0.0,
                    rejection_reason=RejectionReason.OVEREXTENDED_MOVE,
                    detail=(
                        f"Price already moved {same_direction_move_pct:.2f}% in the signal's direction over the "
                        f"recent window, exceeding {self._config.risk.max_recent_move_atr_multiple:.1f}x ATR "
                        f"({max_allowed_pct:.2f}%) -- looks like chasing a pump/dump rather than a fresh entry"
                    ),
                )

        stop_loss = self.calculate_stop_loss(entry_price, direction, atr)

        stop_distance_pct = abs(entry_price - stop_loss) / entry_price
        if stop_distance_pct > self._config.risk.max_stop_distance_pct:
            return RiskAssessment(
                approved=False, risk_score=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                risk_reward_ratio=0.0,
                rejection_reason=RejectionReason.HIGH_VOLATILITY,
                detail=(
                    f"Stop distance {stop_distance_pct:.2%} of entry price exceeds the "
                    f"{self._config.risk.max_stop_distance_pct:.2%} sanity cap (ATR abnormally wide)"
                ),
            )

        take_profit_1 = self.calculate_take_profits(entry_price, direction, stop_loss)

        if structural_target is not None:
            take_profit_1 = self._clamp_to_structural_target(take_profit_1, structural_target, direction)

        risk_reward_ratio = self.calculate_risk_reward_ratio(entry_price, stop_loss, take_profit_1)
        risk_score = self.calculate_risk_score(
            self._scoring_risk_reward_ratio(entry_price, stop_loss, structural_target, risk_reward_ratio)
        )

        if risk_reward_ratio < self._config.risk.min_risk_reward:
            return RiskAssessment(
                approved=False, risk_score=risk_score, stop_loss=stop_loss,
                take_profit_1=take_profit_1,
                risk_reward_ratio=risk_reward_ratio,
                rejection_reason=RejectionReason.POOR_RISK_REWARD,
                detail=f"RR {risk_reward_ratio:.2f} below minimum {self._config.risk.min_risk_reward:.2f}",
            )

        return RiskAssessment(
            approved=True, risk_score=risk_score, stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            risk_reward_ratio=risk_reward_ratio,
        )

    def _scoring_risk_reward_ratio(
        self, entry_price: float, stop_loss: float, structural_target: Optional[float], fallback: float
    ) -> float:
        """
        The RR fed to `calculate_risk_score()` -- deliberately NOT always
        the same as `assess()`'s `risk_reward_ratio` (the real traded
        TP1, the accept/reject gate value, and what gets displayed and
        persisted). That value is capped: `calculate_take_profits()`
        defines the formulaic TP1 as EXACTLY `min_risk_reward` multiples
        of the stop distance, and `_clamp_to_structural_target()` can
        only pull it CLOSER when a real level is in the way, never
        extend it further out even when the next real structural level
        is much farther away. So before this method existed, `RR fed to
        calculate_risk_score()` was -- by construction, not by market
        conditions -- almost always exactly `min_risk_reward` (raw score
        60, worth 9.3/20 after the convex rescale) for every approved
        signal regardless of whether the real structural room was barely
        past that minimum or several multiples past it: a setup with
        acres of headroom scored identically to one that just barely
        cleared the bar. Found live: dozens of different symbols
        producing the exact same risk=9.3/20 in the same scan cycle.

        Scoring against the UNCLAMPED distance to the actual next
        structural level (when one exists) reflects how much real room
        the setup has, without changing the traded TP1/stop-loss or
        the accept/reject gate at all -- both keep using the safe,
        formulaic `risk_reward_ratio` exactly as before. Falls back to
        that same value when no structural target is available (nothing
        to score against instead).
        """
        if structural_target is None:
            return fallback
        return self.calculate_risk_reward_ratio(entry_price, stop_loss, structural_target)

    def calculate_stop_loss(self, entry_price: float, direction: SignalDirection, atr: float) -> float:
        """Dynamic, ATR-based stop loss (SRS Rule 5). Never a fixed percentage of price."""
        distance = atr * self._config.risk.atr_stop_loss_multiplier
        return entry_price - distance if direction == SignalDirection.LONG else entry_price + distance

    def calculate_take_profits(
        self, entry_price: float, direction: SignalDirection, stop_loss: float
    ) -> float:
        """
        TP1 at `min_risk_reward` multiples of the ATR-derived stop
        distance -- single-TP model (TP1_HIT is a full, final close;
        there is no second target), still fully dynamic per SRS Rule 5.
        """
        risk_distance = abs(entry_price - stop_loss)
        tp1_distance = risk_distance * self._config.risk.min_risk_reward
        if direction == SignalDirection.LONG:
            return entry_price + tp1_distance
        return entry_price - tp1_distance

    def calculate_risk_reward_ratio(self, entry_price: float, stop_loss: float, take_profit: float) -> float:
        """Reward distance / risk distance for one take-profit level."""
        risk_distance = abs(entry_price - stop_loss)
        if risk_distance == 0:
            raise RiskManagementError(
                "Cannot compute risk/reward with a zero-distance stop loss",
                context={"entry_price": entry_price, "stop_loss": stop_loss},
            )
        reward_distance = abs(take_profit - entry_price)
        return reward_distance / risk_distance

    def calculate_leverage(self, confidence_score: float) -> int:
        """
        Confidence-tiered leverage suggestion, capped at 10x -- informational
        only, never sent to an exchange (see module docstring's "Leverage
        note"). Tiers are read from the active `ConfidenceConfig` grade
        bands, not fixed defaults, so leverage always matches whichever
        strategy profile produced this confidence score. Returns
        `_LEVERAGE_INELIGIBLE` (0) below every tier's floor; callers must
        treat that as "do not generate a signal", not "0x".
        """
        for floor, leverage in self._leverage_tiers():
            if confidence_score >= floor:
                return leverage
        return self._LEVERAGE_INELIGIBLE

    def _leverage_tiers(self) -> tuple[tuple[float, int], ...]:
        """Confidence-score floor -> leverage, checked top-down by `calculate_leverage()`, first match wins."""
        cfg = self._config.confidence
        return (
            (cfg.institutional_grade, self._LEVERAGE_AT_INSTITUTIONAL_GRADE),
            (cfg.excellent_grade, self._LEVERAGE_AT_EXCELLENT_GRADE),
            (cfg.very_strong_grade, self._LEVERAGE_AT_VERY_STRONG_GRADE),
            (cfg.strong_grade, self._LEVERAGE_AT_STRONG_GRADE),
            (max(0.0, cfg.strong_grade - self._FLOOR_BAND_SPREAD), self._LEVERAGE_AT_FLOOR_BAND),
        )

    def calculate_risk_score(self, risk_reward_ratio: float) -> float:
        """0-100 risk score aligned to `RiskConfig`'s three RR tiers, linearly interpolated between them."""
        cfg = self._config.risk
        if risk_reward_ratio < cfg.min_risk_reward:
            return 0.0
        if risk_reward_ratio >= cfg.excellent_risk_reward:
            return self._SCORE_AT_EXCELLENT_TIER

        if risk_reward_ratio < cfg.good_risk_reward:
            band_width = cfg.good_risk_reward - cfg.min_risk_reward
            if band_width <= 0:
                return self._SCORE_AT_MIN_TIER
            fraction = (risk_reward_ratio - cfg.min_risk_reward) / band_width
            return self._SCORE_AT_MIN_TIER + fraction * (self._SCORE_AT_GOOD_TIER - self._SCORE_AT_MIN_TIER)

        band_width = cfg.excellent_risk_reward - cfg.good_risk_reward
        if band_width <= 0:
            return self._SCORE_AT_GOOD_TIER
        fraction = (risk_reward_ratio - cfg.good_risk_reward) / band_width
        return self._SCORE_AT_GOOD_TIER + fraction * (self._SCORE_AT_EXCELLENT_TIER - self._SCORE_AT_GOOD_TIER)

    def check_portfolio_limits(self, direction: SignalDirection, symbol: str) -> Optional[RejectionReason]:
        """
        Enforce `RiskConfig`'s portfolio-level exposure caps (SRS Part 17)
        against currently active trades.

        Note: `max_same_sector_exposure` is NOT enforced here -- no sector
        classification data source exists anywhere in this codebase yet
        (`CoinProfile`/`CoinClassification` classify quality, not sector).
        This is a documented, deliberate gap rather than a guessed mapping;
        wiring it in is future work once a sector data source exists.

        Returns:
            The first violated limit's `RejectionReason`, or None if the
            candidate trade is within every enforced limit.
        """
        cfg = self._config.risk
        active_trades: list[Trade] = self._trade_repository.get_active_trades()

        if len(active_trades) >= cfg.max_active_trades:
            return RejectionReason.PORTFOLIO_RISK_LIMIT

        direction_cap = cfg.max_long_trades if direction == SignalDirection.LONG else cfg.max_short_trades
        direction_count = sum(1 for t in active_trades if t.direction == direction)
        if direction_count >= direction_cap:
            return RejectionReason.PORTFOLIO_RISK_LIMIT

        same_symbol_count = sum(1 for t in active_trades if t.symbol == symbol)
        if same_symbol_count >= cfg.max_same_coin_exposure:
            return RejectionReason.CORRELATION_RISK

        return None

    @staticmethod
    def _clamp_to_structural_target(take_profit_1: float, structural_target: float, direction: SignalDirection) -> float:
        """Never project TP1 further than the nearest real structural level (see module docstring)."""
        if direction == SignalDirection.LONG:
            return min(take_profit_1, structural_target)
        return max(take_profit_1, structural_target)
