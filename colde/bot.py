from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings
from .levels import compute_levels, near_level
from .messages import close_msg, open_msg
from .models import INTERVAL_MS, Contract, LevelInfo, Signal, closed_only, symbol_class
from .positions import calc_pnl, manage_step
from .reports import build_report
from .risk import choose_leverage, size_position
from .store import Store
from .strategy import enforce_min_sl, find_setup

log = logging.getLogger("bot")


class Bot:
    def __init__(self, s: Settings, public, broker, store: Store, notifier, now_ms=None):
        self.s, self.public, self.broker, self.store, self.tg = s, public, broker, store, notifier
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.tz = ZoneInfo(s.timezone)
        self.contracts: dict = {}
        self.levels: dict = {}
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------ zaman
    def day_start_ms(self, now_ms: Optional[int] = None) -> int:
        now = datetime.fromtimestamp((now_ms or self.now_ms()) / 1000, self.tz)
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # ------------------------------------------------------------ evren + seviyeler
    async def refresh_contracts(self) -> None:
        self.contracts = await self.public.contracts()
        log.info("%d kontrat yüklendi", len(self.contracts))

    def universe(self, tickers: dict) -> list:
        out = []
        for sym in self.contracts:
            if any(sym.upper().startswith(p) for p in self.s.exclude_prefixes):
                continue
            tk = tickers.get(sym)
            if not tk:
                continue
            if symbol_class(sym) in ("crypto", "major") and tk["quote_volume"] < self.s.min_quote_volume:
                continue
            out.append(sym)
        return out

    async def refresh_levels(self) -> None:
        tickers = await self.public.tickers()
        uni = self.universe(tickers)
        now = self.now_ms()
        sem = asyncio.Semaphore(3)
        new: dict = {}

        async def one(sym: str) -> None:
            try:
                async with sem:
                    d1 = await self.public.klines(sym, "1d", 120)
                    h4 = await self.public.klines(sym, "4h", 250)
                    h1 = await self.public.klines(sym, "1h", 300)
                info = compute_levels(closed_only(d1, "1d", now), closed_only(h4, "4h", now),
                                      closed_only(h1, "1h", now), self.s.min_level_strength, now)
                if info and info.levels:
                    new[sym] = info
            except Exception as e:  # noqa: BLE001
                log.debug("seviye hesaplanamadı %s: %s", sym, e)

        await asyncio.gather(*(one(x) for x in uni))
        if new:
            self.levels = new
        log.info("Seviyeler hazır: %d/%d parite", len(new), len(uni))

    # ------------------------------------------------------------ korumalar
    def can_open_any(self) -> tuple:
        s = self.s
        if not s.trading_enabled:
            return False, "işlem kapalı"
        ds = self.day_start_ms()
        if s.max_trades_per_day and self.store.opened_since(ds) >= s.max_trades_per_day:
            return False, "günlük işlem sınırı"
        if len(self.store.open_trades()) >= s.max_open_positions:
            return False, "açık pozisyon sınırı"
        closed = self.store.closed_between(ds, self.now_ms() + 1)
        streak = 0
        for t in reversed(closed):
            if t["exit_reason"] == "sl":
                streak += 1
            else:
                break
        if streak >= s.max_consecutive_sl:
            return False, "üst üste SL, bugün dur"
        if closed:
            start_bal = closed[0]["balance_open"] or 0
            if start_bal and sum(t["pnl"] for t in closed) <= -start_bal * s.daily_loss_limit_pct / 100:
                return False, "günlük zarar sınırı"
        return True, ""

    def can_open(self, symbol: str) -> tuple:
        ok, why = self.can_open_any()
        if not ok:
            return ok, why
        if any(t["symbol"] == symbol for t in self.store.open_trades()):
            return False, "bu parite zaten açık"
        last = self.store.last_closed(symbol)
        if last:
            cd = self.s.cooldown_after_sl_h if last["exit_reason"] == "sl" else self.s.cooldown_after_win_h
            if self.now_ms() - last["closed_ms"] < cd * 3_600_000:
                return False, "bekleme süresi"
        return True, ""

    # ------------------------------------------------------------ tarama
    async def scan(self) -> list:
        ok, why = self.can_open_any()
        if not ok:
            log.info("Tarama atlandı: %s", why)
            return []
        now = self.now_ms()
        tickers = await self.public.tickers()
        watch = [sym for sym, info in self.levels.items()
                 if sym in tickers and near_level(info, tickers[sym]["last"], self.s.watch_atr)]
        log.info("Alarm listesi: %d parite seviyeye yakın", len(watch))
        sem = asyncio.Semaphore(4)

        async def work(sym: str) -> Optional[Signal]:
            try:
                async with sem:
                    c15 = closed_only(await self.public.klines(sym, "15m", 120), "15m", now)
                    c1h = closed_only(await self.public.klines(sym, "1h", 120), "1h", now)
                if not c15 or now - (c15[-1].t + INTERVAL_MS["15m"]) > 3 * INTERVAL_MS["15m"]:
                    return None                      # veri bayat (piyasa kapalı olabilir)
                return find_setup(sym, self.levels[sym], {"15m": c15, "1h": c1h}, self.s,
                                  tickers[sym]["change_pct"])
            except Exception as e:  # noqa: BLE001
                log.debug("tarama hatası %s: %s", sym, e)
                return None

        sigs = [x for x in await asyncio.gather(*(work(x) for x in watch)) if x]
        sigs.sort(key=lambda x: x.score, reverse=True)
        opened = []
        for sig in sigs:
            if len(opened) >= 2:
                break
            if await self.try_open(sig, tickers):
                opened.append(sig)
        return opened

    # ------------------------------------------------------------ açılış
    async def try_open(self, sig: Signal, tickers: dict) -> bool:
        async with self.lock:
            ok, why = self.can_open(sig.symbol)
            if not ok:
                log.info("%s atlandı: %s", sig.symbol, why)
                return False
            contract: Optional[Contract] = self.contracts.get(sig.symbol)
            tk = tickers.get(sig.symbol)
            if not contract or not tk:
                return False
            price = tk["last"]
            long = sig.side == "LONG"
            if abs(price - sig.entry) > self.s.max_entry_drift_atr * sig.atr_tf:
                log.info("%s atlandı: fiyat sinyalden uzaklaştı", sig.symbol)
                return False
            if (long and (sig.sl >= price or sig.tp <= price)) or (not long and (sig.sl <= price or sig.tp >= price)):
                return False
            # Boyutlandırmayı sinyalin mum kapanışına değil, ŞU ANKİ fiyata göre yap
            sig = replace(sig, entry=price, sl=enforce_min_sl(sig.side, price, sig.sl, sig.atr_1h, self.s))
            if (long and sig.sl >= price) or (not long and sig.sl <= price) or sig.sl_pct > self.s.max_sl_pct:
                log.info("%s atlandı: stop mesafesi uygun değil", sig.symbol)
                return False
            if sig.rr < self.s.min_rr or sig.tp_pct < self.s.min_tp_pct * 0.8:
                log.info("%s atlandı: güncel fiyatta risk/ödül bozuldu", sig.symbol)
                return False
            balance, avail = await self.broker.balance()
            lev = choose_leverage(sig.symbol, sig.atr_1h / price * 100, sig.sl_pct, contract.max_leverage)
            sz = size_position(balance, avail, sig, lev, contract, self.s)
            if not sz:
                log.info("%s atlandı: pozisyon boyutu minimumun altında", sig.symbol)
                return False
            try:
                fill = await self.broker.open(sig.symbol, sig.side, sz.qty, lev, sig.sl, sig.tp, price, contract)
            except Exception as e:  # noqa: BLE001
                log.error("Emir açılamadı %s: %s", sig.symbol, e)
                await self.tg.alert(f"{sig.symbol} emri açılamadı: {e}")
                return False
            now = self.now_ms()
            t = {"symbol": sig.symbol, "side": sig.side, "tf": sig.tf, "status": "open", "mode": self.s.mode,
                 "entry": fill.price, "sl": sig.sl, "tp": sig.tp, "init_sl": sig.sl, "qty": sz.qty,
                 "leverage": fill.leverage, "risk_usd": sz.qty * abs(fill.price - sig.sl), "notional": sz.notional,
                 "opened_ms": now, "balance_open": balance, "peak": fill.price, "protected": 0, "trailing": 0,
                 "sl_order_id": fill.sl_order_id, "tp_order_id": fill.tp_order_id, "last_check_ms": None,
                 "level": sig.level, "score": sig.score}
            t["id"] = self.store.insert_trade(t)
            await self.tg.send(open_msg(t, sig.reason))
            log.info("AÇILDI %s %s giriş=%s tp=%s sl=%s skor=%.1f", sig.symbol, sig.side, fill.price, sig.tp, sig.sl, sig.score)
            return True

    # ------------------------------------------------------------ yönetim
    async def manage(self) -> None:
        opens = self.store.open_trades()
        if not opens:
            return
        tickers = await self.public.tickers()
        for t in opens:
            try:
                await self._manage_one(t, tickers)
            except Exception as e:  # noqa: BLE001
                log.error("yönetim hatası %s: %s", t["symbol"], e)

    async def _manage_one(self, t: dict, tickers: dict) -> None:
        async with self.lock:
            t = self.store.get_trade(t["id"])
            if not t or t["status"] != "open":
                return
            tk = tickers.get(t["symbol"])
            if not tk:
                return
            now = self.now_ms()
            last = tk["last"]
            hi = lo = last
            contract = self.contracts.get(t["symbol"])
            if self.s.mode == "paper":
                since = t["last_check_ms"]
                start = (t["opened_ms"] // 60_000 + 1) * 60_000 if since is None else (since // 60_000) * 60_000
                bars = [c for c in await self.public.klines(t["symbol"], "1m", 6) if c.t >= start]
                if bars:
                    hi, lo = max(max(c.h for c in bars), last), min(min(c.l for c in bars), last)
                self.store.update_trade(t["id"], last_check_ms=now)
            else:
                alive = await self.broker.position_open(t)
                if alive is False:
                    await self._finish_external(t, last)
                    return
            upd, ex = manage_step(t, hi, lo, last, now, self.s)
            if "sl" in upd and self.s.mode != "paper":
                new_id = await self.broker.move_stop(t, upd["sl"], contract)
                if new_id:
                    upd["sl_order_id"] = new_id
            if upd:
                self.store.update_trade(t["id"], **upd)
                t.update(upd)
            if ex:
                await self._close(t, ex["reason"], ex["price"], contract, send_to_exchange=True)

    async def _finish_external(self, t: dict, last: float) -> None:
        """Borsa SL/TP emri tetiklendi (pozisyon artık yok). Çıkış fiyatı tahmini."""
        long = t["side"] == "LONG"
        mid = (t["entry"] + t["tp"]) / 2
        if (long and last >= mid) or (not long and last <= mid):
            reason, price = "tp", t["tp"]
        elif (long and last <= t["sl"] * 1.002) or (not long and last >= t["sl"] * 0.998):
            reason = "sl" if not t["protected"] else ("trail" if t["trailing"] else "protected")
            price = t["sl"]
        else:
            reason, price = "external", last
        await self._close(t, reason, price, self.contracts.get(t["symbol"]), send_to_exchange=False)

    async def _close(self, t: dict, reason: str, price: float, contract, send_to_exchange: bool) -> None:
        if send_to_exchange:
            price = await self.broker.close(t, price, contract)
        if self.s.mode == "paper" and reason not in ("tp",):
            slip = self.s.slippage_pct / 100
            price = price * (1 - slip) if t["side"] == "LONG" else price * (1 + slip)
        pnl = calc_pnl(t["side"], t["entry"], price, t["qty"], self.s.fee_pct)
        if self.s.mode == "paper":
            bal = self.broker.add_pnl(pnl)
        else:
            bal, _ = await self.broker.balance()
        self.store.update_trade(t["id"], status="closed", closed_ms=self.now_ms(), exit_price=price,
                                exit_reason=reason, pnl=pnl, balance_close=bal)
        t.update(status="closed", exit_price=price, exit_reason=reason, pnl=pnl, balance_close=bal)
        await self.tg.send(close_msg(t))
        log.info("KAPANDI %s %s sebep=%s pnl=%.4f bakiye=%.4f", t["symbol"], t["side"], reason, pnl, bal)

    # ------------------------------------------------------------ raporlar
    async def report_tick(self) -> None:
        now = datetime.fromtimestamp(self.now_ms() / 1000, self.tz)
        if now.hour != self.s.report_hour:
            return
        today = now.strftime("%Y-%m-%d")
        bal, _ = await self.broker.balance()

        def start_of(d: datetime) -> int:
            return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

        end = int(self.now_ms()) + 1
        if self.store.kv_get(f"rep:d:{today}") is None:
            self.store.kv_set(f"rep:d:{today}", "1")
            tr = self.store.closed_between(start_of(now), end)
            await self.tg.send(build_report(f"Günlük rapor · {now.strftime('%d.%m.%Y')}", tr, bal))
        if now.weekday() == 6 and self.store.kv_get(f"rep:w:{today}") is None:
            self.store.kv_set(f"rep:w:{today}", "1")
            tr = self.store.closed_between(start_of(now - timedelta(days=6)), end)
            await self.tg.send(build_report("Haftalık rapor", tr, bal, list_trades=False))
        if (now + timedelta(days=1)).month != now.month and self.store.kv_get(f"rep:m:{today}") is None:
            self.store.kv_set(f"rep:m:{today}", "1")
            tr = self.store.closed_between(start_of(now.replace(day=1)), end)
            await self.tg.send(build_report(f"Aylık rapor · {now.strftime('%m.%Y')}", tr, bal, list_trades=False))
