"""
engines/coin_trust.py

Coin Trust Engine (SRS Part 7 COIN TRUST SYSTEM): blends a symbol's
underlying profile quality (liquidity, volatility, trend reliability,
spread quality, historical stability -- from `CoinProfileRepository`)
with its actual live track record (win rate and win/loss streaks -- from
`CoinStatisticsRepository`) into one 0-100 trust score, and persists it
back onto the symbol's `CoinProfile` row.

Sample-size gating (SRS Part 19 `CoinTrustConfig.min_trades_for_trust_score`):
a coin with few closed trades has an unreliable win-rate reading, so the
track-record component's influence is scaled by `data_confidence =
min(1, total_signals / min_trades_for_trust_score)` -- a brand new coin
(0 signals) gets exactly `new_coin_default_trust_score` regardless of
what its (essentially random, tiny-sample) win rate happens to read,
and only converges to the fully-computed blend as real history accumulates.

Weighting note: `CoinTrustConfig` defines the sample-size gate and the
bonus/penalty CAPS (`max_trust_bonus`, `max_trust_penalty`,
`new_coin_default_trust_score`) but not a profile-vs-track-record split,
so `_PROFILE_WEIGHT`/`_TRACK_RECORD_WEIGHT` are documented local
constants (same pattern as `engines.bitcoin_intelligence`'s ADX
threshold and `engines.market_health`'s heuristic scales).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import CoinClassification, CoinProfile, CoinStatistics
from infrastructure.database.repositories.coin_repository import CoinProfileRepository, CoinStatisticsRepository

_CLASSIFICATION_BANDS: tuple[tuple[float, CoinClassification], ...] = (
    (85.0, CoinClassification.ULTRA_HIGH_QUALITY),
    (70.0, CoinClassification.HIGH_QUALITY),
    (50.0, CoinClassification.MEDIUM_QUALITY),
    (30.0, CoinClassification.HIGH_RISK),
)


@dataclass(frozen=True)
class TrustComponentScores:
    """Every intermediate value that blends into the final trust score, kept for transparency/logging."""

    liquidity: float
    volatility_reliability: float  # 100 - CoinProfile.volatility_score (lower volatility -> more reliable)
    trend_reliability: float
    spread_quality: float
    historical_stability: float
    profile_component: float  # mean of the five scores above
    win_rate: float
    streak_bonus: float
    streak_penalty: float
    track_record_component: float  # win_rate + streak_bonus - streak_penalty, clamped to [0, 100]
    data_confidence: float  # 0-1: how much weight total_signals earns the track record component


@dataclass(frozen=True)
class CoinTrustAssessment:
    """Everything `CoinTrustEngine.analyze()` produces for one symbol."""

    symbol: str
    trust_score: float
    classification: CoinClassification
    components: TrustComponentScores
    total_signals: int
    explanation: str


class CoinTrustEngine:
    """Computes and persists a 0-100 trust score per symbol from profile quality + live track record."""

    _PROFILE_WEIGHT = 0.5
    _TRACK_RECORD_WEIGHT = 0.5
    _BONUS_PER_WINNING_STREAK_UNIT = 1.0
    _PENALTY_PER_LOSING_STREAK_UNIT = 1.0
    # Below this liquidity_score, a symbol is classified LOW_LIQUIDITY
    # regardless of its trust score -- illiquid markets are unsafe to
    # trade even if their (thin, easily-manipulated) win rate looks good.
    _LOW_LIQUIDITY_THRESHOLD = 20.0

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        profile_repository: Optional[CoinProfileRepository] = None,
        statistics_repository: Optional[CoinStatisticsRepository] = None,
    ) -> None:
        self._config = config or get_config()
        self._profile_repository = profile_repository or CoinProfileRepository()
        self._statistics_repository = statistics_repository or CoinStatisticsRepository()

    def analyze(self, symbol: str) -> CoinTrustAssessment:
        """
        Compute `symbol`'s trust assessment and persist the resulting
        `trust_score`/`classification` onto its `CoinProfile` row (if one
        exists -- a symbol with no profile yet simply isn't persisted to,
        since there is nothing to update).
        """
        cfg = self._config.coin_trust
        profile = self._profile_repository.get(symbol)
        statistics = self._statistics_repository.get(symbol)

        components = self._compute_components(profile, statistics, cfg)
        trust_score = self._blend_final_score(components, cfg)
        classification = self._classify(trust_score, components, statistics)

        if profile is not None:
            updated_profile = replace(
                profile,
                coin_trust_score=round(trust_score, 2),
                classification=classification,
                updated_at=datetime.now(timezone.utc),
            )
            self._profile_repository.upsert(updated_profile)

        return CoinTrustAssessment(
            symbol=symbol,
            trust_score=round(trust_score, 2),
            classification=classification,
            components=components,
            total_signals=statistics.total_signals if statistics is not None else 0,
            explanation=self._build_explanation(components, classification),
        )

    def get_current_trust_score(self, symbol: str) -> Optional[float]:
        """Read the most recently persisted trust score without recomputing anything."""
        profile = self._profile_repository.get(symbol)
        return profile.coin_trust_score if profile is not None else None

    def _compute_components(
        self,
        profile: Optional[CoinProfile],
        statistics: Optional[CoinStatistics],
        cfg,
    ) -> TrustComponentScores:
        default = cfg.new_coin_default_trust_score

        if profile is not None:
            liquidity = profile.liquidity_score
            volatility_reliability = 100.0 - profile.volatility_score
            trend_reliability = profile.trend_reliability_score
            spread_quality = profile.spread_quality_score
            historical_stability = profile.historical_stability_score
        else:
            liquidity = volatility_reliability = trend_reliability = spread_quality = historical_stability = default

        profile_component = (
            liquidity + volatility_reliability + trend_reliability + spread_quality + historical_stability
        ) / 5.0

        total_signals = statistics.total_signals if statistics is not None else 0
        win_rate = statistics.win_rate_percent if statistics is not None else default
        current_streak = statistics.current_streak if statistics is not None else 0

        streak_bonus = (
            min(cfg.max_trust_bonus, current_streak * self._BONUS_PER_WINNING_STREAK_UNIT)
            if current_streak > 0
            else 0.0
        )
        streak_penalty = (
            min(cfg.max_trust_penalty, abs(current_streak) * self._PENALTY_PER_LOSING_STREAK_UNIT)
            if current_streak < 0
            else 0.0
        )
        track_record_component = max(0.0, min(100.0, win_rate + streak_bonus - streak_penalty))

        data_confidence = (
            min(1.0, total_signals / cfg.min_trades_for_trust_score) if cfg.min_trades_for_trust_score > 0 else 1.0
        )

        return TrustComponentScores(
            liquidity=liquidity,
            volatility_reliability=volatility_reliability,
            trend_reliability=trend_reliability,
            spread_quality=spread_quality,
            historical_stability=historical_stability,
            profile_component=profile_component,
            win_rate=win_rate,
            streak_bonus=streak_bonus,
            streak_penalty=streak_penalty,
            track_record_component=track_record_component,
            data_confidence=data_confidence,
        )

    def _blend_final_score(self, components: TrustComponentScores, cfg) -> float:
        raw_score = (
            components.profile_component * self._PROFILE_WEIGHT
            + components.track_record_component * self._TRACK_RECORD_WEIGHT
        )
        default = cfg.new_coin_default_trust_score
        # Converges from the neutral default (no data) toward the fully computed
        # blend as data_confidence -> 1 (enough closed trades to trust the read).
        return default + (raw_score - default) * components.data_confidence

    def _classify(
        self,
        trust_score: float,
        components: TrustComponentScores,
        statistics: Optional[CoinStatistics],
    ) -> CoinClassification:
        if statistics is None or statistics.total_signals == 0:
            return CoinClassification.NEW_LISTING
        if components.liquidity < self._LOW_LIQUIDITY_THRESHOLD:
            return CoinClassification.LOW_LIQUIDITY
        for threshold, classification in _CLASSIFICATION_BANDS:
            if trust_score >= threshold:
                return classification
        return CoinClassification.SPECULATIVE

    @staticmethod
    def _build_explanation(components: TrustComponentScores, classification: CoinClassification) -> str:
        return (
            f"profile={components.profile_component:.1f} "
            f"(liquidity={components.liquidity:.1f}, volatility_reliability={components.volatility_reliability:.1f}, "
            f"trend_reliability={components.trend_reliability:.1f}, spread_quality={components.spread_quality:.1f}, "
            f"historical_stability={components.historical_stability:.1f}); "
            f"track_record={components.track_record_component:.1f} "
            f"(win_rate={components.win_rate:.1f}, bonus={components.streak_bonus:.1f}, "
            f"penalty={components.streak_penalty:.1f}); "
            f"data_confidence={components.data_confidence:.2f} -> classification={classification.value}"
        )
