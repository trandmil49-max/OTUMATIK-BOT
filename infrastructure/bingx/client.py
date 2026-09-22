"""
infrastructure/bingx/client.py

Async REST client for BingX USDT-M Perpetual Futures TRADING endpoints
ONLY (account balance, position risk, leverage, margin type, order
placement/cancellation).

WHY A SECOND EXCHANGE CLIENT AT ALL: at the platform owner's explicit
request, real order EXECUTION is routed to BingX instead of Binance,
because BingX does not mandate an IP allowlist for Futures trading
permission on an API key (Binance does -- see
infrastructure.binance.client's module docstring), so it works from a
host with no static outbound IP (e.g. Railway's free/Hobby tiers)
without needing a paid static-IP add-on. Every market-data feed this
platform reads for ANALYSIS (candles, funding rate, open interest,
order book, Smart Money top-trader ratio) deliberately stays on
`infrastructure.binance.client.BinanceFuturesClient` -- Binance remains
the deepest, most information-rich venue and generally leads price
discovery, so using its data to decide direction/confidence while
executing the resulting order wherever is most practical is a
sound design, not a shortcut. See `engines.trade_execution
.TradeExecutionEngine`'s module docstring for the full reasoning and
the safeguard this split requires (re-validating price/levels against
BingX's OWN current price immediately before placing an order, since
Binance and BingX prices track closely but are never guaranteed
identical, especially on lower-liquidity symbols).

INTERFACE PARITY WITH BinanceFuturesClient: this client deliberately
exposes the SAME method names and signatures as the trading-related
subset of `BinanceFuturesClient` (get_account_balance,
get_exchange_info, get_position_risk, set_leverage, set_margin_type,
place_order, cancel_order, cancel_all_open_orders, get_order,
get_open_orders) so `TradeExecutionEngine` can be handed either client
interchangeably without caring which exchange it is actually talking
to.

SYMBOL FORMAT: every OTHER part of this platform (Signal.symbol,
Trade.symbol, every Binance market-data call) uses Binance's
unhyphenated style ("BTCUSDT"). BingX requires a hyphen ("BTC-USDT").
This client converts internally (_to_bingx_symbol/_from_bingx_symbol)
so nothing outside this one file needs to know BingX symbols look
different -- every public method here still takes/returns
Binance-style symbols.

POSITION MODE: BingX supports both one-way and hedge position modes,
set per-account (not per-order). This client ASSUMES the account is in
one-way mode (BingX's default for a new account) and always sends
positionSide="BOTH" accordingly. It deliberately does NOT read or
change the account's position mode itself -- flipping that account-wide
setting from unattended code is exactly the kind of silent,
hard-to-notice behavior change this platform avoids elsewhere too (see
TradeExecutionEngine's fixed ISOLATED margin choice). `verify_one_way_mode()`
checks this assumption explicitly against the account and raises if it
does not hold, rather than silently sending orders built for the wrong
mode.

SIGNING: HMAC-SHA256, hex digest, computed over parameters sorted
ALPHABETICALLY by key and joined into a query string -- BingX's
documented convention (unlike Binance, where insertion order works
fine; sorting here is not optional).

VALIDATION STATUS: every endpoint path, parameter name, and response
field below was assembled from BingX's public API documentation and
several independent community client implementations, cross-checked
against each other for consistency -- but none of it has been executed
against BingX's live API from inside this platform. Per the platform
owner's own instruction, this client MUST be exercised end-to-end
against BingX's VST (Virtual Simulated Trading) demo environment
(`bingx_base_url = "https://open-api-vst.bingx.com"`) -- balance fetch,
leverage, margin type, order placement, order cancellation, all of it --
before `TRADING_ENABLED` is ever set against the production host with
real money behind it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional

import aiohttp

from config.loader import get_config
from config.schema import PlatformConfig
from infrastructure.binance.models import AccountBalance, OrderResult, PositionRisk, SymbolInfo
from system.exceptions import BinanceAPIError
from system.logging_setup import get_logger

_logger = get_logger("trading")

# BingX's one-way-mode sentinel: sent for EVERY order/leverage/margin
# call here (see module docstring's POSITION MODE section) rather than
# "LONG"/"SHORT", which BingX reserves for hedge mode.
_ONE_WAY_POSITION_SIDE = "BOTH"


def _to_bingx_symbol(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC-USDT'. Idempotent: a symbol that already has a hyphen passes through unchanged."""
    if "-" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT"
    raise ValueError(f"Cannot convert a non-USDT-quoted symbol to BingX format: {symbol!r}")


