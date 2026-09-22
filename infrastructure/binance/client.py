"""
infrastructure/binance/client.py

Async REST client for Binance USDT-M Futures (SRS Part 6 BINANCE FUTURES
ENGINE, extended for the autonomous-trading pivot).

Two request families, deliberately kept apart:
    * PUBLIC market-data endpoints (klines, ticker, funding, open interest,
      top-trader ratio, exchange info) -- no signing, safe to cache, safe
      to retry.
    * SIGNED account/trading endpoints (balance, position risk, leverage,
      margin type, order placement/cancellation) -- HMAC-SHA256 signed
      with `config.api.binance_api_key/secret`, per Binance's documented
      scheme (query string, minus the `signature` param itself, signed
      with the secret; see `_sign_params()`). Signed WRITES (order
      placement/cancellation) are NEVER auto-retried -- see
      `_signed_write()`'s docstring for why.

Cross-cutting concerns composed here rather than duplicated per endpoint:
    * `SlidingWindowRateLimiter` (`infrastructure/rate_limiter.py`) --
      proactive weight-budget throttling, shared with the Telegram
      client (Module 15) rather than duplicated.
    * `system.retry.retry_async` -- transient-failure retry with backoff,
      applied ONLY to `BinanceConnectionError` / `BinanceRateLimitError`
      (never to `BinanceDataError` -- a malformed payload will not fix
      itself by retrying the identical request), and ONLY to public/
      signed-GET requests, never to signed writes.
    * A simple TTL cache -- keyed by (path, sorted params), so a Stage 1
      scan across hundreds of symbols within one `cache_lifetime_seconds`
      window does not needlessly re-fetch identical data (SRS Part 6:
      "Cache repeated data. Do not query the same data twice."). Signed
      endpoints are never cached (account state changes independently of
      this process, e.g. from a trade closing).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from config.loader import get_config
from config.schema import PlatformConfig
from infrastructure.binance.models import (
    AccountBalance,
    BookTicker,
    Candle,
    FundingRate,
    OpenInterest,
    OrderResult,
    PositionRisk,
    SymbolInfo,
    Ticker24hr,
    TopTraderRatio,
)
from infrastructure.rate_limiter import SlidingWindowRateLimiter
from system.exceptions import BinanceAPIError, BinanceConnectionError, BinanceDataError, BinanceRateLimitError
from system.logging_setup import get_logger
from system.retry import retry_async

_logger = get_logger("api")

# Binance-documented request weights (per call) for the endpoints this client uses.
# See https://binance-docs.github.io/apidocs/futures/en/#limits -- kept as named
# constants (not re-typed at each call site) per SOLID/DRY.
_WEIGHT_EXCHANGE_INFO = 1
_WEIGHT_KLINES_SMALL = 5   # limit <= 100
_WEIGHT_KLINES_MEDIUM = 10  # limit <= 500
_WEIGHT_KLINES_LARGE = 20  # limit > 500
_WEIGHT_TICKER_24HR_SINGLE = 1
_WEIGHT_TICKER_24HR_ALL = 40
_WEIGHT_PREMIUM_INDEX = 1
_WEIGHT_OPEN_INTEREST = 1
_WEIGHT_BOOK_TICKER = 2
# /futures/data/* endpoints (topLongShortAccountRatio, topLongShortPositionRatio)
# are the "futures data" family, documented at weight 1 each -- same page as the
# constants above. Two calls per get_top_trader_ratio() (see that method).
_WEIGHT_TOP_TRADER_RATIO = 1


class BinanceFuturesClient:
    """
    Async context manager wrapping one `aiohttp.ClientSession`:

        async with BinanceFuturesClient() as client:
            candles = await client.get_klines("BTCUSDT", "15m", limit=200)

    All configuration (base URL, timeout, retry count, rate-limit budget,
    concurrency, cache TTL) is read from `PlatformConfig` -- never
    hardcoded -- so a strategy profile change or `.env` override applies
    here automatically.
    """

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._config = config or get_config()
        self._base_url = self._config.api.binance_base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=self._config.api.request_timeout_seconds)
        self._session = session
        self._owns_session = session is None

        self._rate_limiter = SlidingWindowRateLimiter(
            max_units_per_window=self._config.scanner.max_api_requests_per_minute,
            log_category="api",
        )
        self._semaphore = asyncio.Semaphore(self._config.scanner.max_concurrent_workers)
        self._cache_lifetime_seconds = self._config.scanner.cache_lifetime_seconds
        self._cache: dict[tuple[str, tuple[tuple[str, Any], ...]], tuple[float, Any]] = {}

        # Built once per instance (not as a class-level decorator) so the
        # retry policy honors *this* config's max_retries -- see module
        # docstring for why this can't just be `@retry_async(...)` on the method.
        self._send_with_retry = retry_async(
            max_attempts=self._config.api.max_retries + 1,
            base_delay_seconds=1.0,
            retryable_exceptions=(BinanceConnectionError, BinanceRateLimitError),
        )(self._send_once)

    async def __aenter__(self) -> "BinanceFuturesClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    @property
    def rate_limiter(self) -> SlidingWindowRateLimiter:
        """Exposed for diagnostics (e.g. a future `bot_health` snapshot reading current usage)."""
        return self._rate_limiter

    def clear_cache(self) -> None:
        """Test/diagnostic helper -- production code should rely on TTL expiry instead."""
        self._cache.clear()

    # ─────────────────────────────────────────────────────────────────
    # PUBLIC ENDPOINTS
    # ─────────────────────────────────────────────────────────────────

    async def get_exchange_info(self) -> list[SymbolInfo]:
        """GET /fapi/v1/exchangeInfo -- all USDT-M perpetual symbols. Cached (changes rarely)."""
        data = await self._get("/fapi/v1/exchangeInfo", weight=_WEIGHT_EXCHANGE_INFO)
        return [
            self._parse_symbol_info(entry)
            for entry in data.get("symbols", [])
            if entry.get("contractType") == "PERPETUAL" and entry.get("quoteAsset") == "USDT"
        ]

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        *,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[Candle]:
        """
        GET /fapi/v1/klines -- OHLCV candles for one symbol/interval
        (SRS Part 5: multi-timeframe analysis uses 5m/15m/30m/1h).

        `start_time`/`end_time` are optional (Module 21 BACKTEST RUNNER:
        historical range queries). Omitted, behavior is unchanged from
        before this parameter existed -- Binance returns the most recent
        `limit` candles. Historical data for a fixed past range never
        changes, so it is cached exactly like any other call here rather
        than needing special-casing.
        """
        if limit <= 100:
            weight = _WEIGHT_KLINES_SMALL
        elif limit <= 500:
            weight = _WEIGHT_KLINES_MEDIUM
        else:
            weight = _WEIGHT_KLINES_LARGE

        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time.timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(end_time.timestamp() * 1000)

        data = await self._get("/fapi/v1/klines", params=params, weight=weight)
        return [self._parse_candle(row) for row in data]

    async def get_ticker_24hr(self, symbol: Optional[str] = None) -> list[Ticker24hr]:
        """GET /fapi/v1/ticker/24hr -- single symbol if given, else the whole market (SRS Part 16 Stage 1)."""
        params = {"symbol": symbol} if symbol else None
        weight = _WEIGHT_TICKER_24HR_SINGLE if symbol else _WEIGHT_TICKER_24HR_ALL
        data = await self._get("/fapi/v1/ticker/24hr", params=params, weight=weight)
        rows = [data] if symbol else data
        return [self._parse_ticker_24hr(row) for row in rows]

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        """GET /fapi/v1/premiumIndex -- current funding rate + mark price (SRS Part 8)."""
        data = await self._get(
            "/fapi/v1/premiumIndex", params={"symbol": symbol}, weight=_WEIGHT_PREMIUM_INDEX
        )
        return self._parse_funding_rate(data) if data else None

    async def get_open_interest(self, symbol: str) -> Optional[OpenInterest]:
        """GET /fapi/v1/openInterest (SRS Part 8/14 open-interest conflict check)."""
        data = await self._get(
            "/fapi/v1/openInterest", params={"symbol": symbol}, weight=_WEIGHT_OPEN_INTEREST
        )
        return self._parse_open_interest(data) if data else None

    async def get_book_ticker(self, symbol: str) -> Optional[BookTicker]:
        """GET /fapi/v1/ticker/bookTicker -- best bid/ask. Never cached: spread must always be fresh."""
        data = await self._get(
            "/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol},
            weight=_WEIGHT_BOOK_TICKER,
            cacheable=False,
        )
        return self._parse_book_ticker(data) if data else None

    async def get_top_trader_ratio(self, symbol: str, period: str = "15m") -> Optional[TopTraderRatio]:
        """
        GET /futures/data/topLongShortAccountRatio + .../topLongShortPositionRatio
        (Module 23 Smart Money Engine) -- see `TopTraderRatio`'s docstring
        for what these two ratios mean and why there is no API for the
        consumer Smart Money page's per-trader feed.

        `period` matches Binance's documented enum ("5m"/"15m"/"30m"/"1h"/
        "2h"/"4h"/"6h"/"12h"/"1d"); both calls use `limit=1` since this
        method always wants the single most recent reading, never a
        history (mirrors `get_funding_rate`/`get_open_interest`'s
        "current snapshot" shape). Fired concurrently, same pattern as
        `engines.market_health`'s paired funding-rate/open-interest fetch.
        Returns None if either call comes back empty (e.g. a brand-new
        symbol with no data yet) rather than half-filling a
        `TopTraderRatio` with a fabricated 0.0 for the missing side.
        """
        params = {"symbol": symbol, "period": period, "limit": 1}
        account_data, position_data = await asyncio.gather(
            self._get("/futures/data/topLongShortAccountRatio", params=params, weight=_WEIGHT_TOP_TRADER_RATIO),
            self._get("/futures/data/topLongShortPositionRatio", params=params, weight=_WEIGHT_TOP_TRADER_RATIO),
        )
        if not account_data or not position_data:
            return None
        return self._parse_top_trader_ratio(symbol, account_data[-1], position_data[-1])

    async def check_health(self) -> bool:
        """
        GET /fapi/v1/ping -- Binance's dedicated near-zero-weight
        connectivity check (SRS Part 18 HEALTH CHECK). Never cached: a
        health check must reflect the current moment.
        """
        try:
            await self._get("/fapi/v1/ping", weight=1, cacheable=False)
            return True
        except BinanceAPIError:
            return False

    # ─────────────────────────────────────────────────────────────────
    # SIGNED / TRADING ENDPOINTS (autonomous trading pivot)
    # ─────────────────────────────────────────────────────────────────

    async def get_account_balance(self) -> list[AccountBalance]:
        """GET /fapi/v2/balance (signed) -- every asset on the account. TradeExecutionEngine filters to USDT."""
        data = await self._signed_get("/fapi/v2/balance", weight=5)
        return [self._parse_account_balance(row) for row in data]

    async def get_position_risk(self, symbol: str) -> Optional[PositionRisk]:
        """GET /fapi/v2/positionRisk (signed) -- one-way mode assumed, so exactly one row per symbol."""
        data = await self._signed_get("/fapi/v2/positionRisk", {"symbol": symbol}, weight=5)
        if not data:
            return None
        return self._parse_position_risk(data[0])

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """POST /fapi/v1/leverage (signed)."""
        await self._signed_write("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        """
        POST /fapi/v1/marginType (signed). Swallows Binance's -4046 "No
        need to change margin type" error -- calling this when the
        symbol is already in the requested margin type is the expected,
        idempotent common case (every `open_position()` call sets it
        unconditionally), not a real failure.
        """
        try:
            await self._signed_write("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except BinanceDataError as exc:
            if exc.context.get("body", {}).get("code") == -4046:
                return
            raise

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        quantity: Optional[float] = None,
        stop_price: Optional[float] = None,
        close_position: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """
        POST /fapi/v1/order (signed). Supports exactly the three order
        types this platform uses: MARKET (entry, requires `quantity`),
        STOP_MARKET and TAKE_PROFIT_MARKET (protective exits, requires
        `stop_price` and typically `close_position=True`). No
        `positionSide` param -- one-way mode is assumed account-wide
        (never hedge mode), per the platform's design decisions.
        """
        params: dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type}
        if quantity is not None:
            params["quantity"] = quantity
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if close_position:
            params["closePosition"] = "true"
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id
        data = await self._signed_write("POST", "/fapi/v1/order", params)
        return self._parse_order_result(data)

    async def cancel_order(self, symbol: str, order_id: int) -> OrderResult:
        """DELETE /fapi/v1/order (signed) -- cancel one order by exchange order ID."""
        data = await self._signed_write("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return self._parse_order_result(data)

    async def cancel_all_open_orders(self, symbol: str) -> None:
        """DELETE /fapi/v1/allOpenOrders (signed) -- cancels every resting order for `symbol` (e.g. leftover SL/TP after a close)."""
        await self._signed_write("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_order(self, symbol: str, order_id: int) -> Optional[OrderResult]:
        """GET /fapi/v1/order (signed) -- current status of one order."""
        try:
            data = await self._signed_get("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        except BinanceDataError as exc:
            if exc.context.get("body", {}).get("code") == -2013:  # "Order does not exist"
                return None
            raise
        return self._parse_order_result(data)

    async def get_open_orders(self, symbol: str) -> list[OrderResult]:
        """GET /fapi/v1/openOrders (signed) -- every currently-resting order for `symbol`."""
        data = await self._signed_get("/fapi/v1/openOrders", {"symbol": symbol}, weight=1)
        return [self._parse_order_result(row) for row in data]

    # ─────────────────────────────────────────────────────────────────
    # REQUEST PLUMBING (rate limit -> concurrency limit -> retry -> cache)
    # ─────────────────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        weight: int = 1,
        cacheable: bool = True,
    ) -> Any:
        cache_key = (path, tuple(sorted((params or {}).items())))
        if cacheable:
            cached_entry = self._cache.get(cache_key)
            if cached_entry is not None:
                cached_at, cached_value = cached_entry
                if time.monotonic() - cached_at < self._cache_lifetime_seconds:
                    return cached_value

        await self._rate_limiter.acquire(weight)
        async with self._semaphore:
            result = await self._send_with_retry("GET", path, params)

        if cacheable:
            self._cache[cache_key] = (time.monotonic(), result)
        return result

    def _require_credentials(self) -> None:
        if not self._config.api.binance_api_key or not self._config.api.binance_api_secret:
            raise BinanceAPIError(
                "Signed endpoint called with no Binance API key/secret configured",
                context={"base_url": self._base_url},
            )

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Binance's documented signing scheme: every param except
        `signature` itself, url-encoded in insertion order (Binance does
        NOT require alphabetical order -- do not "fix" this to sort, see
        module docstring / MASTER PROMPT known pitfalls: a test that
        sorts params to match would pass against a wrong implementation),
        HMAC-SHA256'd with the API secret. `timestamp` is added fresh on
        every call (never cached/reused) since it must be within
        `recvWindow` ms of Binance's server clock.
        """
        signed = dict(params)
        signed["timestamp"] = int(time.time() * 1000)
        signed["recvWindow"] = 5000
        query = urllib.parse.urlencode(signed, doseq=True)
        signature = hmac.new(
            self._config.api.binance_api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        signed["signature"] = signature
        return signed

    def _signed_headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._config.api.binance_api_key}

    async def _signed_get(self, path: str, params: Optional[dict[str, Any]] = None, *, weight: int = 1) -> Any:
        """Signed GET (account balance, position risk, order status): safe to retry, never cached (account state)."""
        self._require_credentials()
        signed_params = self._sign_params(params or {})
        await self._rate_limiter.acquire(weight)
        async with self._semaphore:
            return await self._send_with_retry("GET", path, signed_params, headers=self._signed_headers())

    async def _signed_write(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None, *, weight: int = 1
    ) -> Any:
        """
        Signed POST/DELETE (order placement/cancellation, leverage,
        margin type): deliberately calls `_send_once` directly, NEVER
        `_send_with_retry` -- a blind retry after a network failure on an
        order-placement call risks placing the SAME order twice with no
        way to know whether the first attempt actually reached Binance
        before failing. The only safety net is Binance's own
        `clientOrderId` deduplication (see `place_order()`'s
        `client_order_id` parameter) -- callers that need at-most-once
        semantics must supply a stable, unique `client_order_id`, not
        rely on this method to retry safely.
        """
        self._require_credentials()
        signed_params = self._sign_params(params or {})
        await self._rate_limiter.acquire(weight)
        async with self._semaphore:
            return await self._send_once(method, path, signed_params, headers=self._signed_headers())

    async def _send_once(
        self, method: str, path: str, params: Optional[dict[str, Any]], *, headers: Optional[dict[str, str]] = None
    ) -> Any:
        """
        One HTTP round-trip, with no retry of its own -- retry is applied
        by wrapping this method in `system.retry.retry_async` inside
        `__init__` (see there for why).
        """
        if self._session is None:
            raise RuntimeError(
                "BinanceFuturesClient must be used as an async context manager "
                "(`async with BinanceFuturesClient() as client:`)"
            )

        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(method, url, params=params, headers=headers) as response:
                used_weight_header = response.headers.get("X-MBX-USED-WEIGHT-1M")
                if used_weight_header is not None:
                    self._rate_limiter.record_external_usage(int(used_weight_header))

                if response.status in (429, 418):
                    raise BinanceRateLimitError(
                        f"Binance rate limit hit (HTTP {response.status}) calling {path}",
                        context={
                            "path": path,
                            "status": response.status,
                            "retry_after": response.headers.get("Retry-After"),
                        },
                    )

                body = await response.json(content_type=None)

                if response.status >= 400:
                    raise BinanceDataError(
                        f"Binance returned HTTP {response.status} calling {path}",
                        context={"path": path, "status": response.status, "body": body},
                    )

                return body
        except asyncio.TimeoutError as exc:
            raise BinanceConnectionError(
                f"Timed out calling {path}", cause=exc, context={"path": path}
            ) from exc
        except aiohttp.ClientError as exc:
            raise BinanceConnectionError(
                f"Connection error calling {path}", cause=exc, context={"path": path}
            ) from exc

    # ─────────────────────────────────────────────────────────────────
    # RESPONSE PARSING (raw Binance JSON -> typed core.models-adjacent dataclasses)
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_symbol_info(entry: dict[str, Any]) -> SymbolInfo:
        filters = {f["filterType"]: f for f in entry.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_size_filter = filters.get("LOT_SIZE", {})
        min_notional_filter = filters.get("MIN_NOTIONAL")

        return SymbolInfo(
            symbol=entry["symbol"],
            base_asset=entry["baseAsset"],
            quote_asset=entry["quoteAsset"],
            status=entry["status"],
            price_precision=int(entry.get("pricePrecision", 0)),
            quantity_precision=int(entry.get("quantityPrecision", 0)),
            tick_size=float(price_filter.get("tickSize", 0.0)),
            step_size=float(lot_size_filter.get("stepSize", 0.0)),
            min_notional_usdt=float(min_notional_filter["notional"]) if min_notional_filter else None,
        )

    @staticmethod
    def _parse_candle(row: list[Any]) -> Candle:
        return Candle(
            open_time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=datetime.fromtimestamp(row[6] / 1000, tz=timezone.utc),
            quote_volume=float(row[7]),
            num_trades=int(row[8]),
            taker_buy_base_volume=float(row[9]),
            taker_buy_quote_volume=float(row[10]),
        )

    @staticmethod
    def _parse_ticker_24hr(row: dict[str, Any]) -> Ticker24hr:
        return Ticker24hr(
            symbol=row["symbol"],
            last_price=float(row["lastPrice"]),
            price_change_percent=float(row["priceChangePercent"]),
            quote_volume=float(row["quoteVolume"]),
            high_price=float(row["highPrice"]),
            low_price=float(row["lowPrice"]),
            weighted_avg_price=float(row["weightedAvgPrice"]),
        )

    @staticmethod
    def _parse_funding_rate(row: dict[str, Any]) -> FundingRate:
        return FundingRate(
            symbol=row["symbol"],
            mark_price=float(row["markPrice"]),
            funding_rate=float(row["lastFundingRate"]),
            next_funding_time=datetime.fromtimestamp(row["nextFundingTime"] / 1000, tz=timezone.utc),
        )

    @staticmethod
    def _parse_open_interest(row: dict[str, Any]) -> OpenInterest:
        return OpenInterest(
            symbol=row["symbol"],
            open_interest=float(row["openInterest"]),
            timestamp=datetime.fromtimestamp(row["time"] / 1000, tz=timezone.utc),
        )

    @staticmethod
    def _parse_top_trader_ratio(symbol: str, account_row: dict[str, Any], position_row: dict[str, Any]) -> TopTraderRatio:
        # Binance reuses the same field names ("longShortRatio"/"longAccount"/
        # "shortAccount") on both endpoints' response rows -- only the URL
        # (and therefore which underlying metric is being described) differs.
        return TopTraderRatio(
            symbol=symbol,
            account_ratio=float(account_row["longShortRatio"]),
            position_ratio=float(position_row["longShortRatio"]),
            timestamp=datetime.fromtimestamp(int(account_row["timestamp"]) / 1000, tz=timezone.utc),
        )

    @staticmethod
    def _parse_book_ticker(row: dict[str, Any]) -> BookTicker:
        return BookTicker(
            symbol=row["symbol"],
            bid_price=float(row["bidPrice"]),
            bid_qty=float(row["bidQty"]),
            ask_price=float(row["askPrice"]),
            ask_qty=float(row["askQty"]),
        )

    @staticmethod
    def _parse_account_balance(row: dict[str, Any]) -> AccountBalance:
        return AccountBalance(
            asset=row["asset"],
            balance=float(row["balance"]),
            available_balance=float(row["availableBalance"]),
        )

    @staticmethod
    def _parse_position_risk(row: dict[str, Any]) -> PositionRisk:
        return PositionRisk(
            symbol=row["symbol"],
            position_amount=float(row["positionAmt"]),
            entry_price=float(row["entryPrice"]),
            mark_price=float(row["markPrice"]),
            unrealized_pnl=float(row["unRealizedProfit"]),
            leverage=int(row["leverage"]),
        )

    @staticmethod
    def _parse_order_result(row: dict[str, Any]) -> OrderResult:
        return OrderResult(
            order_id=int(row["orderId"]),
            client_order_id=row.get("clientOrderId", ""),
            symbol=row["symbol"],
            status=row["status"],
            avg_price=float(row.get("avgPrice") or 0.0),
            executed_qty=float(row.get("executedQty") or 0.0),
        )
