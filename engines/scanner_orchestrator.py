"""
engines/scanner_orchestrator.py

Scanner Orchestrator (SRS Part 16 SMART SCANNING ENGINE + Part 1 MASTER
PIPELINE): the top-level engine that runs one full scan cycle end to end,
wiring together every engine built in Modules 5-14 without duplicating
any of their logic:

    1. Symbol Discovery   -- sync the exchange symbol universe (Module 5).
    2. Bitcoin Intelligence + Market Health -- one market-wide read per
       cycle (Modules 7-8).
    3. Fast Filter (Stage 1) -- cheap elimination across every eligible
       symbol (Module 5).
    4. Per-surviving-symbol analysis: candles -> ATR -> Market Structure
       (Module 9) -> Coin Trust (Module 11) -> Signal Generation (Module
       13, which itself runs Risk Management [Module 10] + Confidence
       [Module 12] and persists a Signal or a Rejection).
    5. Position Monitor (Module 14) -- advance every WAITING/ACTIVE trade
       using the fresh prices just gathered in step 4.

Component-failure isolation (SRS Part 18 STABILITY ENGINE: one symbol's
failure must never crash the whole scan): every per-symbol step, and the
Bitcoin/Market Health reads, are wrapped so an exception analyzing ONE
thing is logged via `system.error_handler` and skipped, never aborting
the rest of the cycle. Duplicate-signal prevention and portfolio-limit
enforcement are NOT re-implemented here -- they already live inside
`SignalGenerationEngine`/`RiskManagementEngine`; this orchestrator gets
that behavior for free by calling those engines rather than bypassing them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import BtcStatisticsSnapshot, MarketStatisticsSnapshot, Signal, SignalDirection
from engines.bitcoin_intelligence import BitcoinIntelligenceEngine
from engines.coin_trust import CoinTrustEngine
from engines.confidence import MomentumSnapshot
from engines.fast_filter import FastFilterEngine
from engines.indicators import adx as compute_adx
from engines.indicators import atr as compute_atr
from engines.indicators import closes as compute_closes
from engines.indicators import macd as compute_macd
from engines.indicators import recent_price_change_pct
from engines.indicators import rsi as compute_rsi
from engines.indicators import volume_ratio as compute_volume_ratio
from engines.market_health import MarketHealthEngine
from engines.market_structure import MarketStructureEngine, StructureBias
from engines.position_monitor import MonitorTickResult, PositionMonitorEngine
from engines.signal_generation import SignalGenerationEngine
from engines.smart_money import SmartMoneyEngine
from engines.symbol_discovery import SymbolDiscoveryEngine
from infrastructure.database.repositories.coin_repository import CoinRepository
from system.exceptions import DataValidationError
from system.error_handler import handle_error
from system.logging_setup import get_logger

_logger = get_logger("trading")

_T = TypeVar("_T")


@dataclass(frozen=True)
class ScanCycleResult:
    """Everything that happened during one `ScannerOrchestrator.run_scan_cycle()` call."""

    symbols_discovered: int
    symbols_after_fast_filter: int
    signals_generated: tuple[Signal, ...]
    symbols_rejected: tuple[str, ...]
    symbols_failed: tuple[str, ...]
    monitor_result: Optional[MonitorTickResult]
    bitcoin_snapshot: Optional[BtcStatisticsSnapshot]
    market_health_snapshot: Optional[MarketStatisticsSnapshot]


class ScannerOrchestrator:
    """Runs one full scan cycle: discovery -> market context -> fast filter -> per-symbol signal generation -> monitor."""

    _STRUCTURE_TIMEFRAME = "15m"
    _STRUCTURE_CANDLE_LIMIT = 200
    _ATR_PERIOD = 14

    def __init__(
        self,
        client,
        config: Optional[PlatformConfig] = None,
        coin_repository: Optional[CoinRepository] = None,
        symbol_discovery: Optional[SymbolDiscoveryEngine] = None,
        fast_filter: Optional[FastFilterEngine] = None,
        bitcoin_engine: Optional[BitcoinIntelligenceEngine] = None,
        market_health_engine: Optional[MarketHealthEngine] = None,
        coin_trust_engine: Optional[CoinTrustEngine] = None,
        structure_engine: Optional[MarketStructureEngine] = None,
        smart_money_engine: Optional[SmartMoneyEngine] = None,
        signal_generation_engine: Optional[SignalGenerationEngine] = None,
        position_monitor: Optional[PositionMonitorEngine] = None,
    ) -> None:
        self._client = client
        self._config = config or get_config()
        self._coin_repository = coin_repository or CoinRepository()
        self._symbol_discovery = symbol_discovery or SymbolDiscoveryEngine(client, self._coin_repository)
        self._fast_filter = fast_filter or FastFilterEngine(client, config=self._config)
        self._bitcoin_engine = bitcoin_engine or BitcoinIntelligenceEngine(client, config=self._config)
        self._market_health_engine = market_health_engine or MarketHealthEngine(client, config=self._config)
        self._coin_trust_engine = coin_trust_engine or CoinTrustEngine(config=self._config)
        self._structure_engine = structure_engine or MarketStructureEngine()
        self._smart_money_engine = smart_money_engine or SmartMoneyEngine(client, config=self._config)
        self._signal_generation_engine = signal_generation_engine or SignalGenerationEngine(config=self._config)
        self._position_monitor = position_monitor or PositionMonitorEngine(config=self._config)

    async def run_scan_cycle(self) -> ScanCycleResult:
        """Run exactly one full scan cycle. Never raises for a single symbol's failure -- see module docstring."""
        await self._safe_call("symbol discovery", self._symbol_discovery.sync)
        eligible_symbols = self._deduplicate([coin.symbol for coin in self._coin_repository.list_active()])

        bitcoin_snapshot = await self._safe_call("Bitcoin Intelligence", self._bitcoin_engine.analyze)
        market_health_snapshot: Optional[MarketStatisticsSnapshot] = None
        if eligible_symbols:
            market_health_snapshot = await self._safe_call(
                "Market Health", lambda: self._market_health_engine.analyze(eligible_symbols)
            )

        if not eligible_symbols:
            _logger.info("Scan cycle: no eligible symbols -- nothing to filter or analyze this cycle")
            return ScanCycleResult(
                symbols_discovered=0, symbols_after_fast_filter=0, signals_generated=(),
                symbols_rejected=(), symbols_failed=(), monitor_result=None,
                bitcoin_snapshot=bitcoin_snapshot, market_health_snapshot=market_health_snapshot,
            )

        fast_filter_result = await self._safe_call("Stage 1 fast filter", lambda: self._fast_filter.run(eligible_symbols))
        if fast_filter_result is None:
            _logger.warning("Scan cycle: Stage 1 fast filter failed -- treating as zero survivors for this cycle")
            return ScanCycleResult(
                symbols_discovered=len(eligible_symbols), symbols_after_fast_filter=0, signals_generated=(),
                symbols_rejected=(), symbols_failed=(), monitor_result=None,
                bitcoin_snapshot=bitcoin_snapshot, market_health_snapshot=market_health_snapshot,
            )
        symbols_to_analyze = self._apply_max_symbols_cap(fast_filter_result.surviving_symbols)

        signals: list[Signal] = []
        rejected_symbols: list[str] = []
        failed_symbols: list[str] = []
        current_prices: dict[str, float] = {}

        for symbol in symbols_to_analyze:
            try:
                signal = await self._analyze_symbol(symbol, bitcoin_snapshot, market_health_snapshot, current_prices)
                if signal is None:
                    rejected_symbols.append(symbol)
                else:
                    signals.append(signal)
            except Exception as exc:  # noqa: BLE001 - isolate one symbol's failure from the whole cycle
                handle_error(
                    DataValidationError(f"Scan analysis failed for {symbol}", cause=exc, context={"symbol": symbol}),
                    category="trading",
                )
                failed_symbols.append(symbol)

        monitor_result = self._position_monitor.run_tick(current_prices) if current_prices else None

        _logger.info(
            "Scan cycle complete: %d eligible, %d survived Stage 1, %d analyzed, %d signals, %d rejected, %d failed",
            len(eligible_symbols), len(fast_filter_result.surviving_symbols), len(symbols_to_analyze),
            len(signals), len(rejected_symbols), len(failed_symbols),
        )
        return ScanCycleResult(
            symbols_discovered=len(eligible_symbols),
            symbols_after_fast_filter=len(fast_filter_result.surviving_symbols),
            signals_generated=tuple(signals),
            symbols_rejected=tuple(rejected_symbols),
            symbols_failed=tuple(failed_symbols),
            monitor_result=monitor_result,
            bitcoin_snapshot=bitcoin_snapshot,
            market_health_snapshot=market_health_snapshot,
        )

    async def _analyze_symbol(
        self,
        symbol: str,
        bitcoin_snapshot: Optional[BtcStatisticsSnapshot],
        market_health_snapshot: Optional[MarketStatisticsSnapshot],
        current_prices: dict[str, float],
    ) -> Optional[Signal]:
        """One symbol's full Stage 2+ analysis. Returns the persisted Signal, or None if rejected."""
        candles = await self._client.get_klines(symbol, self._STRUCTURE_TIMEFRAME, limit=self._STRUCTURE_CANDLE_LIMIT)
        if len(candles) < self._ATR_PERIOD + 1:
            return None

        current_price = candles[-1].close
        current_prices[symbol] = current_price

        structure = self._structure_engine.analyze(candles)
        if structure.bias == StructureBias.UNCLEAR:
            return None
        direction = SignalDirection.LONG if structure.bias == StructureBias.BULLISH else SignalDirection.SHORT

        atr_value = compute_atr(candles, self._ATR_PERIOD)[-1]
        if math.isnan(atr_value) or atr_value <= 0:
            return None

        momentum = self._build_momentum_snapshot(candles)

        trust = self._coin_trust_engine.analyze(symbol)
        bitcoin_score = self._bitcoin_engine.score_for_direction(direction, bitcoin_snapshot, symbol=symbol)
        market_health_score = market_health_snapshot.market_health_score if market_health_snapshot is not None else 50.0
        smart_money_snapshot = await self._smart_money_engine.get_snapshot(symbol)
        smart_money_score = self._smart_money_engine.score_for_direction(direction, smart_money_snapshot).score
        structural_target = self._forward_structural_target(structure, direction, current_price)
        recent_move_pct = recent_price_change_pct(candles, self._ATR_PERIOD)

        return self._signal_generation_engine.generate(
            symbol=symbol, direction=direction, entry_price=current_price, atr=atr_value,
            structure=structure, bitcoin_score=bitcoin_score, market_health_score=market_health_score,
            coin_trust_score=trust.trust_score, smart_money_score=smart_money_score,
            structural_target=structural_target, momentum=momentum, recent_move_pct=recent_move_pct,
        )

    @staticmethod
    def _build_momentum_snapshot(candles) -> MomentumSnapshot:
        """
        Latest RSI/MACD/ADX/volume_ratio reading for `ConfidenceEngine`'s
        momentum-confirmation adjustment (see that module's docstring).
        `candles` already has `_STRUCTURE_CANDLE_LIMIT` (200) 15m candles
        by the time this is called -- comfortably past every one of these
        indicators' warm-up (MACD's ~35-candle need is the longest), so
        `[-1]` is always a real value here, same assumption
        `engines.bitcoin_intelligence` already makes for its own ADX read.
        """
        close_values = compute_closes(candles)
        macd_result = compute_macd(close_values)
        adx_result = compute_adx(candles)
        return MomentumSnapshot(
            rsi=compute_rsi(close_values)[-1],
            macd_histogram=macd_result.histogram[-1],
            plus_di=adx_result.plus_di[-1],
            minus_di=adx_result.minus_di[-1],
            adx=adx_result.adx[-1],
            volume_ratio=compute_volume_ratio(candles)[-1],
        )

    @staticmethod
    def _forward_structural_target(structure, direction: SignalDirection, current_price: float) -> Optional[float]:
        """
        Only a swing level genuinely AHEAD of current price, in the
        trade's favor, is a meaningful take-profit target. `last_swing_high`
        /`last_swing_low` are simply "the most recent swing point in the
        data" -- after a fresh BOS, the most recent swing high is BEHIND
        (below) price, not ahead of it, and would be a nonsensical LONG
        target (RiskManagementEngine's clamp would pull TP1 to a level
        below entry). Passing None in that case correctly falls back to
        the pure ATR/RR-tier target instead.
        """
        if direction == SignalDirection.LONG:
            target = structure.last_swing_high
            return target if (target is not None and target > current_price) else None
        target = structure.last_swing_low
        return target if (target is not None and target < current_price) else None

    @staticmethod
    def _deduplicate(symbols: list[str]) -> list[str]:
        """Preserve first-seen order while dropping repeats (a defensive guard -- `list_active()` should already be unique)."""
        seen: set[str] = set()
        result: list[str] = []
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                result.append(symbol)
        return result

    def _apply_max_symbols_cap(self, surviving_symbols: tuple[str, ...]) -> list[str]:
        """
        Truncates Stage 1's survivors to `ScannerConfig.max_symbols_to_analyze`
        (`None` -- the default -- means no cap, every survivor proceeds to
        Stage 2, unchanged from before this config field existed). Keeps
        Stage 1's own order rather than re-sorting by volume or any other
        signal -- see that field's docstring in config/schema.py for why.
        """
        cap = self._config.scanner.max_symbols_to_analyze
        symbols = list(surviving_symbols)
        if cap is None or len(symbols) <= cap:
            return symbols
        _logger.info(
            "max_symbols_to_analyze=%d: analyzing %d of %d Stage 1 survivors this cycle",
            cap, cap, len(symbols),
        )
        return symbols[:cap]

    @staticmethod
    async def _safe_call(label: str, coro_fn: Callable[[], Awaitable[_T]]) -> Optional[_T]:
        """Run one async step, isolating its failure from the rest of the scan cycle."""
        try:
            return await coro_fn()
        except Exception as exc:  # noqa: BLE001 - isolate this step's failure from the whole cycle
            handle_error(DataValidationError(f"{label} failed during scan cycle", cause=exc), category="trading")
            return None