def _from_bingx_symbol(symbol: str) -> str:
    """'BTC-USDT' -> 'BTCUSDT' -- so a Trade/Signal's `.symbol` never has to know BingX's format exists."""
    return symbol.replace("-", "")


class BingXAPIError(BinanceAPIError):
    """Raised for any BingX request failure. Subclasses BinanceAPIError so callers that catch that base class (e.g. TradeExecutionEngine's error handling) work unchanged regardless of which exchange raised it."""


class BingXFuturesClient:
    """
    Async context manager wrapping one `aiohttp.ClientSession`, scoped to
    BingX USDT-M Perpetual Futures TRADING endpoints only:

        async with BingXFuturesClient() as client:
            balances = await client.get_account_balance()

    All configuration (base URL, timeout, API key/secret) is read from
    `PlatformConfig.api` -- never hardcoded.
    """

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._config = config or get_config()
        self._base_url = self._config.api.bingx_base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=self._config.api.request_timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "BingXFuturesClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def _require_credentials(self) -> None:
        if not self._config.api.bingx_api_key or not self._config.api.bingx_api_secret:
            raise BingXAPIError(
                "BingX endpoint called with no API key/secret configured",
                context={"base_url": self._base_url},
            )

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """BingX's documented scheme: params sorted alphabetically by key, HMAC-SHA256 hex-digested with the secret. Sorting is NOT optional here (unlike Binance's client)."""
        signed = dict(params)
        signed["timestamp"] = int(time.time() * 1000)
        sorted_items = sorted(signed.items())
        query = "&".join(f"{key}={value}" for key, value in sorted_items)
        signature = hmac.new(
            self._config.api.bingx_api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        signed["signature"] = signature
        return signed

    def _headers(self) -> dict[str, str]:
        return {"X-BX-APIKEY": self._config.api.bingx_api_key}

    async def _request(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None, *, signed: bool = False
    ) -> Any:
        if self._session is None:
            raise RuntimeError(
                "BingXFuturesClient must be used as an async context manager (`async with BingXFuturesClient() as client:`)"
            )
        headers: Optional[dict[str, str]] = None
        request_params = params or {}
        if signed:
            self._require_credentials()
            request_params = self._sign_params(request_params)
            headers = self._headers()

        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(method, url, params=request_params, headers=headers) as response:
                try:
                    body = await response.json()
                except Exception as exc:
                    raise BingXAPIError(
                        f"BingX returned a non-JSON response (status {response.status})",
                        context={"path": path, "status": response.status},
                    ) from exc

                if response.status >= 400:
                    raise BingXAPIError(
                        f"BingX request failed: {method} {path} (status {response.status})",
                        context={"path": path, "status": response.status, "body": body},
                    )
                # BingX's own success/failure signal, independent of HTTP status:
                # code 0 means success; anything else is a business-logic error
                # (bad symbol, insufficient balance, precision violation, etc.).
                if isinstance(body, dict) and body.get("code") not in (0, None):
                    raise BingXAPIError(
                        f"BingX rejected the request: {method} {path} (code {body.get('code')})",
                        context={"path": path, "body": body},
                    )
                return body.get("data") if isinstance(body, dict) else body
        except aiohttp.ClientError as exc:
            raise BingXAPIError(f"Network error calling BingX: {method} {path}", context={"path": path}) from exc

    # ─────────────────────────────────────────────────────────────────
    # ACCOUNT / MARKET-STRUCTURE
    # ─────────────────────────────────────────────────────────────────

    async def get_account_balance(self) -> list[AccountBalance]:
        """GET /openApi/swap/v2/user/balance (signed) -- every asset on the account. TradeExecutionEngine filters to USDT."""
        data = await self._request("GET", "/openApi/swap/v2/user/balance", signed=True)
        rows = data if isinstance(data, list) else data.get("balance") if isinstance(data, dict) else None
        rows = rows if rows is not None else ([data] if isinstance(data, dict) else [])
        return [self._parse_account_balance(row) for row in rows]

    async def get_exchange_info(self) -> list[SymbolInfo]:
        """GET /openApi/swap/v2/quote/contracts (public, unsigned) -- every USDT-M perpetual contract's precision/minimums, needed to size orders correctly on BingX's own rules."""
        data = await self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
        rows = data if isinstance(data, list) else []
        return [self._parse_symbol_info(row) for row in rows]

    async def get_position_risk(self, symbol: str) -> Optional[PositionRisk]:
        """GET /openApi/swap/v2/user/positions (signed) -- one-way mode assumed, so exactly one row per symbol."""
        data = await self._request(
            "GET", "/openApi/swap/v2/user/positions", {"symbol": _to_bingx_symbol(symbol)}, signed=True
        )
        rows = data if isinstance(data, list) else []
        if not rows:
            return None
        return self._parse_position_risk(rows[0])

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """GET /openApi/swap/v1/ticker/price (public, unsigned) -- BingX's OWN latest traded price for `symbol`, used by TradeExecutionEngine to sanity-check levels computed from Binance's data before placing a real order (see this module's docstring)."""
        data = await self._request(
            "GET", "/openApi/swap/v1/ticker/price", {"symbol": _to_bingx_symbol(symbol)}, signed=False
        )
        if not data or "price" not in data:
            return None
        return float(data["price"])

    async def check_health(self) -> bool:
        """
        Lightweight connectivity check for the startup/status notification.
        BingX does not document a dedicated near-zero-weight ping the way
        Binance's `/fapi/v1/ping` is documented, so this reuses the
        public current-price call for BTC-USDT (virtually guaranteed to
        be a listed, liquid pair) as a stand-in.
        """
        try:
            price = await self.get_current_price("BTCUSDT")
            return price is not None and price > 0
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────
    # TRADING
    # ─────────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """POST /openApi/swap/v2/trade/leverage (signed)."""
        await self._request(
            "POST", "/openApi/swap/v2/trade/leverage",
            {"symbol": _to_bingx_symbol(symbol), "side": _ONE_WAY_POSITION_SIDE, "leverage": leverage},
            signed=True,
        )

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        """
        POST /openApi/swap/v2/trade/marginType (signed). Swallows a
        "margin type unchanged" style business error the same way
        `BinanceFuturesClient.set_margin_type()` swallows Binance's
        -4046 -- calling this when the symbol is already in the
        requested margin type is the expected, idempotent common case
        (every `open_position()` call sets it unconditionally), not a
        real failure. BingX's exact code for this is unconfirmed against
        the live API (see module docstring's VALIDATION STATUS) so this
        matches on the message text as a fallback in addition to a code
        check, and re-raises anything that doesn't look like "already
        set".
        """
        try:
            await self._request(
                "POST", "/openApi/swap/v2/trade/marginType",
                {"symbol": _to_bingx_symbol(symbol), "marginType": margin_type},
                signed=True,
            )
        except BingXAPIError as exc:
            body = exc.context.get("body") if isinstance(exc.context, dict) else None
            message = str(body.get("msg", "")).lower() if isinstance(body, dict) else ""
            if "no need" in message or "already" in message:
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
        POST /openApi/swap/v2/trade/order (signed). Same three order
        types as the Binance client (MARKET entry, STOP_MARKET/
        TAKE_PROFIT_MARKET protective exits), with `positionSide` always
        `_ONE_WAY_POSITION_SIDE` -- see module docstring's POSITION MODE
        section.
        """
        params: dict[str, Any] = {
            "symbol": _to_bingx_symbol(symbol), "side": side, "positionSide": _ONE_WAY_POSITION_SIDE,
            "type": order_type,
        }
        if quantity is not None:
            params["quantity"] = quantity
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if close_position:
            params["closePosition"] = "true"
        if client_order_id is not None:
            params["clientOrderId"] = client_order_id
        data = await self._request("POST", "/openApi/swap/v2/trade/order", params, signed=True)
        row = data.get("order", data) if isinstance(data, dict) else data
        return self._parse_order_result(row)

    async def cancel_order(self, symbol: str, order_id: int) -> OrderResult:
        """DELETE /openApi/swap/v2/trade/order (signed) -- cancel one order by exchange order ID."""
        data = await self._request(
            "DELETE", "/openApi/swap/v2/trade/order",
            {"symbol": _to_bingx_symbol(symbol), "orderId": order_id}, signed=True,
        )
        row = data.get("order", data) if isinstance(data, dict) else data
        return self._parse_order_result(row)

    async def cancel_all_open_orders(self, symbol: str) -> None:
        """DELETE /openApi/swap/v2/trade/allOpenOrders (signed) -- cancels every resting order for `symbol` (e.g. leftover SL/TP after a close)."""
        await self._request(
            "DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": _to_bingx_symbol(symbol)}, signed=True,
        )

    async def get_order(self, symbol: str, order_id: int) -> Optional[OrderResult]:
        """GET /openApi/swap/v2/trade/order (signed) -- current status of one order."""
        try:
            data = await self._request(
                "GET", "/openApi/swap/v2/trade/order",
                {"symbol": _to_bingx_symbol(symbol), "orderId": order_id}, signed=True,
            )
        except BingXAPIError as exc:
            body = exc.context.get("body") if isinstance(exc.context, dict) else None
            message = str(body.get("msg", "")).lower() if isinstance(body, dict) else ""
            if "not exist" in message or "not found" in message:
                return None
            raise
        row = data.get("order", data) if isinstance(data, dict) else data
        return self._parse_order_result(row) if row else None

    async def get_open_orders(self, symbol: str) -> list[OrderResult]:
        """GET /openApi/swap/v2/trade/openOrders (signed) -- every currently-resting order for `symbol`."""
        data = await self._request(
            "GET", "/openApi/swap/v2/trade/openOrders", {"symbol": _to_bingx_symbol(symbol)}, signed=True,
        )
        rows = data.get("orders", data) if isinstance(data, dict) else data
        rows = rows if isinstance(rows, list) else []
        return [self._parse_order_result(row) for row in rows]

    async def verify_one_way_mode(self) -> bool:
        """
        GET /openApi/swap/v1/positionSide/dual (signed) -- BingX's
        "query position mode" endpoint. Returns True if the account is
        confirmed in one-way mode (what every other method here
        assumes), False if it is in hedge mode or the check itself
        fails -- callers should treat False as "do not trade until this
        is resolved", not as "assume one-way anyway".
        """
        try:
            data = await self._request("GET", "/openApi/swap/v1/positionSide/dual", signed=True)
        except BingXAPIError:
            _logger.warning("Could not verify BingX position mode -- treating as unconfirmed", exc_info=True)
            return False
        is_dual = bool(data.get("dualSidePosition")) if isinstance(data, dict) else None
        if is_dual is None:
            _logger.warning("Unexpected response shape from BingX position-mode check: %r", data)
            return False
        return not is_dual

    # ─────────────────────────────────────────────────────────────────
    # PARSING
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_account_balance(row: dict[str, Any]) -> AccountBalance:
        return AccountBalance(
            asset=row.get("asset", ""),
            balance=float(row.get("balance", 0.0)),
            available_balance=float(row.get("availableMargin", row.get("availableBalance", 0.0))),
        )

    @staticmethod
    def _parse_symbol_info(row: dict[str, Any]) -> SymbolInfo:
        quantity_precision = int(row.get("quantityPrecision", 0))
        return SymbolInfo(
            symbol=_from_bingx_symbol(row.get("symbol", "")),
            base_asset=row.get("asset", ""),
            quote_asset=row.get("currency", "USDT"),
            status="TRADING" if str(row.get("apiStateOpen", "true")).lower() == "true" else "BREAK",
            price_precision=int(row.get("pricePrecision", 0)),
            quantity_precision=quantity_precision,
            tick_size=10 ** -int(row.get("pricePrecision", 0)),
            step_size=10 ** -quantity_precision,
            min_notional_usdt=float(row["tradeMinUSDT"]) if row.get("tradeMinUSDT") is not None else None,
        )

    @staticmethod
    def _parse_position_risk(row: dict[str, Any]) -> PositionRisk:
        return PositionRisk(
            symbol=_from_bingx_symbol(row.get("symbol", "")),
            position_amount=float(row.get("positionAmt", 0.0)),
            entry_price=float(row.get("avgPrice", row.get("entryPrice", 0.0))),
            mark_price=float(row.get("markPrice", 0.0)),
            unrealized_pnl=float(row.get("unrealizedProfit", 0.0)),
            leverage=int(row.get("leverage", 1)),
        )

    @staticmethod
    def _parse_order_result(row: dict[str, Any]) -> OrderResult:
        return OrderResult(
            order_id=int(row.get("orderId", 0)),
            client_order_id=row.get("clientOrderId", ""),
            symbol=_from_bingx_symbol(row.get("symbol", "")),
            status=row.get("status", ""),
            avg_price=float(row.get("avgPrice") or 0.0),
            executed_qty=float(row.get("executedQty") or 0.0),
        )
