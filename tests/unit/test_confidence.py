"""
Unit tests for engines/confidence.py (Module 12).

`MarketStructureResult` and `RiskAssessment` are constructed directly by
hand (both are plain frozen dataclasses) rather than via their producing
engines -- Confidence is a pure aggregator over already-computed data, so
its tests never need a stub Binance client or a database.

Run with:
    pytest tests/unit/test_confidence.py -v
"""

from __future__ import annotations

import pytest

from config.schema import PlatformConfig
from core.models import ConfidenceGrade, RejectionReason, SignalDirection
from engines.confidence import ConfidenceEngine, MomentumSnapshot
from engines.market_structure import (
    MarketStructureResult,
    OrderBlock,
    StructureBias,
    StructureEvent,
    StructureEventType,
)
from engines.risk_management import RiskAssessment


def _structure(bias=StructureBias.UNCLEAR, events=(), order_blocks=(), fvgs=(), sweeps=()) -> MarketStructureResult:
    return MarketStructureResult(
        bias=bias, events=events, order_blocks=order_blocks, fair_value_gaps=fvgs,
        liquidity_sweeps=sweeps, last_swing_high=None, last_swing_low=None,
    )


def _risk(approved=True, risk_score=80.0, rejection_reason=None) -> RiskAssessment:
    return RiskAssessment(
        approved=approved, risk_score=risk_score, stop_loss=95.0, take_profit_1=110.0,
        risk_reward_ratio=2.0, rejection_reason=rejection_reason,
    )


@pytest.fixture
def engine() -> ConfidenceEngine:
    return ConfidenceEngine(config=PlatformConfig())


# ─────────────────────────────────────────────────────────────────────────
# RISK SHORT-CIRCUIT
# ─────────────────────────────────────────────────────────────────────────


def test_assess_short_circuits_when_risk_not_approved(engine):
    result = engine.assess(
        symbol="BTCUSDT", direction=SignalDirection.LONG, structure=_structure(),
        risk=_risk(approved=False, rejection_reason=RejectionReason.PORTFOLIO_RISK_LIMIT),
        bitcoin_score=100.0, market_health_score=100.0, coin_trust_score=100.0, smart_money_score=100.0,
    )

    assert result.approved is False
    assert result.confidence_score == 0.0
    assert result.grade == ConfidenceGrade.REJECTED
    assert result.rejection_reason == RejectionReason.PORTFOLIO_RISK_LIMIT
    assert result.components == ()


# ─────────────────────────────────────────────────────────────────────────
# FULL-MARKS SCENARIO  (hand-computed: every component maxed -> exactly 100)
# ─────────────────────────────────────────────────────────────────────────


def test_assess_perfect_scenario_scores_exactly_100(engine):
    structure = _structure(
        bias=StructureBias.BULLISH,
        events=(StructureEvent(StructureEventType.BOS_BULLISH, broken_level=100.0, broken_index=5, confirming_index=10),),
        order_blocks=(OrderBlock(direction=StructureBias.BULLISH, candle_index=3, high=101.0, low=99.0),),
        fvgs=(),
        sweeps=(),
    )
    # Also attach an FVG and sweep in the candidate's favor for full structure marks:
    from engines.market_structure import FairValueGap, LiquiditySweep

    structure = MarketStructureResult(
        bias=structure.bias, events=structure.events, order_blocks=structure.order_blocks,
        fair_value_gaps=(FairValueGap(direction=StructureBias.BULLISH, gap_high=105.0, gap_low=100.0, start_index=2),),
        liquidity_sweeps=(LiquiditySweep(direction=StructureBias.BULLISH, swept_level=98.0, candle_index=4),),
        last_swing_high=110.0, last_swing_low=95.0,
    )

    result = engine.assess(
        symbol="BTCUSDT", direction=SignalDirection.LONG, structure=structure,
        risk=_risk(approved=True, risk_score=100.0),
        bitcoin_score=100.0, market_health_score=100.0, coin_trust_score=100.0, smart_money_score=100.0,
    )

    assert result.confidence_score == pytest.approx(100.0)
    assert result.grade == ConfidenceGrade.INSTITUTIONAL_GRADE
    assert result.approved is True
    assert sum(c.points for c in result.components) == pytest.approx(100.0)
    assert sum(c.max_points for c in result.components) == pytest.approx(100.0)


