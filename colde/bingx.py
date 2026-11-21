"""BingX USDT-M perpetual futures REST istemcisi.

NOT: Bu dosya BingX'in resmi dokümanına göre yazıldı. Geliştirme ortamında BingX'e erişim
olmadığı için canlı uçlar (emir/bakiye/pozisyon) gerçek borsada test EDİLMEDİ. Bu yüzden
önce MODE=paper ile bir hafta çalıştır; vst/live'a ancak sonra geç.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import quote

import httpx

from .models import Candle, Contract

log = logging.getLogger("bingx")

BASE = {"live": "https://open-api.bingx.com", "paper": "https://open-api.bingx.com",
        "vst": "https://open-api-vst.bingx.com"}


class BingXError(Exception):
    def __init__(self, code, msg, path=""):
        super().__init__(f"BingX {code}: {msg} ({path})")
        self.code, self.msg = code, msg


def num(x, default: float = 0.0) -> float:
    try:
        if isinstance(x, str):
            x = x.replace("%", "").strip()
        return float(x)
    except (TypeError, ValueError):
        return default


def fmt(x: float, decimals: int = 8) -> str:
    s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s or "0"


class BingXClient:
    def __init__(self, api_key: str = "", api_secret: str = "", base: str = BASE["live"],
                 min_interval: float = 0.15, timeout: float = 15.0):
        self.key, self.secret, self.base = api_key, api_secret, base
        self.min_interval = min_interval
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def _signed_query(self, params: dict) -> str:
        p = {k: v for k, v in params.items() if v is not None}
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        raw = "&".join(f"{k}={p[k]}" for k in sorted(p))
        sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        enc = "&".join(f"{k}={quote(str(p[k]), safe='')}" for k in sorted(p))
        return f"{enc}&signature={sig}"

    async def request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False):
        params = params or {}
        last_err: Exception = RuntimeError("istek yapılamadı")
        for attempt in range(4):
            await self._throttle()
            try:
                if signed:
                    url = f"{self.base}{path}?{self._signed_query(params)}"
                    headers = {"X-BX-APIKEY": self.key}
                else:
                    q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None)
                    url = f"{self.base}{path}" + (f"?{q}" if q else "")
                    headers = {}
                async with httpx.AsyncClient(timeout=self.timeout) as cli:
                    r = await cli.request(method, url, headers=headers)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}")
                js = r.json()
                code = js.get("code", 0)
                if code not in (0, "0", None):
                    # geçici hız sınırı hatalarını yeniden dene
                    if code in (100410, 100413) and attempt < 3:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    raise BingXError(code, js.get("msg", ""), path)
                return js.get("data")
            except BingXError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("%s %s deneme %d: %s", method, path, attempt + 1, e)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err

    # ---------------- public ----------------
    async def contracts(self) -> dict:
        data = await self.request("GET", "/openApi/swap/v2/quote/contracts")
        out = {}
        for d in data or []:
            sym = d.get("symbol")
            if not sym or not sym.endswith("-USDT"):
                continue
            if int(num(d.get("status"), 1)) != 1:
                continue
            if str(d.get("apiStateOpen", "true")).lower() == "false":
                continue
            out[sym] = Contract(
                symbol=sym,
                price_precision=int(num(d.get("pricePrecision"), 4)),
                qty_precision=int(num(d.get("quantityPrecision"), 0)),
                min_qty=num(d.get("tradeMinQuantity"), num(d.get("size"), 0.0)),
                min_notional=num(d.get("tradeMinUSDT"), 5.0),
                max_leverage=int(num(d.get("maxLongLeverage"), 20)) or 20,
            )
        return out

    async def tickers(self) -> dict:
        data = await self.request("GET", "/openApi/swap/v2/quote/ticker")
        if isinstance(data, dict):
            data = [data]
        out = {}
        for d in data or []:
            sym = d.get("symbol")
            last = num(d.get("lastPrice"))
            if sym and last > 0:
                out[sym] = {"last": last, "quote_volume": num(d.get("quoteVolume")),
                            "change_pct": num(d.get("priceChangePercent"))}
        return out

    async def klines(self, symbol: str, interval: str, limit: int) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            data = await self.request("GET", "/openApi/swap/v3/quote/klines", params)
        except BingXError:
            data = await self.request("GET", "/openApi/swap/v2/quote/klines", params)
        out = {}
        for d in data or []:
            if isinstance(d, dict):
                t = int(num(d.get("time")))
                out[t] = Candle(t, num(d.get("open")), num(d.get("high")), num(d.get("low")),
                                num(d.get("close")), num(d.get("volume")))
        return [out[k] for k in sorted(out)]

    # ---------------- signed (trade) ----------------
    async def balance(self) -> tuple:
        data = await self.request("GET", "/openApi/swap/v2/user/balance", signed=True)
        b = data.get("balance", data) if isinstance(data, dict) else {}
        return num(b.get("balance")) or num(b.get("equity")), num(b.get("availableMargin"), num(b.get("balance")))

    async def dual_side(self) -> bool:
        try:
            d = await self.request("GET", "/openApi/swap/v1/positionSide/dual", signed=True)
            return str(d.get("dualSidePosition", "true")).lower() == "true"
        except Exception as e:  # noqa: BLE001
            log.warning("Pozisyon modu okunamadı, hedge varsayıldı: %s", e)
            return True

    async def positions(self) -> list:
        return await self.request("GET", "/openApi/swap/v2/user/positions", signed=True) or []

    async def set_margin_isolated(self, symbol: str) -> None:
        try:
            await self.request("POST", "/openApi/swap/v2/trade/marginType",
                               {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BingXError as e:
            log.info("marginType %s: %s", symbol, e.msg)

    async def set_leverage(self, symbol: str, side: str, leverage: int) -> int:
        lev = leverage
        while lev >= 1:
            try:
                await self.request("POST", "/openApi/swap/v2/trade/leverage",
                                   {"symbol": symbol, "side": side, "leverage": lev}, signed=True)
                return lev
            except BingXError as e:
                log.warning("leverage %s %sx reddedildi: %s", symbol, lev, e.msg)
                lev = lev // 2
        raise BingXError(-1, "kaldıraç ayarlanamadı", symbol)

    async def order(self, params: dict) -> dict:
        return await self.request("POST", "/openApi/swap/v2/trade/order", params, signed=True) or {}

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        await self.request("DELETE", "/openApi/swap/v2/trade/order",
                           {"symbol": symbol, "orderId": order_id}, signed=True)

    async def cancel_all(self, symbol: str) -> None:
        await self.request("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol}, signed=True)
