import asyncio

from colde.bingx import BingXClient, BingXError
from colde.brokers import LiveBroker
from colde.messages import close_msg, money, open_msg, price
from colde.models import Contract
from colde.reports import build_report
from tests.helpers import settings


def test_number_formats():
    assert price(97250.0) == "97.250" and price(0.00123) == "0,00123" and price(2.5) == "2,5"
    assert money(0.18, True) == "+$0,18" and money(-0.12, True) == "−$0,12" and money(12.4) == "$12,40"


def test_message_shapes():
    t = dict(symbol="BTC-USDT", side="LONG", entry=97250.0, tp=98400.0, sl=96700.0, leverage=5, risk_usd=0.12,
             balance_open=12.4, exit_reason="tp", pnl=0.18, balance_close=12.58)
    m = open_msg(t, "1s destek tuttu, 2 mum onayı")
    assert "Giriş: 97.250" in m and "Hedef (TP): 98.400 (+1,2%)" in m and "Stop (SL): 96.700 (−0,6%)" in m
    assert "5x · Risk: $0,12" in m and "Bakiye: $12,40" in m and len(m.splitlines()) == 7
    t["exit_price"] = 98400.0
    assert close_msg(t).startswith("✅ TP oldu · BTCUSDT · LONG")
    assert "Giriş 97.250 → Çıkış 98.400" in close_msg(t) and "Kâr: +$0,18" in close_msg(t)
    assert close_msg({**t, "exit_reason": "sl", "pnl": -0.12}).startswith("🛑 SL oldu")
    assert close_msg({**t, "exit_reason": "trail", "pnl": 0.05}).startswith("🔒 Kâr korundu")
    tiny = close_msg({**t, "exit_reason": "protected", "pnl": -0.003})
    assert tiny.startswith("⚖️ Başa baş çıkış") and "−$0,003" in tiny and "−$0 " not in tiny
    assert "[VST]" in open_msg({**t, "mode": "vst"}, "x") and "[PAPER]" not in open_msg(t, "x")


def test_report():
    tr = [dict(symbol="BTC-USDT", side="LONG", pnl=0.18, exit_reason="tp", balance_open=12.0),
          dict(symbol="ETH-USDT", side="SHORT", pnl=-0.12, exit_reason="sl", balance_open=12.18)]
    r = build_report("Günlük rapor", tr, 12.06)
    assert "İşlem: 2" in r and "50%" in r and "BTCUSDT long +$0,18" in r
    assert "İşlem yok" in build_report("Günlük rapor", [], 12.0)


def test_client_parsing_with_mocked_requests():
    c = BingXClient()

    async def fake(method, path, params=None, signed=False):
        if "klines" in path:
            return [{"open": "2", "high": "3", "low": "1", "close": "2.5", "volume": "10", "time": 2000},
                    {"open": "1", "high": "2", "low": "1", "close": "1.5", "volume": "10", "time": 1000},
                    {"open": "1", "high": "2", "low": "1", "close": "1.5", "volume": "10", "time": 1000}]
        if "ticker" in path:
            return [{"symbol": "BTC-USDT", "lastPrice": "100", "quoteVolume": "5e6", "priceChangePercent": "1.5%"},
                    {"symbol": "BAD-USDT", "lastPrice": "0"}]
        return [{"symbol": "BTC-USDT", "status": 1, "pricePrecision": 1, "quantityPrecision": 4,
                 "tradeMinQuantity": "0.0001", "tradeMinUSDT": "2"},
                {"symbol": "OFF-USDT", "status": 0}, {"symbol": "X-USD"}]

    c.request = fake
    k = asyncio.run(c.klines("BTC-USDT", "1h", 3))
    assert [x.t for x in k] == [1000, 2000] and k[1].c == 2.5
    tk = asyncio.run(c.tickers())
    assert list(tk) == ["BTC-USDT"] and tk["BTC-USDT"]["change_pct"] == 1.5
    ct = asyncio.run(c.contracts())
    assert list(ct) == ["BTC-USDT"] and ct["BTC-USDT"].qty_precision == 4


def test_signature_is_deterministic_hmac():
    c = BingXClient("k", "secret")
    q = c._signed_query({"symbol": "BTC-USDT", "side": "BUY"})
    assert q.startswith("recvWindow=5000&side=BUY&symbol=BTC-USDT&timestamp=") and "&signature=" in q
    assert len(q.split("signature=")[1]) == 64


