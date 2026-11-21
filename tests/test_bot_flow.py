import asyncio

from colde.bot import Bot
from colde.brokers import PaperBroker
from colde.models import Candle, Contract
from colde.store import Store
from tests.helpers import BASE, H1, M15, build, info, long_candles, settings


class FakePublic:
    def __init__(self):
        self.price = 101.4
        self.k = {"1h": long_candles(), "15m": build([103.5] * 8, 60, M15), "1m": []}
        self.calls = 0

    async def contracts(self):
        return {"TEST-USDT": Contract("TEST-USDT", 2, 2, 0.01, 2.0, 20)}

    async def tickers(self):
        return {"TEST-USDT": {"last": self.price, "quote_volume": 9e9, "change_pct": 1.0}}

    async def klines(self, sym, interval, limit):
        return list(self.k[interval])


class FakeTG:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True

    async def alert(self, text):
        self.sent.append("ALERT " + text)


def make(**kw):
    s = settings(**kw)
    store, pub, tg = Store(":memory:"), FakePublic(), FakeTG()
    clock = {"t": BASE + 20_000}
    bot = Bot(s, pub, PaperBroker(store, s), store, tg, now_ms=lambda: clock["t"])
    bot.contracts = asyncio.run(pub.contracts())
    bot.levels = {"TEST-USDT": info()}
    return bot, store, pub, tg, clock


def test_scan_opens_trade_and_sends_short_message():
    bot, store, pub, tg, clock = make()
    opened = asyncio.run(bot.scan())
    assert len(opened) == 1
    t = store.open_trades()[0]
    assert t["side"] == "LONG" and t["sl"] < t["entry"] < t["tp"] and t["qty"] > 0 and t["leverage"] >= 1
    assert "LONG açıldı · TESTUSDT" in tg.sent[0] and "Neden:" in tg.sent[0]
    assert len(tg.sent[0].splitlines()) == 7                      # kısa mesaj
    # aynı parite tekrar açılmaz
    assert asyncio.run(bot.scan()) == []


def test_tp_flow_updates_balance():
    bot, store, pub, tg, clock = make()
    asyncio.run(bot.scan())
    t = store.open_trades()[0]
    clock["t"] += 180_000
    start = (t["opened_ms"] // 60_000 + 1) * 60_000
    pub.price = t["tp"] + 0.1
    pub.k["1m"] = [Candle(start, t["entry"], t["tp"] + 0.2, t["entry"] - 0.05, t["tp"] + 0.1, 1)]
    asyncio.run(bot.manage())
    c = store.get_trade(t["id"])
    assert c["status"] == "closed" and c["exit_reason"] == "tp" and c["pnl"] > 0
    assert "TP oldu" in tg.sent[-1]
    assert abs(float(store.kv_get("paper_balance")) - (12.0 + c["pnl"])) < 1e-6


def test_sl_flow_and_consecutive_sl_stops_day():
    bot, store, pub, tg, clock = make(cooldown_after_sl_h=0.0, max_consecutive_sl=2)
    for _ in range(2):
        asyncio.run(bot.scan())
        t = store.open_trades()[0]
        clock["t"] += 180_000
        start = (t["opened_ms"] // 60_000 + 1) * 60_000
        pub.price = t["sl"] - 0.1
        pub.k["1m"] = [Candle(start, t["entry"], t["entry"] + 0.05, t["sl"] - 0.2, t["sl"] - 0.1, 1)]
        asyncio.run(bot.manage())
        c = store.get_trade(t["id"])
        assert c["exit_reason"] == "sl" and c["pnl"] < 0
        pub.price = 101.4
        clock["t"] += 60_000
    assert "SL oldu" in tg.sent[-1]
    ok, why = bot.can_open_any()
    assert not ok and "SL" in why
    assert asyncio.run(bot.scan()) == []


def test_daily_trade_limit_and_cooldown_and_disabled():
    bot, store, pub, tg, clock = make(max_trades_per_day=1)
    asyncio.run(bot.scan())
    assert bot.can_open_any() == (False, "günlük işlem sınırı")
    bot2, *_ = make(trading_enabled=False)
    assert bot2.can_open_any()[0] is False


def test_protected_exit_message():
    bot, store, pub, tg, clock = make()
    asyncio.run(bot.scan())
    t = store.open_trades()[0]
    full = t["tp"] - t["entry"]
    clock["t"] += 180_000
    start = (t["opened_ms"] // 60_000 + 1) * 60_000
    peak = t["entry"] + 0.8 * full
    pub.price = t["entry"] + 0.5 * full
    pub.k["1m"] = [Candle(start, t["entry"], peak, t["entry"] + 0.45 * full, pub.price, 1)]
    asyncio.run(bot.manage())
    c = store.get_trade(t["id"])
    assert c["status"] == "closed" and c["exit_reason"] in ("trail", "protected") and c["pnl"] > 0
    assert "Korumada çıkış" in tg.sent[-1]


def test_stale_data_skipped():
    bot, store, pub, tg, clock = make()
    clock["t"] += 3 * 3_600_000          # veri 3 saat eski (piyasa kapalı gibi)
    assert asyncio.run(bot.scan()) == []
