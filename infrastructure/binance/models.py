"""
infrastructure/binance/models.py

Typed response models for Binance USDT-M Futures public market-data
endpoints (SRS Part 6 BINANCE FUTURES ENGINE). Every field the platform
actually needs is named explicitly here rather than passing raw dicts
around, so a typo'd JSON key surfaces immediately at the parsing boundary
(`BinanceFuturesClient._parse_*`) instead of silently propagating `None`
deep into an indicator calculation.

All are frozen (immutable) -- these represent a fact observed from the
exchange at a point in time and are never mutated after parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Candle:
    """One OHLCV kline (SRS Part 5/6: multi-timeframe candle data -- 5M/15M/30M/1H)."""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime
    quote_volume: float
    num_trades: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float


@dataclass(frozen=True)
class SymbolInfo:
    """A USDT-M perpetual symbol, from GET /fapi/v1/exchangeInfo."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    price_precision: int
    quantity_precision: int
    tick_size: float
    step_size: float
    min_notional_usdt: Optional[float] = None

    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"


@dataclass(frozen=True)
class Ticker24hr:
    """GET /fapi/v1/ticker/24hr -- rolling 24h stats used by the Stage 1 fast filter (SRS Part 16)."""

    symbol: str
    last_price: float
    price_change_percent: float
    quote_volume: float
    high_price: float
    low_price: float
    weighted_avg_price: float


@dataclass(frozen=True)
class FundingRate:
    """GET /fapi/v1/premiumIndex -- current funding rate + mark price (SRS Part 8 funding conflict check)."""

    symbol: str
    mark_price: float
    funding_rate: float
    next_funding_time: datetime


@dataclass(frozen=True)
class OpenInterest:
    """GET /fapi/v1/openInterest (SRS Part 8/14 open interest conflict check)."""

    symbol: str
    open_interest: float
    timestamp: datetime


@dataclass(frozen=True)
class TopTraderRatio:
    """
    GET /futures/data/topLongShortAccountRatio and
    /futures/data/topLongShortPositionRatio -- Binance's own official,
    public, no-auth "top trader" long/short positioning (Module 23 Smart
    Money Engine). This is the documented equivalent of the consumer
    Smart Money page's aggregate positioning: Binance's futures API has
    no endpoint for that page's per-trader nickname/whale feed (that data
    is only ever rendered client-side, not exposed via API), but the
    SAME underlying "how are the top accounts by margin balance / by
    position size positioned right now" signal these ratios describe IS
    officially published -- see this engine's module docstring.

    `account_ratio` reads `/topLongShortAccountRatio`: among the top 20%
    of accounts by margin balance, the ratio of long accounts to short
    accounts (>1.0 means more accounts are long than short).
    `position_ratio` reads `/topLongShortPositionRatio`: the same top-20%
    cohort, but weighted by position SIZE rather than account count
    (>1.0 means more long notional than short notional). The two can
    disagree (many small accounts long, but the biggest few positions
    short) -- both are kept rather than averaged, so the engine consuming
    this can treat that disagreement as its own signal.
    """

    symbol: str
    account_ratio: float
    position_ratio: float
    timestamp: datetime


@dataclass(frozen=True)
class BookTicker:
    """GET /fapi/v1/ticker/bookTicker -- best bid/ask, used for spread quality (SRS Part 6/7)."""

    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float

    @property
    def spread_percent(self) -> float:
        """Bid-ask spread as a percentage of the mid price; 0.0 if either side of the book is empty."""
        if self.bid_price <= 0 or self.ask_price <= 0:
            return 0.0
        mid_price = (self.bid_price + self.ask_price) / 2
        return ((self.ask_price - self.bid_price) / mid_price) * 100.0


# ─────────────────────────────────────────────────────────────────────────
# AUTHENTICATED / TRADING ENDPOINTS (autonomous trading pivot)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountBalance:
    """GET /fapi/v2/balance (signed) -- one asset's balance, filtered to USDT by the client."""

    asset: str
    balance: float
    available_balance: float


@dataclass(frozen=True)
class PositionRisk:
    """GET /fapi/v2/positionRisk (signed) -- current exposure for one symbol, one-way mode assumed."""

    symbol: str
    position_amount: float  # signed: positive = long, negative = short, 0 = flat
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int

    @property
    def is_flat(self) -> bool:
        return self.position_amount == 0.0


@dataclass(frozen=True)
class OrderResult:
    """
    POST /fapi/v1/order (signed) response, trimmed to the fields
    TradeExecutionEngine actually needs. `avg_price` is 0.0 for an order
    that hasn't filled yet (e.g. a STOP_MARKET/TAKE_PROFIT_MARKET placed
    as a resting conditional order) -- callers must treat 0.0 as "not
    filled", never as a real average price.
    """

    order_id: int
    client_order_id: str
    symbol: str
    status: str
    avg_price: float
    executed_qty: float
