from __future__ import annotations


def ema(values: list, n: int) -> list:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def true_ranges(c: list) -> list:
    out = []
    for i, x in enumerate(c):
        if i == 0:
            out.append(x.h - x.l)
        else:
            pc = c[i - 1].c
            out.append(max(x.h - x.l, abs(x.h - pc), abs(x.l - pc)))
    return out


def atr(c: list, n: int = 14) -> float:
    if len(c) < 2:
        return 0.0
    tr = true_ranges(c)[-n:]
    return sum(tr) / len(tr)


def trend_4h(c4h: list) -> int:
    closes = [x.c for x in c4h]
    if len(closes) < 55:
        return 0
    e20, e50, last = ema(closes, 20)[-1], ema(closes, 50)[-1], closes[-1]
    if e20 > e50 and last > e50:
        return 1
    if e20 < e50 and last < e50:
        return -1
    return 0
