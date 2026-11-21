"""Destek / direnç seviyeleri: 1g, 4s ve 1s grafiklerdeki dönüş noktalarından (pivot) çıkarılır."""
from __future__ import annotations

from typing import Optional

from .indicators import atr, trend_4h
from .models import Level, LevelInfo

TF_WEIGHT = {"1d": 3.0, "4h": 2.0, "1h": 1.0}
TF_K = {"1d": 2, "4h": 3, "1h": 4}


def pivots(candles: list, k: int) -> list:
    """Sol tarafta kesin, sağ tarafta eşit-veya-düşük olan tepe/dip fiyatları."""
    out = []
    n = len(candles)
    for i in range(k, n - k):
        hi, lo = candles[i].h, candles[i].l
        if all(hi > candles[j].h for j in range(i - k, i)) and all(hi >= candles[j].h for j in range(i + 1, i + k + 1)):
            out.append(hi)
        if all(lo < candles[j].l for j in range(i - k, i)) and all(lo <= candles[j].l for j in range(i + 1, i + k + 1)):
            out.append(lo)
    return out


def cluster(points: list, tol: float) -> list:
    """points: [(price, weight)] -> [Level]; tol içindeki pivotlar tek seviyede toplanır."""
    pts = sorted(points)
    groups = []
    for p, w in pts:
        if groups:
            mean = sum(x for x, _ in groups[-1]) / len(groups[-1])
            if p - mean <= tol:
                groups[-1].append((p, w))
                continue
        groups.append([(p, w)])
    out = []
    for g in groups:
        wsum = sum(w for _, w in g)
        price = sum(p * w for p, w in g) / wsum
        out.append(Level(price=price, strength=wsum, touches=len(g)))
    return out


def compute_levels(c1d: list, c4h: list, c1h: list, min_strength: float, now_ms: int) -> Optional[LevelInfo]:
    if len(c1h) < 100 or len(c4h) < 60:
        return None
    atr1h = atr(c1h)
    ref = c1h[-1].c
    if atr1h <= 0 or ref <= 0:
        return None
    tol = max(0.35 * atr1h, ref * 0.001)
    pts = []
    for tf, c in (("1d", c1d), ("4h", c4h), ("1h", c1h)):
        for p in pivots(c, TF_K[tf]):
            pts.append((p, TF_WEIGHT[tf]))
    levels = [lv for lv in cluster(pts, tol) if lv.strength >= min_strength]
    return LevelInfo(levels=levels, tol=tol, atr1h=atr1h, trend=trend_4h(c4h), updated_ms=now_ms)


def near_level(info: LevelInfo, price: float, watch_atr: float) -> bool:
    """'Alarm' kontrolü: fiyat bir seviyeye yeterince yakın mı?"""
    if not info or not info.levels:
        return False
    reach = max(watch_atr * info.atr1h, price * 0.004)
    return any(abs(price - lv.price) <= reach for lv in info.levels)