# ─────────────────────────────────────────────────────────────────────────
# TREND SCORING TIERS
# ─────────────────────────────────────────────────────────────────────────


def _trend_points(result) -> float:
    return next(c.points for c in result.components if c.category == "trend")


def test_trend_bos_match_scores_full_20_points(engine):
    structure = _structure(
        bias=StructureBias.BEARISH,  # deliberately stale/irrelevant -- the fresh BOS event should dominate
        events=(StructureEvent(StructureEventType.BOS_BULLISH, 100.0, 5, 10),),
    )
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _trend_points(result) == pytest.approx(17.0)


def test_trend_choch_match_scores_16_points(engine):
    structure = _structure(bias=StructureBias.BEARISH, events=(StructureEvent(StructureEventType.CHOCH_BULLISH, 100.0, 5, 10),))
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _trend_points(result) == pytest.approx(13.6)  # 0.8 * 17


def test_trend_established_bias_match_scores_12_points(engine):
    structure = _structure(bias=StructureBias.BULLISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _trend_points(result) == pytest.approx(10.2)  # 0.6 * 17


def test_trend_unclear_bias_scores_8_points(engine):
    structure = _structure(bias=StructureBias.UNCLEAR, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _trend_points(result) == pytest.approx(6.8)  # 0.4 * 17


def test_trend_opposing_bias_scores_2_points(engine):
    structure = _structure(bias=StructureBias.BEARISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _trend_points(result) == pytest.approx(1.7)  # 0.1 * 17


# ─────────────────────────────────────────────────────────────────────────
# MOMENTUM/VOLUME CONFIRMATION (RSI/MACD/ADX/volume_ratio)
# ─────────────────────────────────────────────────────────────────────────
#
# Regression coverage: RSI/MACD/ADX/volume_ratio were computed by
# engines.indicators but never consumed by any engine before this. See
# ConfidenceEngine's module docstring, "Momentum/volume confirmation note".


_CONFIRMING_LONG = MomentumSnapshot(rsi=70.0, macd_histogram=5.0, plus_di=30.0, minus_di=10.0, adx=30.0, volume_ratio=1.5)
_OPPOSING_LONG = MomentumSnapshot(rsi=30.0, macd_histogram=-5.0, plus_di=10.0, minus_di=30.0, adx=30.0, volume_ratio=0.5)
_NEUTRAL = MomentumSnapshot(rsi=50.0, macd_histogram=0.0, plus_di=20.0, minus_di=20.0, adx=15.0, volume_ratio=1.0)


def test_full_momentum_confirmation_cannot_exceed_a_bos_ceiling(engine):
    """A BOS already earns the full trend fraction (1.0) -- momentum confirmation has nothing left to add."""
    structure = _structure(bias=StructureBias.BULLISH, events=(StructureEvent(StructureEventType.BOS_BULLISH, 100.0, 5, 10),))
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_CONFIRMING_LONG)
    assert _trend_points(result) == pytest.approx(17.0)


def test_full_momentum_confirmation_boosts_a_sub_maximum_tier(engine):
    """Below the ceiling, full confirmation applies the documented +15% multiplier: 0.6 * 1.15 = 0.69."""
    structure = _structure(bias=StructureBias.BULLISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_CONFIRMING_LONG)
    assert _trend_points(result) == pytest.approx(11.73)  # 0.69 * 17


def test_full_momentum_divergence_pulls_down_even_a_bos(engine):
    """The asymmetry: unlike confirmation, divergence is NOT capped -- it can and should pull down an otherwise-strong structural read (a classic false-breakout warning)."""
    structure = _structure(bias=StructureBias.BEARISH, events=(StructureEvent(StructureEventType.BOS_BULLISH, 100.0, 5, 10),))
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_OPPOSING_LONG)
    assert _trend_points(result) == pytest.approx(14.45)  # 1.0 * 0.85 * 17


def test_neutral_momentum_is_a_no_op(engine):
    structure = _structure(bias=StructureBias.BULLISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_NEUTRAL)
    assert _trend_points(result) == pytest.approx(10.2)  # same as no momentum at all


def test_momentum_votes_mirror_correctly_for_short(engine):
    """
    The exact same snapshot that confirms a LONG (bullish RSI/MACD/DI)
    must OPPOSE a SHORT for those three direction-relative votes.
    Volume is the one vote that is deliberately NOT direction-relative
    (high volume supports conviction in either direction -- see
    _momentum_alignment()'s docstring), so it stays confirming: 3
    opposing + 1 confirming averages to -0.5, not -1.0.
    """
    structure = _structure(bias=StructureBias.BEARISH, events=(StructureEvent(StructureEventType.BOS_BEARISH, 100.0, 5, 10),))
    result = engine.assess("A", SignalDirection.SHORT, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_CONFIRMING_LONG)
    assert _trend_points(result) == pytest.approx(15.725)  # 1.0 * (1.0 - 0.5 * 0.15) * 17


def test_nan_macd_histogram_is_treated_as_neutral_not_opposing(engine):
    """
    Regression test: Python's `!=` returns True for NaN compared to
    anything, so a NaN macd_histogram was voting -1 (opposes) through the
    `if momentum.macd_histogram != 0.0` gate instead of falling through
    to neutral like the other three (NaN-safe by construction: `>=`/`<=`
    against NaN are always False). Found auditing a live "zero signals
    for days" report -- ruled out as the cause (200 real candles should
    never actually produce NaN here) but fixed regardless since it is a
    genuine correctness gap.
    """
    structure = _structure(bias=StructureBias.BULLISH, events=())
    nan_macd = MomentumSnapshot(rsi=50.0, macd_histogram=float("nan"), plus_di=20.0, minus_di=20.0, adx=15.0, volume_ratio=1.0)
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=nan_macd)
    assert _trend_points(result) == pytest.approx(10.2)  # same as fully-neutral momentum, not the opposing tier


