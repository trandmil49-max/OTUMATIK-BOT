"""Setup arama: sadece 'temiz tutunan' seviyeler, 2 mum kapanış onayı, yakın TP, mantıklı SL."""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .indicators import atr
from .models import Level, LevelInfo, Signal

TF_LABEL = {"15m": "15dk", "1h": "1s"}


def enforce_min_sl(side: str, entry: float, sl: float, atr1h: float, s: Settings) -> float:
    """Stop, girişe en az max(0,5 ATR, %0,35) uzakta olsun; çok dar stop gürültüyle patlar."""
    d = max(s.min_sl_atr * atr1h, entry * s.min_sl_pct / 100)
    return min(sl, entry - d) if side == "LONG" else max(sl, entry + d)


def plan_tp(side: str, entry: float, levels: list, atr: float, s: Settings) -> Optional[float]:
    """TP: bir sonraki karşı seviyenin hemen önü. Yer yoksa (seviye çok yakınsa) işlem yok."""
    if side == "LONG":
        opp = [lv.price for lv in levels if lv.price > entry]
        nearest = min(opp) if opp else None
    else:
        opp = [lv.price for lv in levels if lv.price < entry]
        nearest = max(opp) if opp else None
    cap = s.tp_atr_cap * atr
    if nearest is None:
        dist = 1.0 * atr                         # bilinen seviye yok -> 1 ATR
    else:
        dist = min(s.tp_fraction * abs(nearest - entry), cap)
    if dist <= 0 or dist / entry * 100 < s.min_tp_pct:
        return None
    return entry + dist if side == "LONG" else entry - dist


def _setup(symbol: str, side: str, tf: str, c: list, L: Level, info: LevelInfo,
           atr_tf: float, s: Settings) -> Optional[Signal]:
    n, lb = s.confirm_closes, s.setup_lookback
    recent, conf, last = c[-lb:], c[-n:], c[-1]
    lvl, tol = L.price, info.tol
    long = side == "LONG"

    if info.trend == (-1 if long else 1):        # trende ters işlem yok
        return None
    if last.h - last.l > 2.5 * atr_tf:           # haber/spike mumu sonrası girme
        return None

    if long:
        if not all(x.c > lvl for x in conf) or not last.c > last.o:
            return None
        if any(x.c < lvl - tol for x in recent):  # seviye kapanışla kırılmış -> temiz değil
            return None
        if not any(lvl - max(atr_tf, 2 * tol) <= x.l <= lvl + tol for x in recent):
            return None                           # seviyeye dokunuş yok
        if last.c - lvl > 1.2 * atr_tf + tol:     # kovalamak yok
            return None
        sl = min(lvl - 0.3 * atr_tf, min(x.l for x in recent) - 0.1 * atr_tf)
        extra = len(c) > n and c[-n - 1].c > lvl
        if sl >= last.c:
            return None
    else:
        if not all(x.c < lvl for x in conf) or not last.c < last.o:
            return None
        if any(x.c > lvl + tol for x in recent):
            return None
        if not any(lvl - tol <= x.h <= lvl + max(atr_tf, 2 * tol) for x in recent):
            return None
        if lvl - last.c > 1.2 * atr_tf + tol:
            return None
        sl = max(lvl + 0.3 * atr_tf, max(x.h for x in recent) + 0.1 * atr_tf)
        extra = len(c) > n and c[-n - 1].c < lvl
        if sl <= last.c:
            return None

    entry = last.c
    sl = enforce_min_sl(side, entry, sl, info.atr1h, s)
    tp = plan_tp(side, entry, info.levels, atr_tf, s)
    if tp is None:
        return None
    sig_sl_pct = abs(entry - sl) / entry * 100
    rr = abs(tp - entry) / abs(entry - sl)
    if sig_sl_pct > s.max_sl_pct or rr < s.min_rr:
        return None

    vols = [x.v for x in c[-21:-1]]
    vol_ok = bool(vols) and last.v >= sum(vols) / len(vols)
    aligned = info.trend == (1 if long else -1)
    score = (min(L.strength, 8.0) + (2.0 if aligned else 0.0) + (1.0 if tf == "1h" else 0.0)
             + (1.0 if vol_ok else 0.0) + (1.0 if rr >= 0.8 else 0.0) + (0.5 if extra else 0.0))
    if score < s.min_score:
        return None

    what = "destek tuttu" if long else "direnç tuttu"
    return Signal(symbol=symbol, side=side, tf=tf, entry=entry, sl=sl, tp=tp, level=lvl,
                  level_strength=L.strength, score=score, atr_tf=atr_tf, atr_1h=info.atr1h,
                  candle_t=last.t, reason=f"{TF_LABEL[tf]} {what}, {n} mum onayı")


def find_setup(symbol: str, info: LevelInfo, candles: dict, s: Settings, change24: float = 0.0) -> Optional[Signal]:
    """candles: {'15m': [...], '1h': [...]} yalnızca KAPANMIŞ mumlar. En yüksek skorlu setup'ı döndürür."""
    if abs(change24) >= s.max_24h_move_pct:
        return None
    best = None
    for tf in ("1h", "15m"):
        c = candles.get(tf)
        if not c or len(c) < 30:
            continue
        a = atr(c)
        if a <= 0:
            continue
        for L in info.levels:
            for side in ("LONG", "SHORT"):
                sig = _setup(symbol, side, tf, c, L, info, a, s)
                if sig and (best is None or sig.score > best.score):
                    best = sig
    return best
