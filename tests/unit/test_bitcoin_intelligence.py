"""
Unit tests for engines/bitcoin_intelligence.py (Module 7).

Run with:
    pytest tests/unit/test_bitcoin_intelligence.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from config.schema import PlatformConfig
from core.models import BtcStatisticsSnapshot, SignalDirection
from engines.bitcoin_intelligence import BitcoinIntelligenceEngine, BitcoinTrend
from infrastructure.binance.models import Candle, FundingRate, OpenInterest
from infrastructure.database.connection import Database
from infrastructure.database.repositories.market_repository import BtcStatisticsRepository
from infrastructure.database.schema import run_migrations
from infrastructure.macro.models import DominanceSnapshot, DxySnapshot
from system.exceptions import DataValidationError


def _candle(now: datetime, index: int, close: float, high: float, low: float) -> Candle:
    return Candle(
        open_time=now + timedelta(hours=index), open=close, high=high, low=low, close=close,
        volume=100.0, close_time=now + timedelta(hours=index, minutes=59),
        quote_volume=100.0 * close, num_trades=10, taker_buy_base_volume=50.0,
        taker_buy_quote_volume=50.0 * close,
    )


def _trending_candles(count: int, start: float, step: float, spread: float = 1.0) -> list[Candle]:
    """A perfectly monotonic (up for step>0, down for step<0) candle series -- high ADX, clear EMA ordering."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        level = start + step * i
        high = level + spread
        low = level
        close = level + spread / 2
        candles.append(_candle(now, i, close=close, high=high, low=low))
    return candles


