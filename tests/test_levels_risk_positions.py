import math

from colde.levels import cluster, compute_levels, near_level, pivots
from colde.models import Candle, Contract, Signal
from colde.positions import calc_pnl, manage_step
from colde.risk import choose_leverage, risk_pct, size_position
from tests.helpers import BASE, H1, info, settings

S = settings()


def wave(n, interval, lo=100.0, hi=110.0, period=24):
    out = []
    for i in range(n):
        mid = (lo + hi) / 2 + (hi - lo) / 2 * math.sin(2 * math.pi * i / period)
        out.append(Candle(BASE - (n - i) * interval, mid, mid + 0.3, mid - 0.3, mid, 100.0))
    return out


def test_pivots_and_clusters_find_range_edges():
    c1h = wave(300, H1)
    assert len(pivots(c1h, 4)) > 10
    lv = cluster([(100.0, 1), (100.1, 2), (110.0, 3)], tol=0.3)
    assert len(lv) == 2 and lv[0].strength == 3 and abs(lv[0].price - 100.0667) < 1e-3


def test_compute_levels_range():
    c1h, c4h, c1d = wave(300, H1), wave(250, 4 * H1, period=40), wave(120, 24 * H1, period=30)
    li = compute_levels(c1d, c4h, c1h, 3.0, BASE)
    assert li is not None
    prices = [l.price for l in li.levels]
    assert any(abs(p - 100) < 1.0 for p in prices) and any(abs(p - 110) < 1.0 for p in prices)
    assert near_level(li, 100.6, 1.5) and not near_level(li, 105.0, 1.5)


def test_risk_scaling_and_sizing():
    assert risk_pct(12, S) == 1.0 and risk_pct(100, S) == 2.5 and abs(risk_pct(62.5, S) - 1.75) < 1e-9
    sig = Signal("T-USDT", "LONG", "1h", 100, 99, 102, 99.5, 6, 10, 1, 1, 0, "x")
    c = Contract("T-USDT", qty_precision=2, min_qty=0.01, min_notional=2)
    z = size_position(12, 12, sig, 5, c, S)
    assert z and z.qty == 0.12 and abs(z.risk_usd - 0.12) < 1e-9 and abs(z.margin - 12 * 0.12 * 100 / 12 / 5 * 1) < 1.0
    assert size_position(12, 12, sig, 5, Contract("T-USDT", qty_precision=2, min_notional=20), S) is None
    z2 = size_position(1000, 3, sig, 5, c, S)            # marjin yetmiyorsa küçült
    assert z2 and z2.margin <= 3 * 0.9 + 1e-9


def test_leverage_by_class_and_volatility():
    assert choose_leverage("BTC-USDT", 0.3, 0.5, 125) == 10
    assert choose_leverage("MEME-USDT", 3.0, 1.0, 50) == 2
    assert choose_leverage("MEME-USDT", 0.4, 1.0, 50) == 7
    assert choose_leverage("NCCOGOLD2USD-USDT", 0.2, 0.5, 50) == 5
    assert choose_leverage("ETH-USDT", 0.3, 20.0, 50) == 2      # geniş stop -> düşük kaldıraç
    assert choose_leverage("BTC-USDT", 0.3, 0.5, 3) == 3        # borsa üst sınırı


def tr(side="LONG", **kw):
    t = dict(side=side, entry=100.0, sl=99.0 if side == "LONG" else 101.0, tp=102.0 if side == "LONG" else 98.0,
             peak=100.0, protected=0, trailing=0, opened_ms=BASE)
    t.update(kw)
    return t


def test_tp_and_sl_and_same_bar_prefers_sl():
    assert manage_step(tr(), 102.1, 100.0, 102.0, BASE + 1000, S)[1] == {"reason": "tp", "price": 102.0}
    assert manage_step(tr(), 100.5, 98.9, 99.0, BASE + 1000, S)[1] == {"reason": "sl", "price": 99.0}
    assert manage_step(tr(), 102.5, 98.5, 100.0, BASE + 1000, S)[1]["reason"] == "sl"
    assert manage_step(tr("SHORT"), 100.5, 97.9, 98.0, BASE + 1000, S)[1] == {"reason": "tp", "price": 98.0}
    assert manage_step(tr("SHORT"), 101.1, 99.5, 101.0, BASE + 1000, S)[1]["reason"] == "sl"


def test_protection_then_trailing_exit():
    upd, ex = manage_step(tr(), 101.0, 100.5, 100.8, BASE + 1000, S)     # yolun %50'si
    assert ex is None and upd["protected"] == 1 and abs(upd["sl"] - 100.12) < 1e-9
    t = tr(**{k: v for k, v in upd.items()})
    upd2, ex2 = manage_step(t, 101.6, 101.3, 101.5, BASE + 2000, S)       # %80: trail devrede
    assert ex2 is None and upd2["trailing"] == 1 and abs(upd2["sl"] - (101.6 - 0.35 * 1.6)) < 1e-9
    t.update(upd2)
    upd3, ex3 = manage_step(t, 101.4, 100.9, 101.0, BASE + 3000, S)       # geri dönüş -> korumada çıkış
    assert ex3["reason"] == "trail" and ex3["price"] == t["sl"] and ex3["price"] > 100.0


def test_same_bar_peak_and_reversal_exits_in_profit():
    upd, ex = manage_step(tr(), 101.6, 100.9, 100.95, BASE + 1000, S)
    assert ex and ex["reason"] == "trail" and ex["price"] > 100.0


def test_short_protection_and_time_stop():
    upd, ex = manage_step(tr("SHORT"), 99.5, 99.0, 99.2, BASE + 1000, S)
    assert ex is None and upd["protected"] == 1 and abs(upd["sl"] - 99.88) < 1e-9
    _, ex = manage_step(tr(), 100.4, 99.8, 100.1, BASE + 25 * 3_600_000, S)
    assert ex == {"reason": "time", "price": 100.1}


def test_pnl_includes_fees():
    p = calc_pnl("LONG", 100.0, 102.0, 1.0, 0.05)
    assert abs(p - (2.0 - (100 + 102) * 0.0005)) < 1e-9
    assert calc_pnl("SHORT", 100.0, 98.0, 1.0, 0.05) > 0


def test_min_stop_distance_and_margin_cap():
    from colde.strategy import enforce_min_sl
    # çok dar stop (0,27%) geniş tutulur: max(0,5*ATR, %0,35)
    sl = enforce_min_sl("LONG", 3.766, 3.756, atr1h=0.015, s=S)
    assert abs((3.766 - sl) - 0.0075) < 1e-9 or (3.766 - sl) >= 3.766 * 0.0035 - 1e-9
    assert sl < 3.756
    assert enforce_min_sl("LONG", 100.0, 95.0, atr1h=1.0, s=S) == 95.0           # zaten geniş: dokunma
    assert enforce_min_sl("SHORT", 100.0, 100.1, atr1h=1.0, s=S) > 100.1
    # tek işlemin marjini bakiyenin %35'ini geçmez
    sig = Signal("T-USDT", "LONG", "1h", 100, 99.9, 102, 99.5, 6, 10, 1, 1, 0, "x")
    z = size_position(12, 12, sig, 5, Contract("T-USDT", qty_precision=2, min_notional=2), S)
    assert z and z.margin <= 12 * 0.35 + 1e-9
