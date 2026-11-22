"""İki 'broker': PaperBroker (sahte para, gerçek fiyat) ve LiveBroker (BingX; vst ve live)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .bingx import BingXClient, BingXError, fmt, num
from .config import Settings
from .models import Contract
from .store import Store

log = logging.getLogger("broker")


@dataclass
class Fill:
    price: float
    sl_order_id: str = ""
    tp_order_id: str = ""
    leverage: int = 1


class PaperBroker:
    """Gerçek piyasa fiyatıyla sahte işlem. Komisyon ve kayma (slippage) hesaba katılır."""

    def __init__(self, store: Store, s: Settings):
        self.store, self.s = store, s
        if store.kv_get("paper_balance") is None:
            store.kv_set("paper_balance", str(s.paper_start_balance))

    async def balance(self) -> tuple:
        b = float(self.store.kv_get("paper_balance"))
        used = sum(t["notional"] / max(t["leverage"], 1) for t in self.store.open_trades())
        return b, max(b - used, 0.0)

    def add_pnl(self, pnl: float) -> float:
        b = float(self.store.kv_get("paper_balance")) + pnl
        self.store.kv_set("paper_balance", f"{b:.8f}")
        return b

    async def open(self, symbol: str, side: str, qty: float, leverage: int, sl: float, tp: float,
                   price: float, contract: Contract) -> Fill:
        slip = self.s.slippage_pct / 100
        fill = price * (1 + slip) if side == "LONG" else price * (1 - slip)
        return Fill(price=fill, leverage=leverage)

    async def move_stop(self, trade: dict, new_sl: float, contract: Optional[Contract]) -> None:
        return None

    async def close(self, trade: dict, price: float, contract: Optional[Contract]) -> float:
        return price

    async def position_open(self, trade: dict) -> Optional[bool]:
        return None


class LiveBroker:
    """BingX emirleri (vst = demo, live = gerçek). Giriş MARKET; SL ve TP ayrı kapanış emirleri."""

    def __init__(self, client: BingXClient, s: Settings, store: Optional[Store] = None):
        self.c, self.s, self.store = client, s, store
        self.dual: Optional[bool] = None

    async def _mode(self) -> bool:
        if self.dual is None:
            self.dual = await self.c.dual_side()
        return self.dual

    async def balance(self) -> tuple:
        bal, avail = await self.c.balance()
        if self.s.balance_override > 0:
            # Demo hesapta büyük bakiye var; 12$'lık hesap gibi boyutlandır ve kâr/zararı üstüne ekle.
            realized = self.store.pnl_sum(self.s.mode) if self.store else 0.0
            virt = self.s.balance_override + realized
            used = sum(t["notional"] / max(t["leverage"], 1) for t in self.store.open_trades()) if self.store else 0.0
            return virt, max(virt - used, 0.0)
        return bal, avail

    @staticmethod
    def _ps(side: str, dual: bool) -> str:
        return side if dual else "BOTH"

    async def _close_order(self, symbol: str, side: str, qty: float, dual: bool, extra: dict) -> dict:
        p = {"symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
             "positionSide": self._ps(side, dual), "quantity": fmt(qty), **extra}
        if not dual:
            p["reduceOnly"] = "true"
        return await self.c.order(p)

    @staticmethod
    def _oid(resp: dict) -> str:
        o = resp.get("order", resp) if isinstance(resp, dict) else {}
        return str(o.get("orderId", "")) if o else ""

    async def open(self, symbol: str, side: str, qty: float, leverage: int, sl: float, tp: float,
                   price: float, contract: Contract) -> Fill:
        dual = await self._mode()
        await self.c.set_margin_isolated(symbol)
        lev = leverage
        if dual:
            lev = await self.c.set_leverage(symbol, side, leverage)
        else:
            lev = await self.c.set_leverage(symbol, "BOTH", leverage)
        resp = await self.c.order({"symbol": symbol, "side": "BUY" if side == "LONG" else "SELL",
                                   "positionSide": self._ps(side, dual), "type": "MARKET", "quantity": fmt(qty)})
        o = resp.get("order", resp) if isinstance(resp, dict) else {}
        fill_price = num(o.get("avgPrice")) or price
        pp = contract.price_precision
        sl_id = tp_id = ""
        try:
            r = await self._close_order(symbol, side, qty, dual,
                                        {"type": "STOP_MARKET", "stopPrice": fmt(round(sl, pp), pp), "workingType": "MARK_PRICE"})
            sl_id = self._oid(r)
        except Exception as e:  # noqa: BLE001
            # SL'siz pozisyon bırakma: hemen kapat
            log.error("SL emri konamadı (%s), pozisyon kapatılıyor: %s", symbol, e)
            try:
                await self._close_order(symbol, side, qty, dual, {"type": "MARKET"})
            finally:
                raise
        try:
            r = await self._close_order(symbol, side, qty, dual,
                                        {"type": "TAKE_PROFIT_MARKET", "stopPrice": fmt(round(tp, pp), pp), "workingType": "MARK_PRICE"})
            tp_id = self._oid(r)
        except Exception as e:  # noqa: BLE001
            log.warning("TP emri konamadı (%s), bot yazılım TP'si kullanacak: %s", symbol, e)
        return Fill(price=fill_price, sl_order_id=sl_id, tp_order_id=tp_id, leverage=lev)

    async def move_stop(self, trade: dict, new_sl: float, contract: Optional[Contract]) -> None:
        dual = await self._mode()
        pp = contract.price_precision if contract else 6
        old = trade.get("sl_order_id")
        new_id = None
        try:
            r = await self._close_order(trade["symbol"], trade["side"], trade["qty"], dual,
                                        {"type": "STOP_MARKET", "stopPrice": fmt(round(new_sl, pp), pp), "workingType": "MARK_PRICE"})
            new_id = self._oid(r)
        except Exception as e:  # noqa: BLE001
            log.warning("Yeni SL konamadı (%s): %s — eski SL yerinde, yazılım SL'si devrede", trade["symbol"], e)
            return None
        if old:
            try:
                await self.c.cancel_order(trade["symbol"], old)
            except Exception as e:  # noqa: BLE001
                log.warning("Eski SL iptal edilemedi: %s", e)
        return new_id

    async def close(self, trade: dict, price: float, contract: Optional[Contract]) -> float:
        dual = await self._mode()
        try:
            await self.c.cancel_all(trade["symbol"])
        except Exception as e:  # noqa: BLE001
            log.warning("Emirler iptal edilemedi: %s", e)
        await self._close_order(trade["symbol"], trade["side"], trade["qty"], dual, {"type": "MARKET"})
        return price

    async def position_open(self, trade: dict) -> Optional[bool]:
        try:
            for p in await self.c.positions():
                if p.get("symbol") == trade["symbol"] and abs(num(p.get("positionAmt"))) > 0:
                    if p.get("positionSide") in (trade["side"], "BOTH", None):
                        return True
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("Pozisyon sorgulanamadı: %s", e)
            return None