def _choppy_candles(count: int, base: float = 100.0, amplitude: float = 1.0) -> list[Candle]:
    """Alternating up/down candles -- no sustained direction, low ADX, EMA fast ~= EMA slow."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        level = base + (amplitude if i % 2 == 0 else -amplitude)
        candles.append(_candle(now, i, close=level, high=level + 0.5, low=level - 0.5))
    return candles


class _StubBitcoinClient:
    def __init__(
        self,
        candles: list[Candle],
        funding: Optional[FundingRate] = None,
        open_interest: Optional[OpenInterest] = None,
    ) -> None:
        self._candles = candles
        self._funding = funding
        self._open_interest = open_interest

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        return self._candles

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        return self._funding

    async def get_open_interest(self, symbol: str) -> Optional[OpenInterest]:
        return self._open_interest


class _StubMacroClient:
    def __init__(self, dominance: Optional[DominanceSnapshot] = None, dxy: Optional[DxySnapshot] = None) -> None:
        self._dominance = dominance
        self._dxy = dxy

    async def get_dominance(self) -> Optional[DominanceSnapshot]:
        return self._dominance

    async def get_dxy_snapshot(self) -> Optional[DxySnapshot]:
        return self._dxy


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()


@pytest.fixture
def repository(tmp_path) -> BtcStatisticsRepository:
    database = Database(db_path=str(tmp_path / "btc_test.db"), config=PlatformConfig())
    run_migrations(database)
    return BtcStatisticsRepository(database=database)


# ─────────────────────────────────────────────────────────────────────────
# analyze() -- persistence, validation, classification
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_raises_on_insufficient_history(config, repository):
    client = _StubBitcoinClient(_trending_candles(10, start=100.0, step=1.0))
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    with pytest.raises(DataValidationError):
        await engine.analyze()


@pytest.mark.asyncio
async def test_analyze_persists_and_returns_a_snapshot(config, repository):
    funding = FundingRate(
        symbol="BTCUSDT", mark_price=50000.0, funding_rate=0.0001,
        next_funding_time=datetime.now(timezone.utc),
    )
    open_interest = OpenInterest(symbol="BTCUSDT", open_interest=1000.0, timestamp=datetime.now(timezone.utc))
    client = _StubBitcoinClient(
        _trending_candles(60, start=40000.0, step=100.0), funding=funding, open_interest=open_interest
    )
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    assert isinstance(snapshot, BtcStatisticsSnapshot)
    assert snapshot.id is not None  # persisted -- repository.create() populated it
    assert snapshot.funding_rate == pytest.approx(0.0001)
    assert snapshot.open_interest_usdt == pytest.approx(1000.0 * snapshot.price)
    assert repository.get_latest() is not None


@pytest.mark.asyncio
async def test_analyze_classifies_sustained_uptrend_as_strong_bullish(config, repository):
    client = _StubBitcoinClient(_trending_candles(60, start=40000.0, step=100.0))
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    assert snapshot.trend == BitcoinTrend.STRONG_BULLISH.value
    assert snapshot.health_score > 50.0


@pytest.mark.asyncio
async def test_analyze_classifies_sustained_downtrend_as_strong_bearish(config, repository):
    client = _StubBitcoinClient(_trending_candles(60, start=40000.0, step=-100.0))
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    assert snapshot.trend == BitcoinTrend.STRONG_BEARISH.value


@pytest.mark.asyncio
async def test_analyze_classifies_choppy_data_as_not_strong(config, repository):
    # A symmetric zigzag has no sustained direction (low ADX), so it must
    # never be classified STRONG_* -- but whether the very last tick nudges
    # the reading to NEUTRAL, weak BULLISH, or weak BEARISH is essentially a
    # coin flip of where the series happens to end, so that exact label
    # isn't asserted.
    client = _StubBitcoinClient(_choppy_candles(60, base=40000.0, amplitude=50.0))
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    assert snapshot.trend in {BitcoinTrend.NEUTRAL.value, BitcoinTrend.BULLISH.value, BitcoinTrend.BEARISH.value}
    assert snapshot.health_score < 60.0  # low trend-strength component pulls health down


@pytest.mark.asyncio
async def test_analyze_missing_funding_and_open_interest_are_stored_as_none(config, repository):
    client = _StubBitcoinClient(_trending_candles(60, start=100.0, step=1.0), funding=None, open_interest=None)
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    assert snapshot.funding_rate is None
    assert snapshot.open_interest_usdt is None


# ─────────────────────────────────────────────────────────────────────────
# CONFIRMATION LOGIC  (tested directly against the internal seam -- see
# module docstring rationale: precisely engineering real candle data to
# produce one exact raw-classification flip is far less reliable than
# driving the pure confirmation function with controlled inputs.)
# ─────────────────────────────────────────────────────────────────────────


def test_confirmation_required_holds_previous_confirmed_trend_on_single_flip(config, repository):
    repository.create(
        BtcStatisticsSnapshot(
            snapshot_time=datetime.now(timezone.utc), trend=BitcoinTrend.STRONG_BULLISH.value,
            health_score=90.0, volatility_score=80.0, price=100.0,
        )
    )
    config.bitcoin.confirmation_required = True
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)

    # index -2 ("previous"): price=105 > ema_fast=103 > ema_slow=101, adx=30 -> STRONG_BULLISH.
    # index -1 ("now"): ema_slow is NaN -> classifies NEUTRAL. The two disagree, so the
    # engine must hold the previously CONFIRMED trend (STRONG_BULLISH) rather than flip.
    confirmed = engine._determine_confirmed_trend(
        close_values=[105.0, 110.0],
        ema_fast_series=[103.0, 104.0],
        ema_slow_series=[101.0, float("nan")],
        adx_series=[30.0, 30.0],
        previous_snapshot=repository.get_latest(),
    )

    assert confirmed == BitcoinTrend.STRONG_BULLISH  # held the old confirmed trend, did not flip to NEUTRAL


def test_confirmation_required_accepts_new_trend_once_stable_across_two_reads(config, repository):
    config.bitcoin.confirmation_required = True
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)

    # Both the current and previous reading classify STRONG_BEARISH -> confirmed immediately.
    confirmed = engine._determine_confirmed_trend(
        close_values=[90.0, 85.0],
        ema_fast_series=[95.0, 90.0],
        ema_slow_series=[100.0, 98.0],
        adx_series=[30.0, 30.0],
        previous_snapshot=None,  # agreeing readings short-circuit before this is ever consulted
    )

    assert confirmed == BitcoinTrend.STRONG_BEARISH


def test_confirmation_not_required_flips_immediately(config, repository):
    repository.create(
        BtcStatisticsSnapshot(
            snapshot_time=datetime.now(timezone.utc), trend=BitcoinTrend.STRONG_BULLISH.value,
            health_score=90.0, volatility_score=80.0, price=100.0,
        )
    )
    config.bitcoin.confirmation_required = False
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)

    # Same disagreeing readings as the confirmation-required test above, but with
    # confirmation OFF: raw_trend_previous (STRONG_BULLISH) is never even consulted.
    confirmed = engine._determine_confirmed_trend(
        close_values=[105.0, 110.0],
        ema_fast_series=[103.0, 104.0],
        ema_slow_series=[101.0, float("nan")],
        adx_series=[30.0, 30.0],
        previous_snapshot=repository.get_latest(),
    )

    assert confirmed == BitcoinTrend.NEUTRAL  # accepted the raw reading immediately, no confirmation gate


def test_confirmation_required_with_no_prior_snapshot_accepts_raw_trend(config, repository):
    config.bitcoin.confirmation_required = True
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)

    # Same disagreeing readings (previous=STRONG_BULLISH, now=NEUTRAL), but this time
    # with NO prior snapshot in the repository to fall back on -- the engine has
    # nothing to hold onto, so it must accept the only reading it actually has.
    confirmed = engine._determine_confirmed_trend(
        close_values=[105.0, 110.0],
        ema_fast_series=[103.0, 104.0],
        ema_slow_series=[101.0, float("nan")],
        adx_series=[30.0, 30.0],
        previous_snapshot=None,
    )

    assert confirmed == BitcoinTrend.NEUTRAL  # nothing to fall back to -- accept the only reading available


# ─────────────────────────────────────────────────────────────────────────
# score_for_direction()
# ─────────────────────────────────────────────────────────────────────────


def _snapshot(
    trend: BitcoinTrend,
    health_score: float,
    *,
    dxy_trend: Optional[str] = None,
    btc_dominance_trend: Optional[str] = None,
    usdt_dominance_trend: Optional[str] = None,
) -> BtcStatisticsSnapshot:
    return BtcStatisticsSnapshot(
        snapshot_time=datetime.now(timezone.utc), trend=trend.value,
        health_score=health_score, volatility_score=80.0, price=100.0,
        dxy_trend=dxy_trend, btc_dominance_trend=btc_dominance_trend, usdt_dominance_trend=usdt_dominance_trend,
    )


def test_score_for_direction_favors_long_in_strong_bullish_healthy_market(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(BitcoinTrend.STRONG_BULLISH, health_score=100.0)

    long_score = engine.score_for_direction(SignalDirection.LONG, snapshot)
    short_score = engine.score_for_direction(SignalDirection.SHORT, snapshot)

    assert long_score == pytest.approx(95.0)
    assert short_score == pytest.approx(5.0)
    assert long_score > short_score


def test_score_for_direction_favors_short_in_strong_bearish_healthy_market(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(BitcoinTrend.STRONG_BEARISH, health_score=100.0)

    assert engine.score_for_direction(SignalDirection.SHORT, snapshot) == pytest.approx(95.0)
    assert engine.score_for_direction(SignalDirection.LONG, snapshot) == pytest.approx(5.0)


def test_score_for_direction_collapses_toward_neutral_as_health_drops(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(BitcoinTrend.STRONG_BULLISH, health_score=0.0)

    assert engine.score_for_direction(SignalDirection.LONG, snapshot) == pytest.approx(50.0)
    assert engine.score_for_direction(SignalDirection.SHORT, snapshot) == pytest.approx(50.0)


def test_score_for_direction_returns_neutral_when_no_snapshot_exists(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    assert engine.score_for_direction(SignalDirection.LONG) == pytest.approx(50.0)


def test_get_current_snapshot_reads_from_repository_without_api_call(config, repository):
    repository.create(_snapshot(BitcoinTrend.BULLISH, health_score=70.0))
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)

    snapshot = engine.get_current_snapshot()

    assert snapshot is not None
    assert snapshot.trend == BitcoinTrend.BULLISH.value


# ─────────────────────────────────────────────────────────────────────────
# BTC/USDT DOMINANCE + DXY  (ported from sinyal_kanali_2's MacroClient at
# the platform owner's explicit request)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "previous_pct,current_pct,dead_zone,expected",
    [
        (50.0, 50.5, 0.10, "Rising"),  # +0.5pp, past the 0.10 dead zone
        (50.0, 49.5, 0.10, "Falling"),  # -0.5pp
        (50.0, 50.05, 0.10, "Flat"),  # +0.05pp, clearly inside the dead zone
        (50.0, 50.20, 0.10, "Rising"),  # +0.20pp, clearly past the dead zone
        (None, 50.0, 0.10, "Unknown"),  # no previous scan (e.g. first run ever)
        (50.0, None, 0.10, "Unknown"),  # this scan's fetch failed
    ],
)
def test_dominance_trend_dead_zone_classification(previous_pct, current_pct, dead_zone, expected):
    assert BitcoinIntelligenceEngine._dominance_trend(previous_pct, current_pct, dead_zone) == expected


@pytest.mark.parametrize(
    "price,sma20,sma50,expected",
    [
        (105.0, 103.0, 101.0, "Bullish"),  # price > sma20 > sma50, stacked in order
        (95.0, 97.0, 99.0, "Bearish"),  # price < sma20 < sma50
        (100.0, 103.0, 101.0, "Mixed"),  # price below sma20 but sma20 > sma50 -- tangled, not stacked
    ],
)
def test_classify_dxy_sma_relationship(price, sma20, sma50, expected):
    assert BitcoinIntelligenceEngine._classify_dxy(price, sma20, sma50) == expected


def test_dxy_symmetric_nudge_rewards_and_penalizes(config, repository):
    """DXY is the one symmetric leg -- a conflicting reading actively subtracts, not just withholds."""
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    aligned = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, dxy_trend="Bearish")  # dollar weakening supports LONG
    conflicting = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, dxy_trend="Bullish")

    assert engine.score_for_direction(SignalDirection.LONG, aligned, symbol="ETHUSDT") == pytest.approx(56.0)  # 50 + dxy_weight(6)
    assert engine.score_for_direction(SignalDirection.LONG, conflicting, symbol="ETHUSDT") == pytest.approx(44.0)  # 50 - 6
    # SHORT is the mirror image of LONG for DXY.
    assert engine.score_for_direction(SignalDirection.SHORT, conflicting, symbol="ETHUSDT") == pytest.approx(56.0)


def test_btc_dominance_nudge_is_asymmetric_reward_only(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    rotation_into_alts = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, btc_dominance_trend="Falling")
    rotation_into_btc = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, btc_dominance_trend="Rising")

    # Falling BTC dominance (capital rotating to alts) rewards an alt LONG:
    assert engine.score_for_direction(SignalDirection.LONG, rotation_into_alts, symbol="ETHUSDT") == pytest.approx(55.0)  # 50 + 5
    # But Rising BTC dominance does NOT penalize the same LONG -- asymmetric, reward-only:
    assert engine.score_for_direction(SignalDirection.LONG, rotation_into_btc, symbol="ETHUSDT") == pytest.approx(50.0)
    # Rising BTC dominance rewards an alt SHORT instead:
    assert engine.score_for_direction(SignalDirection.SHORT, rotation_into_btc, symbol="ETHUSDT") == pytest.approx(55.0)


def test_btc_dominance_nudge_excluded_for_btcusdt_itself(config, repository):
    """Dominance reads rotation BETWEEN btc and alts -- meaningless for judging BTC's own candidate signal."""
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, btc_dominance_trend="Falling")  # would reward an alt LONG

    assert engine.score_for_direction(SignalDirection.LONG, snapshot, symbol="BTCUSDT") == pytest.approx(50.0)
    assert engine.score_for_direction(SignalDirection.LONG, snapshot, symbol="ETHUSDT") == pytest.approx(55.0)  # same snapshot, different (non-BTC) symbol


