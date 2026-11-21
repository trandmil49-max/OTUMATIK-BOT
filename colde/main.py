from __future__ import annotations

import asyncio
import logging
import sys
import time

from .bingx import BASE, BingXClient
from .bot import Bot
from .brokers import LiveBroker, PaperBroker
from .config import Settings
from .store import Store
from .telegram import Telegram

log = logging.getLogger("main")


async def guarded(name: str, coro_fn, tg: Telegram, fail_limit: int = 5) -> None:
    fails = 0
    while True:
        try:
            await coro_fn()
            fails = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            fails += 1
            log.exception("%s hatası (%d): %s", name, fails, e)
            if fails >= fail_limit:
                await tg.alert(f"{name} üst üste {fails} kez hata verdi: {e}")
            await asyncio.sleep(min(60, 5 * fails))


async def run() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings.from_env()
    store = Store(s.db_path)
    tg = Telegram(s.telegram_token, s.telegram_chat_id)
    public = BingXClient(base=BASE["live"])
    broker = PaperBroker(store, s) if s.mode == "paper" else LiveBroker(
        BingXClient(s.bingx_api_key, s.bingx_api_secret, base=BASE[s.mode]), s)
    bot = Bot(s, public, broker, store, tg)

    await bot.refresh_contracts()
    await bot.refresh_levels()
    if s.send_startup_message:
        bal, _ = await broker.balance()
        await tg.send(f"▶️ COLDE-BOT başladı · mod: {s.mode.upper()} · bakiye: ${bal:,.2f}")

    async def scan_loop():
        step = 900
        now = time.time()
        wait = step - (now % step) + s.scan_delay_s
        await asyncio.sleep(wait)
        await bot.scan()

    async def manage_loop():
        await asyncio.sleep(s.manage_interval_s)
        await bot.manage()

    async def levels_loop():
        await asyncio.sleep(s.levels_refresh_min * 60)
        await bot.refresh_levels()
        await bot.refresh_contracts()

    async def report_loop():
        await asyncio.sleep(30)
        await bot.report_tick()

    await asyncio.gather(
        guarded("tarama", scan_loop, tg), guarded("yönetim", manage_loop, tg),
        guarded("seviyeler", levels_loop, tg), guarded("rapor", report_loop, tg))


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
