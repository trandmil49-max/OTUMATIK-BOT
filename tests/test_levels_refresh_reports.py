import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from colde.models import Contract
from tests.helpers import BASE, H1
from tests.test_bot_flow import FakeTG, FakePublic, make
from tests.test_levels_risk_positions import wave


class WavePublic(FakePublic):
    async def contracts(self):
        return {"A-USDT": Contract("A-USDT"), "LOW-USDT": Contract("LOW-USDT"), "NCCOGOLD2USD-USDT": Contract("NCCOGOLD2USD-USDT")}

    async def tickers(self):
        return {"A-USDT": {"last": 100.6, "quote_volume": 9e6, "change_pct": 0.5},
                "LOW-USDT": {"last": 100.6, "quote_volume": 1e3, "change_pct": 0.5},      # hacim düşük -> elenir
                "NCCOGOLD2USD-USDT": {"last": 100.6, "quote_volume": 0, "change_pct": 0.1}}  # emtia: hacim filtresi yok

    async def klines(self, sym, interval, limit):
        n = {"1d": 120, "4h": 250, "1h": 300}[interval]
        step = {"1d": 24 * H1, "4h": 4 * H1, "1h": H1}[interval]
        return wave(n, step, period={"1d": 30, "4h": 40, "1h": 24}[interval])


def test_refresh_levels_filters_universe():
    bot, *_ = make()
    bot.public = WavePublic()
    bot.contracts = asyncio.run(bot.public.contracts())
    asyncio.run(bot.refresh_levels())
    assert set(bot.levels) == {"A-USDT", "NCCOGOLD2USD-USDT"}
    assert any(abs(l.price - 100) < 1.0 for l in bot.levels["A-USDT"].levels)


def test_reports_sent_once_per_day_and_on_right_days():
    bot, store, pub, tg, clock = make()
    tz = ZoneInfo("Europe/Istanbul")

    def at(y, m, d, h, mi):
        clock["t"] = int(datetime(y, m, d, h, mi, tzinfo=tz).timestamp() * 1000)

    at(2026, 11, 18, 22, 59)                       # Çarşamba 22:59 -> rapor yok
    asyncio.run(bot.report_tick()); assert tg.sent == []
    at(2026, 11, 18, 23, 5)                        # Çarşamba 23:05 -> sadece günlük
    asyncio.run(bot.report_tick()); asyncio.run(bot.report_tick())
    assert len(tg.sent) == 1 and "Günlük rapor" in tg.sent[0]
    at(2026, 11, 22, 23, 5)                        # Pazar -> günlük + haftalık
    asyncio.run(bot.report_tick())
    assert any("Haftalık rapor" in m for m in tg.sent)
    at(2026, 11, 30, 23, 5)                        # ayın son günü -> günlük + aylık
    asyncio.run(bot.report_tick())
    assert any("Aylık rapor" in m for m in tg.sent)