def test_usdt_dominance_nudge_is_asymmetric_reward_only_and_applies_to_btc_too(config, repository):
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    risk_on = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, usdt_dominance_trend="Falling")  # money leaving stables

    # Unlike BTC dominance, USDT dominance is never excluded -- risk-on/risk-off applies to BTC too.
    assert engine.score_for_direction(SignalDirection.LONG, risk_on, symbol="BTCUSDT") == pytest.approx(55.0)  # 50 + 5
    assert engine.score_for_direction(SignalDirection.LONG, risk_on, symbol="ETHUSDT") == pytest.approx(55.0)


def test_macro_nudges_stack(config, repository):
    """All three legs can fire on the same candidate at once, e.g. a genuine broad risk-on setup."""
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(
        BitcoinTrend.NEUTRAL, health_score=100.0,
        dxy_trend="Bearish", btc_dominance_trend="Falling", usdt_dominance_trend="Falling",
    )

    assert engine.score_for_direction(SignalDirection.LONG, snapshot, symbol="ETHUSDT") == pytest.approx(66.0)  # 50 + 6 + 5 + 5


def test_unknown_macro_trend_contributes_nothing(config, repository):
    """Missing/never-fetched macro data (the default -- None on all three fields) must not move the score at all."""
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient([]), config=config, repository=repository)
    snapshot = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0)  # dxy/dominance trends default to None ("Unknown")

    assert engine.score_for_direction(SignalDirection.LONG, snapshot, symbol="ETHUSDT") == pytest.approx(50.0)