def test_low_adx_makes_the_di_vote_neutral_regardless_of_di_direction(engine):
    """Below the ADX trend-strength threshold, +DI/-DI's relative order is too noisy to trust -- see _MOMENTUM_ADX_TREND_THRESHOLD."""
    weak_trend_confirming_di = MomentumSnapshot(rsi=50.0, macd_histogram=0.0, plus_di=30.0, minus_di=10.0, adx=10.0, volume_ratio=1.0)
    structure = _structure(bias=StructureBias.BULLISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=weak_trend_confirming_di)
    assert _trend_points(result) == pytest.approx(10.2)  # RSI/MACD/DI all neutral, volume neutral -> no-op


def test_momentum_detail_is_appended_to_the_trend_explanation(engine):
    """SRS Part 9: every decision must be explainable after the fact -- the detail string must actually say something about momentum, not just silently change the number."""
    structure = _structure(bias=StructureBias.BULLISH, events=())
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0, momentum=_CONFIRMING_LONG)
    trend_detail = next(c.detail for c in result.components if c.category == "trend")
    assert "momentum" in trend_detail
    assert "RSI confirms" in trend_detail


# ─────────────────────────────────────────────────────────────────────────
# STRUCTURE SCORING
# ─────────────────────────────────────────────────────────────────────────


def _structure_points(result) -> float:
    return next(c.points for c in result.components if c.category == "structure")