class FakeClient:
    def __init__(self, fail_sl=False, dual=True):
        self.calls, self.fail_sl, self._dual = [], fail_sl, dual

    async def dual_side(self): return self._dual
    async def set_margin_isolated(self, s): self.calls.append(("margin", s))
    async def set_leverage(self, s, side, lev): self.calls.append(("lev", side, lev)); return lev
    async def cancel_order(self, s, oid): self.calls.append(("cancel", oid))
    async def cancel_all(self, s): self.calls.append(("cancel_all", s))
    async def balance(self): return 12.0, 12.0

    async def order(self, p):
        self.calls.append(("order", p))
        if self.fail_sl and p.get("type") == "STOP_MARKET":
            raise BingXError(1, "boom")
        return {"order": {"orderId": f"id{len(self.calls)}", "avgPrice": "100.1"}}


def test_live_broker_open_places_market_sl_tp_hedge():
    fc = FakeClient()
    b = LiveBroker(fc, settings(mode="vst"))
    f = asyncio.run(b.open("BTC-USDT", "LONG", 0.12, 5, 99.0, 102.0, 100.0, Contract("BTC-USDT", 2, 2)))
    orders = [c[1] for c in fc.calls if c[0] == "order"]
    assert orders[0]["type"] == "MARKET" and orders[0]["side"] == "BUY" and orders[0]["positionSide"] == "LONG"
    assert orders[1]["type"] == "STOP_MARKET" and orders[1]["side"] == "SELL" and orders[1]["stopPrice"] == "99"
    assert orders[2]["type"] == "TAKE_PROFIT_MARKET" and orders[2]["stopPrice"] == "102"
    assert f.price == 100.1 and f.sl_order_id and f.tp_order_id


def test_live_broker_one_way_uses_reduce_only_and_short_sides():
    fc = FakeClient(dual=False)
    b = LiveBroker(fc, settings(mode="vst"))
    asyncio.run(b.open("BTC-USDT", "SHORT", 0.12, 5, 101.0, 98.0, 100.0, Contract("BTC-USDT", 2, 2)))
    orders = [c[1] for c in fc.calls if c[0] == "order"]
    assert orders[0]["side"] == "SELL" and orders[0]["positionSide"] == "BOTH"
    assert orders[1]["side"] == "BUY" and orders[1]["reduceOnly"] == "true"


def test_live_broker_closes_position_if_sl_cannot_be_placed():
    fc = FakeClient(fail_sl=True)
    b = LiveBroker(fc, settings(mode="vst"))
    try:
        asyncio.run(b.open("BTC-USDT", "LONG", 0.12, 5, 99.0, 102.0, 100.0, Contract("BTC-USDT", 2, 2)))
        assert False, "hata fırlatılmalıydı"
    except BingXError:
        pass
    orders = [c[1] for c in fc.calls if c[0] == "order"]
    assert orders[-1]["type"] == "MARKET" and orders[-1]["side"] == "SELL"     # SL'siz pozisyon bırakılmadı


def test_vst_virtual_balance_tracks_realized_pnl():
    from colde.store import Store
    st = Store(":memory:")
    fc = FakeClient()
    b = LiveBroker(fc, settings(mode="vst", balance_override=12.0), st)
    assert asyncio.run(b.balance()) == (12.0, 12.0)
    st.insert_trade(dict(symbol="A-USDT", side="LONG", status="closed", mode="vst", pnl=0.5, notional=10, leverage=2))
    st.insert_trade(dict(symbol="B-USDT", side="LONG", status="open", mode="vst", pnl=None, notional=10, leverage=2))
    bal, avail = asyncio.run(b.balance())
    assert abs(bal - 12.5) < 1e-9 and abs(avail - 7.5) < 1e-9


def test_symbol_display_names():
    from colde.messages import sym
    assert sym("BTC-USDT") == "BTCUSDT" and sym("XUSD-USDT") == "XUSDUSDT"
    assert sym("NCCO7241NATGAS2USD-USDT") == "NATGAS2USD" and sym("NCCOGOLD2USD-USDT") == "GOLD2USD"
