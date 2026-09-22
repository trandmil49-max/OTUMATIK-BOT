"""
engines/smart_money.py

Smart Money Engine (Module 23 -- added at the platform owner's explicit
request, after PROJECT_STATUS.md's prior audit had already reviewed and
deliberately deferred exactly this input: "long/short account ratio ...
available as a genuine future enhancement if wanted, with its own
SRS-style spec for how it should weigh into scoring." This module is
that spec.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT:
Binance's consumer-facing "Smart Money" page (binance.com/en/smart-money)
shows a per-trader-nickname feed of individual positions and a
leaderboard of top traders/whales. That specific view has no public,
documented API -- Binance has never exposed leaderboard data through the
standard Futures API, and the only ways to get it are scraping the
rendered page or an unofficial/paid third-party service, neither of
which is something a bot meant to run unattended on Railway for months
can depend on (the page's markup can change at any time, scraping risks
the account/IP, a paid API adds an external dependency this platform
would otherwise have zero of).

What IS official, public, free, and has been stable since 2020 is the
SAME underlying signal in aggregate form: `GET
/futures/data/topLongShortAccountRatio` and `.../topLongShortPositionRatio`
-- among the top 20% of accounts by margin balance, are more of them
long or short (by account count, and separately by position size). No
nicknames, but a trading bot has no use for a nickname; it has use for
"is the smart money net long or net short, and by how much" -- which is
exactly what these two ratios are. This engine is built on those, via
`infrastructure.binance.client.BinanceFuturesClient.get_top_trader_ratio()`.

DESIGN: two ratios, not one blended number. `account_ratio` (by count)
and `position_ratio` (by notional size) usually move together but can
diverge -- many small accounts leaning one way while the few largest
positions lean the other is itself informative. How much a strategy
profile weighs one versus the other is a judgment call, not a fact this
engine invents, so both are exposed via `SmartMoneyConfig
.account_ratio_weight`/`position_ratio_weight` (both default to 0.5).

SCORING: `score_for_direction()` does two separate things with each
ratio, not one formula wearing two hats:
    1. Alignment: does this ratio support the candidate direction, and
       by how much? Read on the natural log of the ratio (symmetric
       around 0 at ratio=1.0, i.e. perfectly balanced) so a ratio of 2.0
       (2x long) and 0.5 (2x short) are equal-and-opposite rather than
       the raw ratio's misleading 2.0-vs-0.5 asymmetry, then mapped
       through a saturating curve to 0-100 (50 = neutral/no lean).
    2. Crowding risk: REGARDLESS of direction, an extreme reading on
       either side is exactly the kind of imbalance that precedes a
       squeeze/liquidation cascade against the crowded side (a lesson
       taken directly from `sinyal_kanali_2`'s own contrarian
       long/short-ratio handling, generalized here into one documented,
       testable curve instead of hand-picked asymmetric thresholds).
       This is a separate penalty subtracted after alignment, capped so
       it can never by itself flip a well-aligned score negative.

Gradual suppression, not a hard veto: matches
`engines.bitcoin_intelligence`'s explicitly stated philosophy ("if
Bitcoin is unhealthy, reduce or suppress altcoin signals... gradual, not
a hard veto"). A candidate with smart money strongly against it scores
low on THIS category and can still fail `ConfidenceConfig
.minimum_confidence` like any other weak input, surfacing as the generic
`RejectionReason.LOW_CONFIDENCE` -- there is no dedicated smart-money
rejection reason, same reasoning `bitcoin_intelligence` gives for not
having one either.

Missing data degrades to neutral, never to a fabricated lean: if
`get_snapshot()` returns `None` (new symbol, transient API failure),
`score_for_direction()` returns exactly 50.0 -- the same "Unknown -> no
effect" contract `sinyal_kanali_2`'s MacroClient used for its DXY/
dominance reads, so one missing data point costs a candidate the chance
to earn more than a neutral share of this category, without ever
blocking it outright or inventing support that was never observed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import SignalDirection
from infrastructure.binance.models import TopTraderRatio
from system.logging_setup import get_logger

_logger = get_logger("engines")

# Neutral score returned when there is no ratio data to score at all
# (missing snapshot) -- see module docstring's "Missing data" note.
_NEUTRAL_SCORE = 50.0


@dataclass(frozen=True)
class SmartMoneyScore:
    """One direction-aware Smart Money read, with the detail string `ConfidenceEngine` surfaces verbatim."""

    score: float
    detail: str


class SmartMoneyEngine:
    """
    Fetches and scores Binance's official Top Trader Long/Short Ratio
    for one symbol. See module docstring for the full design.
    """

    # Log-ratio value at which the alignment curve reaches its full
    # +/-1.0 saturation (chosen so a 2x lean, ln(2.0)=0.69, already earns
    # most of the available swing; a log-ratio this large or larger earns
    # no MORE alignment credit, since scoring is bounded 0-100, not
    # open-ended).
    _ALIGNMENT_SATURATION_LOG_RATIO = 0.90

    # Crowding penalty starts accruing once |log(ratio)| exceeds this --
    # ln(2.5) is approximately 0.92, i.e. roughly a 2.5x-or-worse lean on
    # either side. Below this, a lean is read as informative alignment
    # only (see above), not yet a crowding warning.
    _CROWDING_LOG_RATIO_THRESHOLD = 0.92
    # Points subtracted per unit of log-ratio beyond the threshold above.
    _CROWDING_PENALTY_SLOPE = 25.0
    # Crowding can never by itself pull a score down by more than this
    # many points -- a dampener on an otherwise well-aligned read, not a
    # veto (see module docstring's "gradual suppression" note).
    _MAX_CROWDING_PENALTY = 20.0

    def __init__(self, client, config: Optional[PlatformConfig] = None) -> None:
        self._client = client
        self._config = config or get_config()

    async def get_snapshot(self, symbol: str) -> Optional[TopTraderRatio]:
        """
        Fetches the current Top Trader Long/Short Ratio (account +
        position) for one symbol. `None` on missing/failed data -- never
        raises for a single symbol, same contract as the other
        per-symbol engines `ScannerOrchestrator` already isolates
        failures around.
        """
        try:
            return await self._client.get_top_trader_ratio(symbol, period=self._config.smart_money.period)
        except Exception:  # noqa: BLE001 - degrade to "no data", same as a None API response
            _logger.warning("Smart Money: get_top_trader_ratio failed for %s, treating as no data", symbol)
            return None

    def score_for_direction(self, direction: SignalDirection, snapshot: Optional[TopTraderRatio]) -> SmartMoneyScore:
        """
        Pure function, no I/O -- see module docstring's scoring design.
        `snapshot=None` returns a flat neutral 50.0, never blocks.
        """
        if snapshot is None:
            return SmartMoneyScore(score=_NEUTRAL_SCORE, detail="no top-trader ratio data available (neutral)")

        cfg = self._config.smart_money
        sign = 1.0 if direction == SignalDirection.LONG else -1.0

        account_log_ratio = math.log(snapshot.account_ratio) if snapshot.account_ratio > 0 else 0.0
        position_log_ratio = math.log(snapshot.position_ratio) if snapshot.position_ratio > 0 else 0.0

        account_alignment = self._alignment_score(account_log_ratio * sign)
        position_alignment = self._alignment_score(position_log_ratio * sign)
        base_score = cfg.account_ratio_weight * account_alignment + cfg.position_ratio_weight * position_alignment

        crowding_extremity = max(abs(account_log_ratio), abs(position_log_ratio))
        penalty = self._crowding_penalty(crowding_extremity)
        final_score = max(0.0, min(100.0, base_score - penalty))

        detail = (
            f"top-trader account ratio={snapshot.account_ratio:.2f}, position ratio={snapshot.position_ratio:.2f}"
            + (f", crowding penalty -{penalty:.1f}" if penalty > 0 else "")
        )
        return SmartMoneyScore(score=final_score, detail=detail)

    @classmethod
    def _alignment_score(cls, signed_log_ratio: float) -> float:
        """Maps a direction-signed log-ratio to 0-100 (50=neutral), saturating at `_ALIGNMENT_SATURATION_LOG_RATIO`."""
        fraction = max(-1.0, min(1.0, signed_log_ratio / cls._ALIGNMENT_SATURATION_LOG_RATIO))
        return 50.0 + fraction * 50.0

    @classmethod
    def _crowding_penalty(cls, log_ratio_extremity: float) -> float:
        """Direction-agnostic squeeze-risk penalty -- see module docstring's 'Crowding risk' note."""
        if log_ratio_extremity <= cls._CROWDING_LOG_RATIO_THRESHOLD:
            return 0.0
        excess = log_ratio_extremity - cls._CROWDING_LOG_RATIO_THRESHOLD
        return min(cls._MAX_CROWDING_PENALTY, excess * cls._CROWDING_PENALTY_SLOPE)
