"""
engines/trade_execution.py

TradeExecutionEngine (autonomous trading pivot): the ONLY code in this
platform that places or cancels REAL exchange orders. Everything
upstream (signal generation, risk management, confidence scoring) still
only ever produces a `Signal` -- this engine is what turns an approved
signal into an actual exchange position once autonomous trading is
switched on. This reverses the codebase's original signal-only master
rule ("this bot NEVER executes trades"), done at the platform owner's
explicit request.

EXCHANGE SPLIT (platform owner's explicit choice): real order execution
goes to BingX (via `infrastructure.bingx.client.BingXFuturesClient`,
injected here as `execution_client`), NOT Binance -- BingX does not
mandate an IP allowlist for Futures trading permission on an API key,
so it works from a host with no static outbound IP, unlike Binance.
Every market-data feed used for ANALYSIS (candles, funding rate, open
interest, Smart Money top-trader ratio) stays on Binance regardless --
see `infrastructure.bingx.client`'s module docstring for the full
reasoning. This engine is written against the `TradingClient` Protocol
below, not either concrete client class, so it works with whichever
client is injected.

Two independent safety switches must BOTH be true before a real order is
ever placed -- see `trading_is_enabled`: `RunMode.LIVE` (the platform's
existing top-level run mode) AND `config.api.trading_enabled` (a second,
dedicated flag). Deliberately redundant: flipping `run_mode` to LIVE for
some unrelated reason (e.g. testing the live scan cadence against real
market data) can never, by itself, start placing real orders -- both
flags have to be set on purpose.

`open_position()` distinguishes two very different kinds of "did not
open a position":
    * Expected, routine skip conditions (trading switched off, no
      available balance, symbol not in exchange info, computed size
      rounds to zero, dynamic position cap already reached) -- returns `None` silently, exactly like a
      rejected signal elsewhere in this platform. Not an error.
    * `signal.id is None` -- a programming error (this method requires
      an already-persisted signal, since order client-IDs and the
      resulting Trade row both need it) -- raises `ValueError`.
    * Anything else (a signed request itself failing after the exchange
      accepted it, a malformed response) propagates normally. Swallowing
      those here would risk hiding a partially-open, unprotected
      position -- silence is only safe for the routine skip conditions
      above, never for a failure mid-sequence.

`close_position()` is the opposite: it NEVER raises. Its job is closing
whatever is left of the position -- for a TP1/STOP_LOSS close, the
exchange's own resting order already did that, so there is only cleanup
(cancelling the OTHER resting order); for a TRAILING_STOP_EXIT, NOTHING
has closed the position yet (there is no resting order for "price
retraced from its best point" -- this platform decided that, not the
exchange), so this method actively places a MARKET order to close
whatever quantity is still open before cancelling the leftover resting
orders. Any failure along the way is logged, not propagated -- the
trade is already marked closed in this platform's own records by the
time this runs; a failure here is a follow-up-and-fix situation, not a
reason to blow up the caller (`execution_modes/live.py`'s notification
dispatch).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional, Protocol

from config.loader import get_config
from config.schema import PlatformConfig, RunMode
from core.models import Signal, SignalDirection, Trade, TradeStatus
from infrastructure.binance.models import AccountBalance, OrderResult, SymbolInfo
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.logging_setup import get_logger

_logger = get_logger("trading")

# Binance isolated margin -- chosen so one position's loss can never
# cascade into liquidating unrelated positions sharing cross margin.
# Not configurable: this platform always risks a small, fixed % of
# balance per trade (see RiskConfig.risk_per_trade_percent), so the
# capital-efficiency case for cross margin does not apply here.
_MARGIN_TYPE = "ISOLATED"


class TradingClient(Protocol):
    """
    The subset of exchange-client methods TradeExecutionEngine actually
    needs -- satisfied by both `infrastructure.binance.client
    .BinanceFuturesClient` and `infrastructure.bingx.client
    .BingXFuturesClient` (the platform routes real order execution to
    BingX specifically, per this engine's module docstring, but the
    engine itself is written against this interface, not either
    concrete class, so it never has to care which exchange it is
    actually talking to).
    """

    async def get_account_balance(self) -> list[AccountBalance]: ...
    async def get_exchange_info(self) -> list[SymbolInfo]: ...
    async def get_current_price(self, symbol: str) -> Optional[float]: ...
    async def get_position_risk(self, symbol: str): ...
    async def set_margin_type(self, symbol: str, margin_type: str = ...) -> None: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    async def place_order(
        self, symbol: str, side: str, order_type: str, *,
        quantity: Optional[float] = None, stop_price: Optional[float] = None,
        close_position: bool = False, client_order_id: Optional[str] = None,
    ) -> OrderResult: ...
    async def cancel_all_open_orders(self, symbol: str) -> None: ...


class TradeExecutionEngine:
    """Opens/closes REAL Binance positions for approved signals, when autonomous trading is switched on."""

    def __init__(
        self,
        execution_client: TradingClient,
        config: Optional[PlatformConfig] = None,
        trade_repository: Optional[TradeRepository] = None,
        signal_repository: Optional[SignalRepository] = None,
    ) -> None:
        self._client = execution_client
        self._config = config or get_config()
        self._trade_repository = trade_repository or TradeRepository()
        self._signal_repository = signal_repository or SignalRepository()

    @property
    def trading_is_enabled(self) -> bool:
        """Both independent switches must be True -- see module docstring."""
        return self._config.general.run_mode == RunMode.LIVE and self._config.api.trading_enabled

    async def open_position(self, signal: Signal) -> Optional[Trade]:
        """
        Size, open, and protect a real position for `signal`, then
        persist the resulting `Trade` -- see module docstring for the
        None/raise/propagate rules.

        Cross-exchange price safeguard: `signal`'s entry/stop/take-profit
        were computed from Binance's price at analysis time (see module
        docstring's EXCHANGE SPLIT), but the order is placed on a
        different exchange. Binance and BingX prices track closely but
        are never guaranteed identical -- especially on lower-liquidity
        symbols -- so this method re-anchors all three levels to the
        execution venue's OWN current price immediately before sizing or
        placing anything, via `_rebase_levels_to_execution_price()`. The
        RISK (distance to stop) and REWARD (distance to take-profit) the
        signal was approved for are preserved exactly; only where they
        sit is shifted to match reality on the venue that will actually
        fill the order.

        Sequencing is deliberate and matters: margin type + leverage are
        set BEFORE the entry order (the exchange does not accept either
        as an order parameter itself), the entry order fires before the
        protective STOP_MARKET/TAKE_PROFIT_MARKET orders (there is
        nothing to protect until a position exists), and the `Trade` row
        is only written -- with the signal flipped to `ACTIVE` in the
        same step -- once all three orders exist, so `PositionMonitorEngine`
        can never race this method into creating a second, duplicate
        "paper" Trade for the same signal.
        """
        if not self.trading_is_enabled:
            return None
        if signal.id is None:
            raise ValueError("open_position() requires a persisted signal (signal.id is not None)")

        balances = await self._client.get_account_balance()
        usdt_balance = next((b for b in balances if b.asset == "USDT"), None)
        if usdt_balance is None or usdt_balance.available_balance <= 0:
            _logger.info("Skipping signal %d (%s): no available USDT balance", signal.id, signal.symbol)
            return None

        if self._config.risk.dynamic_position_cap_enabled:
            position_cap = self._dynamic_position_cap(usdt_balance.available_balance)
            open_real_trades = len(
                [t for t in self._trade_repository.get_active_trades() if t.entry_order_id is not None]
            )
            if open_real_trades >= position_cap:
                _logger.info(
                    "Skipping signal %d (%s): %d real position(s) already open, at the %d-position cap for a $%.2f balance",
                    signal.id, signal.symbol, open_real_trades, position_cap, usdt_balance.available_balance,
                )
                return None

        symbols = await self._client.get_exchange_info()
        symbol_info = next((s for s in symbols if s.symbol == signal.symbol), None)
        if symbol_info is None:
            _logger.warning("Skipping signal %d: %s not found in the execution venue's exchange info", signal.id, signal.symbol)
            return None

        execution_price = await self._client.get_current_price(signal.symbol)
        if execution_price is None or execution_price <= 0:
            _logger.warning("Skipping signal %d (%s): could not fetch the execution venue's current price", signal.id, signal.symbol)
            return None

        rebased_stop_loss, rebased_take_profit = self._rebase_levels_to_execution_price(signal, execution_price)

        quantity = self._calculate_quantity(signal, execution_price, rebased_stop_loss, usdt_balance.available_balance, symbol_info)
        if quantity <= 0:
            _logger.info("Skipping signal %d (%s): position size rounds to zero at current balance", signal.id, signal.symbol)
            return None

        await self._client.set_margin_type(signal.symbol, _MARGIN_TYPE)
        await self._client.set_leverage(signal.symbol, signal.leverage)

        entry_side = "BUY" if signal.direction == SignalDirection.LONG else "SELL"
        exit_side = "SELL" if signal.direction == SignalDirection.LONG else "BUY"

        entry_order = await self._client.place_order(
            signal.symbol, entry_side, "MARKET",
            quantity=quantity, client_order_id=f"colde-{signal.id}-entry",
        )
        stop_order = await self._client.place_order(
            signal.symbol, exit_side, "STOP_MARKET", stop_price=rebased_stop_loss,
            close_position=True, client_order_id=f"colde-{signal.id}-sl",
        )
        take_profit_order = await self._client.place_order(
            signal.symbol, exit_side, "TAKE_PROFIT_MARKET", stop_price=rebased_take_profit,
            close_position=True, client_order_id=f"colde-{signal.id}-tp",
        )

        # A resting conditional order (STOP_MARKET/TAKE_PROFIT_MARKET) has
        # not filled, so its avg_price is 0.0; only the MARKET entry order
        # can have a real fill price here. Falling back to execution_price
        # (the execution venue's price at the moment we sized/placed this
        # order, already re-anchored above) is the closest available
        # estimate for the rare case avg_price comes back 0.0 for the
        # entry order too (e.g. a delayed fill report).
        actual_entry_price = entry_order.avg_price if entry_order.avg_price > 0 else execution_price

        trade = self._trade_repository.create(
            Trade(
                signal_id=signal.id, symbol=signal.symbol, direction=signal.direction,
                entry_price=actual_entry_price, entry_time=datetime.now(timezone.utc),
                initial_stop_loss=rebased_stop_loss, current_stop_loss=rebased_stop_loss,
                take_profit_1=rebased_take_profit, confidence_score=signal.confidence_score,
                leverage=signal.leverage, coin_trust_score=signal.coin_trust_score,
                risk_score=signal.risk_score, bitcoin_score=signal.bitcoin_score,
                status=TradeStatus.ACTIVE,
                entry_order_id=entry_order.order_id,
                stop_order_id=stop_order.order_id,
                take_profit_order_id=take_profit_order.order_id,
            )
        )
        # Flips WAITING -> ACTIVE so PositionMonitorEngine's own
        # _activate_waiting_signals() never sees this signal again and
        # creates a second, duplicate paper Trade for it.
        self._signal_repository.update_status(signal.id, TradeStatus.ACTIVE)

        _logger.info(
            "Opened real position for signal %d: %s %s qty=%s @ %.8f (entry=%s, sl=%s, tp=%s)",
            signal.id, signal.symbol, entry_side, quantity, actual_entry_price,
            entry_order.order_id, stop_order.order_id, take_profit_order.order_id,
        )
        return trade

    def _dynamic_position_cap(self, available_balance: float) -> int:
        """
        Balance-scaled concurrent-position cap for REAL trading -- see
        RiskConfig.dynamic_position_cap_* fields' docstring for the
        formula and reasoning. Capped at `max_active_trades`: the dynamic
        formula can only ever be as strict as or stricter than the
        platform's overall portfolio ceiling, never looser.
        """
        cfg = self._config.risk
        extra_steps = max(0.0, available_balance - cfg.dynamic_position_cap_base_balance) // cfg.dynamic_position_cap_balance_step
        cap = cfg.dynamic_position_cap_base_count + int(extra_steps)
        return min(cap, cfg.max_active_trades)

    async def close_position(self, trade: Trade) -> None:
        """
        Closes whatever is left of the real position for `trade`, then
        cancels any still-resting protective order -- see module
        docstring for the TP1/STOP_LOSS-vs-TRAILING_STOP_EXIT distinction
        and why this never raises.
        """
        if not self.trading_is_enabled:
            return

        try:
            position = await self._client.get_position_risk(trade.symbol)
        except Exception:
            _logger.warning(
                "Failed to read position risk for %s (trade %s) before closing -- skipping the market-close step",
                trade.symbol, trade.id, exc_info=True,
            )
            position = None

        if position is not None and position.position_amount != 0:
            exit_side = "SELL" if position.position_amount > 0 else "BUY"
            try:
                await self._client.place_order(
                    trade.symbol, exit_side, "MARKET",
                    quantity=abs(position.position_amount), close_position=True,
                    client_order_id=f"colde-{trade.signal_id}-exit",
                )
            except Exception:
                _logger.warning(
                    "Failed to market-close the remaining position for %s (trade %s) -- may need manual intervention",
                    trade.symbol, trade.id, exc_info=True,
                )

        try:
            await self._client.cancel_all_open_orders(trade.symbol)
        except Exception:
            _logger.warning(
                "Failed to cancel remaining open orders for %s (trade %s) -- may need manual cleanup",
                trade.symbol, trade.id, exc_info=True,
            )

    def _rebase_levels_to_execution_price(self, signal: Signal, execution_price: float) -> tuple[float, float]:
        """
        Re-anchors `signal`'s stop-loss and take-profit to the execution
        venue's current price, preserving the exact risk distance
        (entry-to-stop) and reward distance (entry-to-take-profit) the
        signal was approved for -- see `open_position()`'s docstring.
        Direction-aware: a LONG's stop sits below entry and its target
        above; a SHORT is the mirror image.
        """
        if signal.direction == SignalDirection.LONG:
            stop_distance = signal.entry_price - signal.stop_loss
            reward_distance = signal.take_profit_1 - signal.entry_price
            return execution_price - stop_distance, execution_price + reward_distance
        stop_distance = signal.stop_loss - signal.entry_price
        reward_distance = signal.entry_price - signal.take_profit_1
        return execution_price + stop_distance, execution_price - reward_distance

    def _calculate_quantity(
        self, signal: Signal, execution_price: float, stop_loss: float, available_balance: float, symbol_info
    ) -> float:
        """
        Fixed-%-of-balance risk sizing (RiskConfig.risk_per_trade_percent):
        `risk_amount` is the dollar amount actually lost if the stop is
        hit, not the position's notional value. Scales gently with
        balance with this ONE parameter -- cents at risk on a $12
        balance, a few dollars at $100 -- so no separate small-balance/
        large-balance threshold logic is needed (see RiskConfig's field
        docstring). Sized against `execution_price`/`stop_loss` (the
        execution venue's own current price and the already-rebased
        stop -- see `open_position()`), not the signal's original
        Binance-time levels, so the dollar amount actually at risk
        matches what will really happen on the exchange placing the order.

        Margin-capped: `notional` can never exceed what the account can
        actually margin at `signal.leverage`, even if the risk-based
        notional would want more (e.g. an unusually tight stop).

        Returns 0.0 (never negative, never raises) if the sized notional
        falls below the symbol's exchange-enforced minimum -- the caller
        treats that as "skip this signal", not an error.
        """
        risk_amount = available_balance * (self._config.risk.risk_per_trade_percent / 100.0)
        stop_distance_pct = abs(execution_price - stop_loss) / execution_price
        if stop_distance_pct <= 0:
            return 0.0

        notional = risk_amount / stop_distance_pct
        notional = min(notional, available_balance * signal.leverage)

        if symbol_info.min_notional_usdt is not None and notional < symbol_info.min_notional_usdt:
            return 0.0

        quantity = notional / execution_price
        if symbol_info.step_size > 0:
            # Add a tiny epsilon before flooring: a mathematically exact multiple
            # of step_size (e.g. 60.0 / 0.1) can land a hair under the intended
            # integer step count due to plain binary floating-point rounding
            # (0.6 / 0.1 == 5.999999999999999, not 6.0), which would otherwise
            # silently under-size by one whole step.
            quantity = math.floor(quantity / symbol_info.step_size + 1e-9) * symbol_info.step_size
        return quantity