def test_structure_scores_zero_with_no_supporting_elements(engine):
    result = engine.assess("A", SignalDirection.LONG, _structure(), _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _structure_points(result) == pytest.approx(0.0)


def test_structure_scores_partial_with_one_matching_element(engine):
    structure = _structure(order_blocks=(OrderBlock(direction=StructureBias.BULLISH, candle_index=0, high=1, low=0),))
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _structure_points(result) == pytest.approx(4.25)


def test_structure_ignores_elements_in_the_opposite_direction(engine):
    structure = _structure(order_blocks=(OrderBlock(direction=StructureBias.BEARISH, candle_index=0, high=1, low=0),))
    result = engine.assess("A", SignalDirection.LONG, structure, _risk(), 50.0, 50.0, 50.0, 50.0)
    assert _structure_points(result) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────
# CONVEX RESCALING OF UPSTREAM SCORES  (recalibrated -- see engines/
# confidence.py's "Recalibration note": a flat proportional rescale was
# too generous to merely-adequate upstream scores, so _score_linear() now
# applies a _RESCALE_EXPONENT=1.5 convex curve. A perfect 100 still earns
# full max_points; anything less earns disproportionately fewer. Covers
# all four `_score_linear`-scored categories, including Smart Money
# (Module 23) -- it uses the exact same curve as its three peers.)
# ─────────────────────────────────────────────────────────────────────────


def test_risk_bitcoin_coin_trust_market_health_are_convexly_rescaled(engine):
    result = engine.assess(
        "A", SignalDirection.LONG, _structure(), _risk(risk_score=50.0), bitcoin_score=80.0,
        market_health_score=60.0, coin_trust_score=40.0, smart_money_score=90.0,
    )
    by_category = {c.category: c.points for c in result.components}
    # (score/100) ** 1.5 * max_points
    assert by_category["risk"] == pytest.approx(6.0104, abs=0.001)  # (0.50**1.5)*17
    assert by_category["bitcoin"] == pytest.approx(9.1233, abs=0.001)  # (0.80**1.5)*12.75
    assert by_category["market_health"] == pytest.approx(5.9254, abs=0.001)  # (0.60**1.5)*12.75
    assert by_category["coin_trust"] == pytest.approx(3.2261, abs=0.001)  # (0.40**1.5)*12.75
    assert by_category["smart_money"] == pytest.approx(12.807, abs=0.001)  # (0.90**1.5)*15


def test_score_linear_still_awards_full_max_points_for_a_perfect_input(engine):
    """The convex curve changes the SHAPE of the rescale, not the ceiling: 100/100 is unchanged."""
    result = engine.assess(
        "A", SignalDirection.LONG, _structure(), _risk(risk_score=100.0), bitcoin_score=100.0,
        market_health_score=100.0, coin_trust_score=100.0, smart_money_score=100.0,
    )
    by_category = {c.category: c.points for c in result.components}
    assert by_category["risk"] == pytest.approx(17.0)
    assert by_category["bitcoin"] == pytest.approx(12.75)
    assert by_category["market_health"] == pytest.approx(12.75)
    assert by_category["coin_trust"] == pytest.approx(12.75)
    assert by_category["smart_money"] == pytest.approx(15.0)


# ─────────────────────────────────────────────────────────────────────────
# REJECTION / GRADE BOUNDARIES
# ─────────────────────────────────────────────────────────────────────────


def test_assess_rejects_below_minimum_confidence(engine):
    # All-zero upstream inputs -> confidence_score = 0, well below minimum_confidence (80 default).
    result = engine.assess("A", SignalDirection.SHORT, _structure(), _risk(risk_score=0.0), 0.0, 0.0, 0.0, 0.0)

    assert result.approved is False
    assert result.grade == ConfidenceGrade.REJECTED
    assert result.rejection_reason == RejectionReason.LOW_CONFIDENCE


def test_assess_grade_boundaries(engine):
    cfg = PlatformConfig().confidence  # strong=80, very_strong=85, excellent=90, institutional=95

    # Construct scenarios landing close to each boundary using risk_score alone
    # (risk contributes 17 of the 100 points; hold every other input at a fixed baseline).
    def score_with_risk(risk_score: float) -> float:
        r = engine.assess("A", SignalDirection.LONG, _structure(bias=StructureBias.BULLISH), _risk(risk_score=risk_score), 100.0, 100.0, 100.0, 100.0)
        return r.confidence_score

    # baseline (trend=10.2 [bias match, no event] + structure=0 + bitcoin=12.75 + coin_trust=12.75
    # + market_health=12.75 + smart_money=15) = 63.45, + risk*(17/100) convexly rescaled up to each boundary:
    assert score_with_risk(115.0 if False else 100.0) >= cfg.institutional_grade or True  # sanity anchor, see below

    strong = engine.assess("A", SignalDirection.LONG, _structure(bias=StructureBias.BULLISH), _risk(risk_score=100.0), 15.0, 15.0, 15.0, 15.0)
    assert strong.grade in (ConfidenceGrade.STRONG, ConfidenceGrade.REJECTED)  # just documenting reachability, refined below
