"""
engines/confidence.py

Confidence Engine (SRS Part 9 CONFIDENCE ENGINE / EXPLAINABLE DECISION):
combines Bitcoin Intelligence, Market Health, Market Structure, Risk
Management, and Coin Trust into one final 0-100 confidence score for a
candidate signal, broken down into named point-contributions so every
decision is explainable after the fact (SRS Part 9's own example:
"Trend +18, Structure +15, ... Final Confidence 92").

This engine is a PURE aggregator: it takes already-computed outputs from
the five upstream engines as plain data (a `MarketStructureResult`, a
`RiskAssessment`, and three 0-100 floats), never the engine instances or
their API clients. Calling each upstream engine in the right order is the
future Scanner Orchestrator's job; keeping Confidence decoupled from how
those numbers were produced is what makes it trivially unit-testable
without a database or a Binance client.

Point allocation (sums to 100 -- no `ConfidenceConfig` field defines a
per-category split, since that config only holds the final grade bands,
so this split is a documented local constant, same pattern as
`engines.coin_trust`'s profile/track-record weighting):
    Trend          17.00  (engines.market_structure bias + BOS/CHoCH)
    Structure      12.75  (order blocks / FVGs / liquidity sweeps present)
    Risk           17.00  (RiskAssessment.risk_score, linearly rescaled)
    Bitcoin        12.75  (BitcoinIntelligenceEngine.score_for_direction())
    Coin Trust     12.75  (CoinTrustEngine trust_score)
    Market Health  12.75  (MarketHealthEngine health_score)
    Smart Money    15.00  (SmartMoneyEngine.score_for_direction())

Smart Money category (Module 23, added at the platform owner's explicit
request -- prior audit had reviewed and deliberately deferred exactly
this input, see PROJECT_STATUS.md's "extra indicators reviewed but not
added" note): the other six categories above were rescaled DOWN by a
uniform 15% (0.85x each, preserving their relative priority against one
another exactly) to free the 15 points this category enters at -- the
same weight class as Bitcoin/Coin Trust/Market Health, since all four are
"broader context beyond this coin's own 15m chart" signals blended via
the same `_score_linear` pattern, rather than a dominant/veto input. See
`engines.smart_money` for what feeds this score and why it is a
gradual-suppression input (matching Bitcoin Intelligence's own stated
"gradual, not a hard veto" philosophy) rather than a hard filter.

Unlike the SRS's illustrative example (which included a separate
subtractive "Penalty" line), this implementation achieves the same
effect by capping each component at its own max_points rather than
adding a penalty term afterward -- there is nothing left to subtract
from once every component is already bounded, and a separate penalty
term risks double-counting whatever it was meant to penalize.

Recalibration note (per the platform owner's explicit instruction: "very
high confidence should only be given to exceptional setups, confidence
should reflect actual trade quality"): `_score_linear()` -- the four
upstream 0-100 scores (risk, bitcoin, coin_trust, market_health) that
arrive already-computed from other engines -- no longer rescales those
proportionally. A flat proportional rescale gives a merely-adequate
input (e.g. an upstream score that just barely clears ITS OWN engine's
threshold) the same SHARE of credit as a middling input gets of a
middling grade, which is generous rather than discriminating: "adequate"
and "exceptional" end up close together in points. `_score_linear()` now
raises the normalized input to `_RESCALE_EXPONENT` (1.5) before applying
the max_points share, a deliberately modest convex curve: a perfect
upstream score (100) still earns the full max_points (unchanged ceiling
-- institutional-grade signals remain reachable), but a merely-passing
score earns visibly less than it used to, and the gap widens the further
below 100 an input sits. Concretely, an input of 80/100 now earns ~72%
of max_points instead of 80%; an input of 60/100 earns ~46% instead of
60%. `structure` (one of the two categories NOT computed via
`_score_linear`) is untouched by this change -- its scoring is
tiered/additive over discrete, already-meaningful market-structure
facts (a present order block, a present FVG), not a continuous upstream
score, so "generous vs. discriminating" does not apply to it the same
way, and changing it was not part of what was asked.

Momentum/volume confirmation note: RSI/MACD/ADX/volume_ratio (all
already computed by `engines.indicators`, previously never consumed by
any engine -- see git history / PROJECT_STATUS.md) now adjust `trend`'s
score as a bounded +/-15% multiplier, via `_momentum_alignment()`: each
of the four signals casts one vote (confirms the candidate direction,
opposes it, or neutral), and the average vote scales the trend fraction
that market-structure alone already earned. This is a modifier on an
existing category, not a new one -- the 100-point split above is
unchanged -- and it is asymmetric by construction: `min(1.0, ...)` means
momentum confirmation can never push a category above what its own
max_points allows (a BOS is still the ceiling), but momentum divergence
CAN pull an otherwise-strong structural read down (e.g. a fresh BOS
against weakening/opposing momentum -- a classic false-breakout
warning). `momentum` is an optional `assess()` parameter (`None` = treat
as neutral, multiplier 1.0) so this degrades safely if a caller has no
indicator data for some symbol/tick, and every existing caller that
predates this feature keeps working unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import ConfidenceGrade, RejectionReason, SignalDirection
from engines.market_structure import MarketStructureResult, StructureBias, StructureEventType
from engines.risk_management import RiskAssessment


@dataclass(frozen=True)
class ConfidenceComponent:
    """One named category's contribution to the final confidence score."""

    category: str
    points: float
    max_points: float
    detail: str


