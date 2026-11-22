from colde.models import Level
from colde.strategy import find_setup, plan_tp
from tests.helpers import H1, M15, build, info, long_candles, mirror, settings

S = settings()


def cands(c1h):
    return {"1h": c1h, "15m": build([103.5] * 8, 60, M15)}


def test_long_setup_found():
    sig = find_setup("TEST-USDT", info(), cands(long_candles()), S)
    assert sig is not None, "destekten dönüşte LONG bekleniyordu"
    assert sig.side == "LONG" and sig.tf == "1h"
    assert sig.sl < 100.0 < sig.entry < sig.tp            # SL desteğin altında, TP girişin üstünde
    assert sig.tp < 110.0                                  # TP direncin ÖNÜNDE
    assert sig.tp_pct >= S.min_tp_pct and sig.sl_pct <= S.max_sl_pct and sig.rr >= S.min_rr


def test_short_setup_found_mirrored():
    sig = find_setup("TEST-USDT", info(trend=-1), cands(mirror(long_candles())), S)
    assert sig is not None and sig.side == "SHORT"
    assert sig.sl > 110.0 > sig.entry > sig.tp > 100.0


def test_countertrend_rejected():
    assert find_setup("TEST-USDT", info(trend=-1), cands(long_candles()), S) is None


def test_close_below_support_rejected():
    c = long_candles()
    x = c[-5]
    c[-5] = type(x)(x.t, x.o, x.h, 98.8, 99.0, x.v)       # seviyenin altında kapanış = temiz değil
    assert find_setup("TEST-USDT", info(), cands(c), S) is None


def test_no_room_to_tp_rejected():
    lv = [Level(100.0, 6.0, 5), Level(101.6, 6.0, 5)]      # direnç hemen üstte
    assert find_setup("TEST-USDT", info(levels=lv), cands(long_candles()), S) is None


def test_low_score_rejected():
    assert find_setup("TEST-USDT", info(), cands(long_candles()), settings(min_score=20)) is None


def test_extreme_24h_move_rejected():
    assert find_setup("TEST-USDT", info(), cands(long_candles()), S, change24=22.0) is None


def test_chasing_rejected():
    c = long_candles()
    x = c[-1]
    c[-1] = type(x)(x.t, 101.4, 105.0, 101.3, 104.8, x.v)   # fiyat seviyeden çok uzaklaşmış
    assert find_setup("TEST-USDT", info(), cands(c), S) is None


def test_plan_tp_is_just_before_resistance():
    lv = [Level(100.5, 6, 5)]
    tp = plan_tp("LONG", 100.0, lv, atr=5.0, s=settings(tp_atr_cap=10))
    assert abs(tp - (100.0 + 0.85 * 0.5)) < 1e-9 and tp < 100.5
    assert plan_tp("LONG", 100.0, lv, atr=5.0, s=settings(tp_atr_cap=10, min_tp_pct=0.5)) is None


def test_tp_distance_follows_entry_timeframe_atr():
    far = [Level(110.0, 6, 5)]
    tp15 = plan_tp("LONG", 100.0, far, atr=0.30, s=S)          # 15dk giriş: küçük ATR -> yakın TP
    tp1h = plan_tp("LONG", 100.0, far, atr=1.00, s=S)          # 1s giriş: daha uzak
    assert abs(tp15 - 100.45) < 1e-9 and abs(tp1h - 101.5) < 1e-9 and tp15 < tp1h
    assert plan_tp("LONG", 100.0, far, atr=0.20, s=S) is None   # hedef %0,4'ün altında -> işlem yok