def test_macro_fields_round_trip_through_the_repository(repository):
    """Persistence check: the 5 new columns (migration 4) must survive a real create()/get_latest() cycle."""
    saved = repository.create(_snapshot(
        BitcoinTrend.BULLISH, health_score=70.0,
        dxy_trend="Bearish", btc_dominance_trend="Falling", usdt_dominance_trend="Rising",
    ))
    assert saved.btc_dominance_pct is None and saved.usdt_dominance_pct is None  # _snapshot() helper doesn't set these -- fine, just confirming the round trip below isn't accidentally passing on stale in-memory values

    reloaded = repository.get_latest()

    assert reloaded is not None
    assert reloaded.dxy_trend == "Bearish"
    assert reloaded.btc_dominance_trend == "Falling"
    assert reloaded.usdt_dominance_trend == "Rising"


@pytest.mark.asyncio
async def test_analyze_fetches_and_classifies_macro_data_when_a_macro_client_is_provided(config, repository):
    candles = _trending_candles(60, start=100.0, step=1.0)
    client = _StubBitcoinClient(candles)
    macro_client = _StubMacroClient(
        dominance=DominanceSnapshot(btc_pct=51.0, usdt_pct=5.0, fetched_at=datetime.now(timezone.utc)),
        dxy=DxySnapshot(price=105.0, sma20=103.0, sma50=101.0, fetched_at=datetime.now(timezone.utc)),
    )
    engine = BitcoinIntelligenceEngine(client, config=config, repository=repository, macro_client=macro_client)

    snapshot = await engine.analyze()

    assert snapshot.btc_dominance_pct == 51.0
    assert snapshot.usdt_dominance_pct == 5.0
    assert snapshot.dxy_trend == "Bullish"  # 105 > 103 > 101
    assert snapshot.btc_dominance_trend == "Unknown"  # first-ever run: no previous snapshot to compare against
    assert snapshot.usdt_dominance_trend == "Unknown"