@dataclass(frozen=True)
class MomentumSnapshot:
    """
    The latest reading of each momentum/volume indicator `engines
    .indicators` already computes, bundled for `ConfidenceEngine.assess()`.
    All five are plain already-computed floats (the caller's job -- e.g.
    `ScannerOrchestrator` -- is pulling the last non-NaN value out of each
    indicator's series; this engine stays decoupled from candles/Binance,
    same reasoning as `MarketStructureResult`/`RiskAssessment` above).
    """

    rsi: float
    macd_histogram: float
    plus_di: float
    minus_di: float
    adx: float
    volume_ratio: float


@dataclass(frozen=True)
class ConfidenceAssessment:
    """Everything `ConfidenceEngine.assess()` produces for one candidate signal."""

    symbol: str
    direction: SignalDirection
    confidence_score: float
    grade: ConfidenceGrade
    approved: bool
    components: tuple[ConfidenceComponent, ...]
    rejection_reason: Optional[RejectionReason] = None


class ConfidenceEngine:
    """Blends five upstream engines' outputs into one explainable 0-100 confidence score."""

    _TREND_MAX_POINTS = 17.0
    _STRUCTURE_MAX_POINTS = 12.75
    _RISK_MAX_POINTS = 17.0
    _BITCOIN_MAX_POINTS = 12.75
    _COIN_TRUST_MAX_POINTS = 12.75
    _MARKET_HEALTH_MAX_POINTS = 12.75
    _SMART_MONEY_MAX_POINTS = 15.0

    # Public mirror of the 7 constants just above, keyed by the same
    # `category` string `SignalScoreComponent`/`get_score_breakdown()`
    # persist -- for external consumers (Telegram's confirmation
    # checklist, engines/telegram_notifications.py) that need "what's
    # this category's max weight" without poking at the private
    # per-category constants directly. Single source of truth: this
    # references the constants above rather than restating the numbers,
    # so a future rebalance can't update one and silently miss the other.
    CATEGORY_MAX_POINTS: dict[str, float] = {
        "trend": _TREND_MAX_POINTS,
        "structure": _STRUCTURE_MAX_POINTS,
        "risk": _RISK_MAX_POINTS,
        "bitcoin": _BITCOIN_MAX_POINTS,
        "coin_trust": _COIN_TRUST_MAX_POINTS,
        "market_health": _MARKET_HEALTH_MAX_POINTS,
        "smart_money": _SMART_MONEY_MAX_POINTS,
    }
    # Each of order-block/FVG/liquidity-sweep presence is worth this many of
    # the 12.75 Structure points (3 elements * 4.25 = the full category --
    # rescaled from the pre-Smart-Money 5.0/15.0 by the same 0.85 factor as
    # _STRUCTURE_MAX_POINTS itself; missing this rescale here specifically
    # was caught by test_assess_perfect_scenario_scores_exactly_100 going
    # to 102.25 instead of 100 -- structure alone was still awarding the
    # OLD 15.0 ceiling against the NEW 12.75 max_points).
    _STRUCTURE_ELEMENT_POINTS = 4.25
    # Convexity applied by _score_linear() -- see module docstring's
    # "Recalibration note". 1.0 would be the old flat/proportional
    # behavior; deliberately modest (not e.g. 3+) so a genuinely strong
    # setup can still reach INSTITUTIONAL_GRADE, not just a mathematically
    # perfect one.
    _RESCALE_EXPONENT = 1.5

    # Momentum-confirmation adjustment to `trend` -- see module
    # docstring's "Momentum/volume confirmation note".
    # RSI neutral band: within 5 points of the 50 midpoint counts as "no
    # lean" rather than letting tiny fluctuations flip a vote.
    _MOMENTUM_RSI_NEUTRAL_BAND = 5.0
    # ADX below this is "no real trend either way" (standard convention:
    # ADX > 25 is a widely-used "trending" threshold; 20 is used here as
    # the point below which +DI/-DI's relative order is too noisy to
    # trust) -- the DI vote is neutral, not counted against the signal.
    _MOMENTUM_ADX_TREND_THRESHOLD = 20.0
    # Volume clearly above/below average -- see _momentum_alignment().
    _MOMENTUM_VOLUME_HIGH = 1.2
    _MOMENTUM_VOLUME_LOW = 0.7
    # The four votes' average (-1.0..+1.0) scales the trend fraction by
    # at most this much either way -- deliberately modest: momentum
    # confirms or casts doubt on what market structure already found, it
    # does not replace it (structure remains the primary trend signal).
    _MOMENTUM_MULTIPLIER_SWING = 0.15

    def __init__(self, config: Optional[PlatformConfig] = None) -> None:
        self._config = config or get_config()

    def assess(
        self,
        symbol: str,
        direction: SignalDirection,
        structure: MarketStructureResult,
        risk: RiskAssessment,
        bitcoin_score: float,
        market_health_score: float,
        coin_trust_score: float,
        smart_money_score: float,
        momentum: Optional[MomentumSnapshot] = None,
    ) -> ConfidenceAssessment:
        """
        Combine every upstream input into one confidence assessment.

        Args:
            symbol: the candidate signal's symbol.
            direction: LONG or SHORT.
            structure: `MarketStructureEngine.analyze()`'s result for this symbol/timeframe.
            risk: `RiskManagementEngine.assess()`'s result for this candidate.
                If `risk.approved` is False, this method short-circuits to a
                REJECTED assessment carrying `risk.rejection_reason` --
                there is no point scoring a trade that can't be opened.
            bitcoin_score: `BitcoinIntelligenceEngine.score_for_direction()`'s 0-100 output.
            market_health_score: `MarketHealthEngine`'s latest `market_health_score`.
            coin_trust_score: `CoinTrustEngine.analyze()`'s 0-100 `trust_score`.
            smart_money_score: `SmartMoneyEngine.score_for_direction()`'s 0-100 output
                (Module 23 -- Binance's official Top Trader Long/Short Ratio,
                account+position, weighed toward or against this candidate's
                direction). Required, not optional, like the three peer
                context scores above -- see `engines.smart_money` for how a
                caller with no data for this symbol still gets a real
                (neutral, not fabricated-supportive) 50.0 from that engine.
            momentum: latest RSI/MACD/ADX/volume_ratio reading, or `None`
                to skip the momentum-confirmation adjustment entirely
                (treated as neutral -- see module docstring).
        """
        if not risk.approved:
            return ConfidenceAssessment(
                symbol=symbol, direction=direction, confidence_score=0.0,
                grade=ConfidenceGrade.REJECTED, approved=False, components=(),
                rejection_reason=risk.rejection_reason,
            )

        components = (
            self._score_trend(structure, direction, momentum),
            self._score_structure(structure, direction),
            self._score_linear("risk", risk.risk_score, self._RISK_MAX_POINTS, f"risk_score={risk.risk_score:.1f}, RR={risk.risk_reward_ratio:.2f}"),
            self._score_linear("bitcoin", bitcoin_score, self._BITCOIN_MAX_POINTS, f"bitcoin_score={bitcoin_score:.1f}"),
            self._score_linear("coin_trust", coin_trust_score, self._COIN_TRUST_MAX_POINTS, f"coin_trust_score={coin_trust_score:.1f}"),
            self._score_linear("market_health", market_health_score, self._MARKET_HEALTH_MAX_POINTS, f"market_health_score={market_health_score:.1f}"),
            self._score_linear("smart_money", smart_money_score, self._SMART_MONEY_MAX_POINTS, f"smart_money_score={smart_money_score:.1f}"),
        )

        confidence_score = max(0.0, min(100.0, sum(c.points for c in components)))
        cfg = self._config.confidence

        if confidence_score < cfg.minimum_confidence:
            return ConfidenceAssessment(
                symbol=symbol, direction=direction, confidence_score=round(confidence_score, 2),
                grade=ConfidenceGrade.REJECTED, approved=False, components=components,
                rejection_reason=RejectionReason.LOW_CONFIDENCE,
            )

        return ConfidenceAssessment(
            symbol=symbol, direction=direction, confidence_score=round(confidence_score, 2),
            grade=self._grade_for(confidence_score, cfg), approved=True, components=components,
        )

    @classmethod
    def _score_linear(cls, category: str, score_0_to_100: float, max_points: float, detail: str) -> ConfidenceComponent:
        """
        Rescale an already-0-100 upstream score into its share of
        `max_points`, via a convex curve rather than a flat proportion --
        see module docstring's "Recalibration note". A perfect input (100)
        still earns exactly `max_points`; anything less earns
        disproportionately fewer points the further it sits from 100.
        """
        clamped = max(0.0, min(100.0, score_0_to_100))
        normalized = clamped / 100.0
        points = (normalized ** cls._RESCALE_EXPONENT) * max_points
        return ConfidenceComponent(category=category, points=points, max_points=max_points, detail=detail)

    def _score_trend(
        self, structure: MarketStructureResult, direction: SignalDirection, momentum: Optional[MomentumSnapshot]
    ) -> ConfidenceComponent:
        candidate_bias = StructureBias.BULLISH if direction == SignalDirection.LONG else StructureBias.BEARISH
        matching_bos = (
            StructureEventType.BOS_BULLISH if candidate_bias == StructureBias.BULLISH else StructureEventType.BOS_BEARISH
        )
        matching_choch = (
            StructureEventType.CHOCH_BULLISH if candidate_bias == StructureBias.BULLISH else StructureEventType.CHOCH_BEARISH
        )

        if any(e.event_type == matching_bos for e in structure.events):
            fraction, detail = 1.0, "BOS confirms continuation in the signal's direction"
        elif any(e.event_type == matching_choch for e in structure.events):
            fraction, detail = 0.8, "CHoCH reversal into the signal's direction"
        elif structure.bias == candidate_bias:
            fraction, detail = 0.6, "established structural bias matches the signal's direction"
        elif structure.bias == StructureBias.UNCLEAR:
            fraction, detail = 0.4, "structural bias is unclear"
        else:
            fraction, detail = 0.1, "structural bias opposes the signal's direction"

        if momentum is not None:
            vote_average, momentum_detail = self._momentum_alignment(momentum, direction)
            multiplier = 1.0 + (vote_average * self._MOMENTUM_MULTIPLIER_SWING)
            fraction = min(1.0, fraction * multiplier)
            detail = f"{detail}; {momentum_detail}"

        return ConfidenceComponent(
            category="trend", points=fraction * self._TREND_MAX_POINTS, max_points=self._TREND_MAX_POINTS, detail=detail
        )

    def _momentum_alignment(self, momentum: MomentumSnapshot, direction: SignalDirection) -> tuple[float, str]:
        """
        Four independent +1 (confirms) / 0 (neutral) / -1 (opposes) votes,
        averaged to -1.0..+1.0 -- see module docstring's "Momentum/volume
        confirmation note". Kept as simple sign/threshold checks on
        values `engines.indicators` already computed, not a second
        formula reinventing what Market Structure or Risk Management
        already do; RSI/MACD read the candidate's OWN direction, ADX/DI
        reads whether a real trend exists at all, and volume reads
        conviction (high or low, regardless of direction) rather than a
        directional lean of its own.
        """
        is_long = direction == SignalDirection.LONG
        votes: list[float] = []
        notes: list[str] = []

        rsi_bullish = momentum.rsi >= 50.0 + self._MOMENTUM_RSI_NEUTRAL_BAND
        rsi_bearish = momentum.rsi <= 50.0 - self._MOMENTUM_RSI_NEUTRAL_BAND
        if rsi_bullish or rsi_bearish:
            rsi_confirms = rsi_bullish if is_long else rsi_bearish
            votes.append(1.0 if rsi_confirms else -1.0)
            notes.append(f"RSI {'confirms' if rsi_confirms else 'opposes'} ({momentum.rsi:.0f})")
        else:
            votes.append(0.0)

        if not math.isnan(momentum.macd_histogram) and momentum.macd_histogram != 0.0:
            macd_bullish = momentum.macd_histogram > 0.0
            macd_confirms = macd_bullish if is_long else not macd_bullish
            votes.append(1.0 if macd_confirms else -1.0)
            notes.append(f"MACD {'confirms' if macd_confirms else 'opposes'}")
        else:
            votes.append(0.0)

        if momentum.adx >= self._MOMENTUM_ADX_TREND_THRESHOLD and momentum.plus_di != momentum.minus_di:
            di_bullish = momentum.plus_di > momentum.minus_di
            di_confirms = di_bullish if is_long else not di_bullish
            votes.append(1.0 if di_confirms else -1.0)
            notes.append(f"ADX {'confirms' if di_confirms else 'opposes'} ({momentum.adx:.0f})")
        else:
            votes.append(0.0)

        if momentum.volume_ratio >= self._MOMENTUM_VOLUME_HIGH:
            votes.append(1.0)
            notes.append(f"volume supports ({momentum.volume_ratio:.1f}x avg)")
        elif momentum.volume_ratio <= self._MOMENTUM_VOLUME_LOW:
            votes.append(-1.0)
            notes.append(f"volume thin ({momentum.volume_ratio:.1f}x avg, breakout risk)")
        else:
            votes.append(0.0)

        average = sum(votes) / len(votes)
        summary = ", ".join(notes) if notes else "momentum neutral"
        return average, f"momentum: {summary}"

    def _score_structure(self, structure: MarketStructureResult, direction: SignalDirection) -> ConfidenceComponent:
        candidate_bias = StructureBias.BULLISH if direction == SignalDirection.LONG else StructureBias.BEARISH
        points = 0.0
        matched_elements: list[str] = []

        if any(ob.direction == candidate_bias for ob in structure.order_blocks):
            points += self._STRUCTURE_ELEMENT_POINTS
            matched_elements.append("order block")
        if any(fvg.direction == candidate_bias for fvg in structure.fair_value_gaps):
            points += self._STRUCTURE_ELEMENT_POINTS
            matched_elements.append("fair value gap")
        if any(sweep.direction == candidate_bias for sweep in structure.liquidity_sweeps):
            points += self._STRUCTURE_ELEMENT_POINTS
            matched_elements.append("liquidity sweep")

        detail = f"supporting elements: {', '.join(matched_elements) if matched_elements else 'none'}"
        return ConfidenceComponent(category="structure", points=points, max_points=self._STRUCTURE_MAX_POINTS, detail=detail)

    @staticmethod
    def _grade_for(score: float, cfg) -> ConfidenceGrade:
        if score >= cfg.institutional_grade:
            return ConfidenceGrade.INSTITUTIONAL_GRADE
        if score >= cfg.excellent_grade:
            return ConfidenceGrade.EXCELLENT
        if score >= cfg.very_strong_grade:
            return ConfidenceGrade.VERY_STRONG
        return ConfidenceGrade.STRONG
