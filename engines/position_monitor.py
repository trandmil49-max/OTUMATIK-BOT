"""
engines/position_monitor.py

Position Monitor / Trade Management Engine (SRS Part 10): the ONLY code
that transitions a Signal/Trade through its lifecycle
(WAITING -> ACTIVE -> TP1_HIT/STOP_LOSS/EXPIRED). Single-TP model: TP1_HIT
is a full, final close -- there is no partial exit, no break-even stop
move, and no second target. Intended to run on a short interval (SRS:
"30-second position monitoring"), checking the current price of every
WAITING signal and open trade against its stored levels.

Signal-only scope (SRS Part 1/21 Master Rules: "this bot NEVER executes
trades"): this engine tracks what WOULD happen to a position and updates
the database/coin statistics accordingly. It is the source of truth the
Telegram Engine (and, once autonomous trading is enabled, TradeExecutionEngine
via execution_modes/live.py) reads for "TP1 hit!" style notifications and
real order closes -- it never calls an exchange order-placement endpoint
itself.

Entry-fill model: a Signal's `entry_price` is the market price AT THE
MOMENT `SignalGenerationEngine` created it (a market-style entry, not a
resting limit order far from price -- see that engine's docstring). This
engine therefore activates a WAITING signal into an ACTIVE trade on the
very next tick it observes fresh price data for that symbol, rather than
modeling limit-order distance-to-fill; a signal only expires un-activated
if the monitor genuinely does not run for `RiskConfig.signal_lifetime_hours`
(e.g. an outage).

Duplicate-processing prevention: every transition is gated on the trade's
CURRENT persisted status (re-read from `get_active_trades()` at the top of
every `run_tick()` call) rather than any in-memory flag, so calling
`run_tick()` repeatedly -- even concurrently, since each check-and-write
goes through `Database.transaction()`'s `BEGIN IMMEDIATE` -- can never
fire the same transition twice: a closed trade no longer appears in
`get_active_trades()` at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import SignalDirection, Trade, TradeStatus
from infrastructure.database.repositories.coin_repository import CoinStatisticsRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.logging_setup import get_logger

_logger = get_logger("trading")


@dataclass(frozen=True)
class MonitorTickResult:
    """Everything that changed during one `PositionMonitorEngine.run_tick()` call."""

    activated_trade_ids: tuple[int, ...] = ()
    expired_signal_ids: tuple[int, ...] = ()
    tp1_hit_trade_ids: tuple[int, ...] = ()
    stop_loss_trade_ids: tuple[int, ...] = ()
    trailing_stop_exit_trade_ids: tuple[int, ...] = ()


class PositionMonitorEngine:
    """Advances every WAITING signal and open trade through its lifecycle given a snapshot of current prices."""

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        signal_repository: Optional[SignalRepository] = None,
        trade_repository: Optional[TradeRepository] = None,
        coin_statistics_repository: Optional[CoinStatisticsRepository] = None,
    ) -> None:
        self._config = config or get_config()
        self._signal_repository = signal_repository or SignalRepository()
        self._trade_repository = trade_repository or TradeRepository()
        self._coin_statistics_repository = coin_statistics_repository or CoinStatisticsRepository()

    def run_tick(self, current_prices: dict[str, float]) -> MonitorTickResult:
        """
        One monitoring pass: activate waiting signals first (so a
        brand-new trade can still be checked against SL/TP within the
        same tick), then advance every open trade.

        Args:
            current_prices: symbol -> latest price. Symbols absent from
                this dict are simply skipped this tick (no data yet),
                never treated as an error.
        """
        activated, expired = self._activate_waiting_signals(current_prices)
        tp1_hits, stop_losses, trailing_exits = self._advance_active_trades(current_prices)
        return MonitorTickResult(
            activated_trade_ids=activated,
            expired_signal_ids=expired,
            tp1_hit_trade_ids=tp1_hits,
            stop_loss_trade_ids=stop_losses,
            trailing_stop_exit_trade_ids=trailing_exits,
        )

    def _activate_waiting_signals(self, current_prices: dict[str, float]) -> tuple[tuple[int, ...], tuple[int, ...]]:
        now = datetime.now(timezone.utc)
        activated_trade_ids: list[int] = []
        expired_signal_ids: list[int] = []

        for signal in self._signal_repository.find_by_status(TradeStatus.WAITING):
            price = current_prices.get(signal.symbol)
            if price is None:
                age_hours = (now - signal.created_at).total_seconds() / 3600.0
                if age_hours >= self._config.risk.signal_lifetime_hours:
                    self._signal_repository.update_status(
                        signal.id, TradeStatus.EXPIRED, trade_result=TradeStatus.EXPIRED.value
                    )
                    expired_signal_ids.append(signal.id)
                continue

            trade = self._trade_repository.create(
                Trade(
                    signal_id=signal.id, symbol=signal.symbol, direction=signal.direction,
                    entry_price=signal.entry_price, entry_time=now,
                    initial_stop_loss=signal.stop_loss, current_stop_loss=signal.stop_loss,
                    take_profit_1=signal.take_profit_1,
                    confidence_score=signal.confidence_score, leverage=signal.leverage,
                    coin_trust_score=signal.coin_trust_score, risk_score=signal.risk_score,
                    bitcoin_score=signal.bitcoin_score, status=TradeStatus.ACTIVE,
                )
            )
            self._signal_repository.update_status(signal.id, TradeStatus.ACTIVE)
            activated_trade_ids.append(trade.id)
            _logger.info(
                "Signal %d activated into trade %d (%s @ %.8f)", signal.id, trade.id, signal.symbol, signal.entry_price
            )

        return tuple(activated_trade_ids), tuple(expired_signal_ids)

    def _advance_active_trades(
        self, current_prices: dict[str, float]
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        tp1_hits: list[int] = []
        stop_losses: list[int] = []
        trailing_exits: list[int] = []
        now = datetime.now(timezone.utc)

        for trade in self._trade_repository.get_active_trades():
            price = current_prices.get(trade.symbol)
            if price is None:
                continue

            if self._hit_level(trade, price, trade.current_stop_loss, favorable=False):
                self._close_trade(trade, price, now, TradeStatus.STOP_LOSS, "Stop loss hit")
                stop_losses.append(trade.id)
                continue

            if self._hit_level(trade, price, trade.take_profit_1, favorable=True):
                self._close_trade(trade, price, now, TradeStatus.TP1_HIT, "Take profit hit")
                tp1_hits.append(trade.id)
                continue

            if self._config.risk.trailing_stop_enabled and self._check_trailing_stop(trade, price):
                self._close_trade(trade, price, now, TradeStatus.TRAILING_STOP_EXIT, "Trailing stop: profit lock")
                trailing_exits.append(trade.id)

        return tuple(tp1_hits), tuple(stop_losses), tuple(trailing_exits)

    def _check_trailing_stop(self, trade: Trade, price: float) -> bool:
        """
        Profit-lock early exit (platform owner's explicit request): once
        a trade has moved into profit at least once, track the best
        price seen (`Trade.best_price_since_entry`) and signal an exit if
        price has since retraced `trailing_stop_atr_multiple` times the
        ORIGINAL entry-time ATR from that best point -- closing early to
        bank the gain rather than risk giving it all back waiting for a
        TP that may never come. Checked AFTER the hard TP1/stop-loss
        checks in `_advance_active_trades()`, never before: a genuine
        TP1 or stop-loss hit always takes priority over this softer,
        volatility-scaled signal.

        The "original ATR" is recovered from the trade's own already
        -stored risk distance (`abs(entry_price - initial_stop_loss) /
        atr_stop_loss_multiplier`) rather than a freshly re-fetched live
        ATR reading -- this needs no extra candle-history fetch per open
        trade per tick (this method only ever sees the price snapshot
        already passed into `run_tick()`), and the entry-time volatility
        reading is a perfectly reasonable proxy for a trade's likely
        lifetime, which is short on this platform's timeframes.
        """
        is_long = trade.direction == SignalDirection.LONG
        is_in_profit_now = (price > trade.entry_price) if is_long else (price < trade.entry_price)

        if is_in_profit_now:
            candidate_best = max(trade.best_price_since_entry or trade.entry_price, price) if is_long \
                else min(trade.best_price_since_entry or trade.entry_price, price)
            if candidate_best != trade.best_price_since_entry:
                self._trade_repository.update_best_price(trade.id, candidate_best)
                trade.best_price_since_entry = candidate_best

        if trade.best_price_since_entry is None:
            return False  # never been in profit yet -- nothing to trail

        atr_multiplier = self._config.risk.atr_stop_loss_multiplier
        original_atr = abs(trade.entry_price - trade.initial_stop_loss) / atr_multiplier
        trailing_distance = self._config.risk.trailing_stop_atr_multiple * original_atr

        retracement = (trade.best_price_since_entry - price) if is_long else (price - trade.best_price_since_entry)
        return retracement >= trailing_distance

    @staticmethod
    def _hit_level(trade: Trade, price: float, level: float, *, favorable: bool) -> bool:
        """
        `favorable=True` checks a take-profit-style crossing (price moved
        toward profit); `favorable=False` checks a stop-loss-style
        crossing (price moved toward loss). Direction-aware, using
        explicit `>=`/`<=` (not a boolean negation of the other) so a
        price landing exactly on the level is always inclusive on
        whichever side matters, rather than falling through a strict
        `<`/`>` by accident.
        """
        use_greater_or_equal = (trade.direction == SignalDirection.LONG) == favorable
        return price >= level if use_greater_or_equal else price <= level

    def _close_trade(self, trade: Trade, exit_price: float, exit_time: datetime, status: TradeStatus, reason: str) -> None:
        pnl_percent = self._realized_pnl_percent(trade, exit_price)
        if status == TradeStatus.TP1_HIT:
            self._trade_repository.record_tp1_hit(trade.id, exit_time, exit_price)
        self._trade_repository.close_trade(
            trade.id, status=status, exit_price=exit_price, exit_time=exit_time,
            realized_pnl_percent=pnl_percent, exit_reason=reason,
        )
        self._signal_repository.update_status(trade.signal_id, status, trade_result=status.value)

        if status == TradeStatus.TP1_HIT:
            outcome = "WIN"
        elif status == TradeStatus.STOP_LOSS:
            outcome = "LOSS"
        else:
            # TRAILING_STOP_EXIT: almost always non-negative in practice
            # (a hard stop-loss hit is checked first, every tick), but
            # classified by the ACTUAL realized sign rather than assumed,
            # since a single volatile tick could in principle whipsaw
            # through breakeven before the trailing distance triggers.
            outcome = "WIN" if pnl_percent >= 0 else "LOSS"
        duration_seconds = (exit_time - trade.entry_time).total_seconds()

        self._coin_statistics_repository.record_trade_outcome(
            trade.symbol, outcome,
            rr=self._realized_risk_reward(trade, exit_price),
            confidence_score=trade.confidence_score,
            duration_seconds=duration_seconds,
            hit_tp1=(status == TradeStatus.TP1_HIT),
            hit_sl=(status == TradeStatus.STOP_LOSS),
        )
        _logger.info(
            "Trade %d closed: %s %s @ %.8f (%s, pnl=%.2f%%)",
            trade.id, trade.symbol, status.value, exit_price, reason, pnl_percent,
        )

    @staticmethod
    def _realized_pnl_percent(trade: Trade, exit_price: float) -> float:
        direction_sign = 1.0 if trade.direction == SignalDirection.LONG else -1.0
        return ((exit_price - trade.entry_price) / trade.entry_price) * 100.0 * direction_sign

    @staticmethod
    def _realized_risk_reward(trade: Trade, exit_price: float) -> float:
        risk_distance = abs(trade.entry_price - trade.initial_stop_loss)
        if risk_distance == 0:
            return 0.0
        reward_distance = abs(exit_price - trade.entry_price)
        return reward_distance / risk_distance

