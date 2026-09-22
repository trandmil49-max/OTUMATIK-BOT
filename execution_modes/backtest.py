"""
execution_modes/backtest.py

Backtest Runner (Module 21) -- PROJECT_STATUS.md: "Paper Trading /
Backtest Runner (reuses Modules 6-14 unchanged against historical/
simulated data)"; SRS Part 20/21: "execution_modes/ <- live / paper /
backtest runners, all reusing the SAME core engines".

SCOPE OF THIS PASS -- read before extending. This module's blocker for
full-pipeline backtesting is DATA AVAILABILITY, not effort or a missing
formula -- see the second bullet below, verified directly against the
three engines' source in this pass, not assumed.

    IN SCOPE, fully implemented and tested:
        * `BacktestRunner.replay_signal()` -- given an existing,
          persisted Signal and historical candles for its symbol,
          replays them chronologically through the UNCHANGED
          `PositionMonitorEngine.run_tick()` -- the exact same trade-
          lifecycle logic (`_hit_level`, TP1/SL
          transitions, P&L calculation) that live trading uses. Nothing
          about how a level-hit is decided is reimplemented or
          reinvented here.
        * `BacktestRunner.fetch_historical_candles()` -- pages through
          `BinanceFuturesClient.get_klines()`'s now-extended
          `start_time`/`end_time` parameters (see that method's
          docstring) to assemble an arbitrary historical range beyond
          Binance's per-call limit.
        * `get_klines(start_time=, end_time=)` on the Binance client
          itself -- a backward-compatible addition (omit both, behavior
          is byte-for-byte what it was before this module existed).

    NOT IN SCOPE -- genuinely blocked by data availability, not deferred
    by choice, not a Module 22 gap:
        * Full signal-GENERATION backtesting -- replaying
          `ScannerOrchestrator`'s Fast Filter / Bitcoin Intelligence /
          Market Health stages against historical data to see what
          signals WOULD have fired in the past. Checked directly against
          those three engines' source: they call
          `get_ticker_24hr()`, `get_book_ticker()`, `get_funding_rate()`,
          `get_open_interest()`. `get_book_ticker()` (live order-book
          best bid/ask) has NO historical equivalent anywhere in
          Binance's REST API -- it is fundamentally a current-moment-only
          concept. Historical 24hr-ticker/funding-rate/open-interest
          reconstruction is *possible* via different endpoints than this
          client currently wraps, but each has its own gaps/granularity
          limits and is a separately-scoped data-engineering problem, not
          something to approximate quietly here.
        * `CoinTrustEngine` and `SignalGenerationEngine` need no client at
          all (verified: both are constructed without one in
          `ScannerOrchestrator.__init__`) -- they are NOT part of this
          blocker, and are already safely reusable for backtesting the
          moment the above is solved.
        * What `RunMode.PAPER` does differently from `RunMode.LIVE`. The
          platform is signal-only (no order execution) at every mode
          already; deciding any PAPER-specific behavior (e.g. a label on
          Telegram notifications) is a Module 22 (main.py / composition
          root) wiring decision, not fabricated here.

`HistoricalMarketDataSource` below is the extension point PROJECT_STATUS.md's
hexagonal-boundary rule anticipates: once historical ticker/funding/open-
interest sourcing is solved, a concrete implementation can be handed to
`ScannerOrchestrator(client=...)` completely unchanged -- no engine code
needs to be touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

from core.models import Signal, Trade, TradeStatus
from engines.position_monitor import PositionMonitorEngine
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.binance.models import Candle
from infrastructure.database.repositories.coin_repository import CoinStatisticsRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.logging_setup import get_logger

_logger = get_logger("trading")

_MAX_CANDLES_PER_REQUEST = 1500  # Binance's own hard cap for /fapi/v1/klines


class HistoricalMarketDataSource(Protocol):
    """
    Extension point, not an implementation -- see module docstring.
    Names exactly the client methods `FastFilterEngine` /
    `BitcoinIntelligenceEngine` / `MarketHealthEngine` call, verified
    directly against their source in this pass.
    """

    async def get_ticker_24hr(self, symbol: Optional[str] = None): ...
    async def get_book_ticker(self, symbol: str): ...
    async def get_funding_rate(self, symbol: str): ...
    async def get_open_interest(self, symbol: str): ...


@dataclass(frozen=True)
class BacktestResult:
    signal: Signal
    trade: Optional[Trade]
    candles_processed: int


class BacktestRunner:
    """Replays one already-persisted Signal against historical candles using the unchanged PositionMonitorEngine."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        position_monitor: Optional[PositionMonitorEngine] = None,
        signal_repository: Optional[SignalRepository] = None,
        trade_repository: Optional[TradeRepository] = None,
        coin_statistics_repository: Optional[CoinStatisticsRepository] = None,
    ) -> None:
        self._client = client
        self._trade_repository = trade_repository or TradeRepository()
        # Passed through explicitly (not left to PositionMonitorEngine's own
        # zero-arg fallback) so that, when a caller supplies its own
        # trade_repository/signal_repository pointed at a specific database
        # but relies on this constructor's PositionMonitorEngine default,
        # trade-closing logic's coin_statistics updates land in that SAME
        # database rather than silently falling back to the process-wide
        # default one. Caught by test_backtest.py during Module 21's own
        # development: the fallback originally pointed at data/platform.db
        # regardless of what database the rest of the call used.
        self._position_monitor = position_monitor or PositionMonitorEngine(
            signal_repository=signal_repository,
            trade_repository=self._trade_repository,
            coin_statistics_repository=coin_statistics_repository,
        )

    async def fetch_historical_candles(
        self, symbol: str, interval: str, start_time: datetime, end_time: datetime,
    ) -> list[Candle]:
        """Pages through Binance's per-call limit to return every candle in [start_time, end_time), chronological, no gaps or boundary duplicates."""
        all_candles: list[Candle] = []
        cursor = start_time
        while cursor < end_time:
            page = await self._client.get_klines(
                symbol, interval, limit=_MAX_CANDLES_PER_REQUEST, start_time=cursor, end_time=end_time,
            )
            if not page:
                break
            all_candles.extend(page)
            next_cursor = page[-1].open_time
            if next_cursor <= cursor:
                break  # defensive: never loop forever if a page fails to advance
            cursor = next_cursor

        seen_open_times: set[datetime] = set()
        deduplicated: list[Candle] = []
        for candle in all_candles:
            if candle.open_time in seen_open_times:
                continue
            seen_open_times.add(candle.open_time)
            deduplicated.append(candle)
        return deduplicated

    def replay_signal(self, signal: Signal, candles: list[Candle]) -> BacktestResult:
        """
        Feeds each candle's CLOSE price, in chronological order, into
        `PositionMonitorEngine.run_tick()` for `signal.symbol` until the
        resulting trade closes or candles run out.

        Simplification, stated plainly rather than silently assumed:
        uses each candle's close only -- it will not detect an
        intra-candle wick that touches a level without the candle
        closing beyond it. Choosing a specific high/low check ordering
        within a candle is itself a modeling assumption with real
        consequences for the result, and no convention for it is
        documented anywhere this module can see -- rather than invent
        one, this stays at the simple, transparent level. Finer intra-
        candle resolution is a natural, separately-scoped enhancement
        once a specific convention is decided.

        Requires `signal` to already be persisted with `TradeStatus.WAITING`
        (e.g. the object returned by `SignalRepository.create()`) --
        `PositionMonitorEngine.run_tick()` reads WAITING signals from the
        repository, not from an object passed directly to it.
        """
        if not candles:
            raise ValueError("replay_signal() requires at least one candle")
        if signal.status != TradeStatus.WAITING:
            raise ValueError(
                f"replay_signal() requires a WAITING (not yet activated) signal; got status={signal.status!r}"
            )

        sorted_candles = sorted(candles, key=lambda c: c.open_time)
        trade_id: Optional[int] = None

        for index, candle in enumerate(sorted_candles, start=1):
            tick = self._position_monitor.run_tick({signal.symbol: candle.close})

            if trade_id is None and tick.activated_trade_ids:
                # Match by signal_id explicitly rather than assuming index 0 --
                # activated_trade_ids reflects every trade activated this tick,
                # which could include an unrelated WAITING signal for the same
                # symbol if one happens to exist in the database.
                for candidate_id in tick.activated_trade_ids:
                    candidate = self._trade_repository.get_by_id(candidate_id)
                    if candidate is not None and candidate.signal_id == signal.id:
                        trade_id = candidate_id
                        break

            if trade_id is not None and (
                trade_id in tick.tp1_hit_trade_ids
                or trade_id in tick.stop_loss_trade_ids
                or trade_id in tick.trailing_stop_exit_trade_ids
            ):
                trade = self._trade_repository.get_by_id(trade_id)
                _logger.info(
                    "Backtest: signal %d closed as %s after %d/%d candle(s)",
                    signal.id, trade.status.value if trade else "?", index, len(sorted_candles),
                )
                return BacktestResult(signal=signal, trade=trade, candles_processed=index)

        trade = self._trade_repository.get_by_id(trade_id) if trade_id is not None else None
        _logger.info(
            "Backtest: signal %d did not close within %d candle(s) (still %s)",
            signal.id, len(sorted_candles), trade.status.value if trade else "WAITING (never activated)",
        )
        return BacktestResult(signal=signal, trade=trade, candles_processed=len(sorted_candles))