@pytest.mark.asyncio
async def test_analyze_skips_macro_fetch_gracefully_when_no_macro_client_is_configured(config, repository):
    """Backward-compat / not-yet-wired-in-production path: analyze() must not error without a macro_client."""
    candles = _trending_candles(60, start=100.0, step=1.0)
    engine = BitcoinIntelligenceEngine(_StubBitcoinClient(candles), config=config, repository=repository)  # no macro_client

    snapshot = await engine.analyze()

    assert snapshot.btc_dominance_pct is None
    assert snapshot.dxy_trend == "Unknown"


# ── classify_status() + confirmed_macro_reasons()  (Telegram checklist / "BTC Status" line, Module 11) ──


@pytest.mark.parametrize("health_score,threshold,expected", [(75.0, 60.0, "Healthy"), (60.0, 60.0, "Healthy"), (45.0, 60.0, "Unhealthy")])
def test_classify_status(health_score, threshold, expected):
    assert BitcoinIntelligenceEngine.classify_status(health_score, threshold) == expected


def test_confirmed_macro_reasons_lists_only_what_actually_aligned():
    snapshot = _snapshot(
        BitcoinTrend.BULLISH, health_score=100.0,  # BULLISH aligns with a LONG candidate
        dxy_trend="Bullish",  # does NOT align with LONG (Bullish DXY supports SHORT)
        btc_dominance_trend="Falling",  # aligns with LONG
        usdt_dominance_trend="Rising",  # does NOT align with LONG
    )

    reasons = BitcoinIntelligenceEngine.confirmed_macro_reasons(SignalDirection.LONG, snapshot, symbol="ETHUSDT")

    assert reasons == ["BTC Trend Confirmed", "BTC Dominance Confirmed"]


def test_confirmed_macro_reasons_excludes_btc_dominance_for_btcusdt_itself():
    snapshot = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0, btc_dominance_trend="Falling")  # would confirm for an alt

    reasons = BitcoinIntelligenceEngine.confirmed_macro_reasons(SignalDirection.LONG, snapshot, symbol="BTCUSDT")

    assert "BTC Dominance Confirmed" not in reasons


def test_confirmed_macro_reasons_empty_when_nothing_aligned_or_unknown():
    snapshot = _snapshot(BitcoinTrend.NEUTRAL, health_score=100.0)  # NEUTRAL trend, all macro fields default to None/"Unknown"

    assert BitcoinIntelligenceEngine.confirmed_macro_reasons(SignalDirection.LONG, snapshot, symbol="ETHUSDT") == []
