from colde.config import Settings
from colde.models import Candle, Level, LevelInfo

BASE = 1_800_000_000_000          # 1 saatin tam katı
H1, M15 = 3_600_000, 900_000


def settings(**kw) -> Settings:
    s = Settings(timezone="Europe/Istanbul", paper_start_balance=12.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def build(closes, n_total, interval_ms, touch_low=None, touch_idx=None, vol=100.0):
    """Önce 'n_total - len(closes)' kadar sakin mum (103-104 arası), sonra verilen kapanışlar."""
    out = []
    quiet = n_total - len(closes)
    price = 103.5
    for i in range(quiet):
        o = price
        c = 103.5 + (0.3 if i % 2 else -0.3)
        out.append((o, max(o, c) + 0.4, min(o, c) - 0.4, c))
        price = c
    for c in closes:
        o = price
        out.append((o, max(o, c) + 0.4, min(o, c) - 0.4, c))
        price = c
    res = []
    for i, (o, h, l, c) in enumerate(out):
        t = BASE - (n_total - i) * interval_ms
        res.append(Candle(t, o, h, l, c, vol))
    if touch_low is not None:
        i = n_total + touch_idx if touch_idx < 0 else touch_idx
        x = res[i]
        res[i] = Candle(x.t, x.o, x.h, touch_low, x.c, x.v)
    return res


# Destekten (100) dönüş: dokunuş, sonra 2+ kapanış üstte, son mum yeşil
LONG_CLOSES = [102.0, 101.0, 100.6, 100.8, 100.9, 101.0, 101.1, 101.4]


def long_candles(n=60, interval_ms=H1):
    return build(LONG_CLOSES, n, interval_ms, touch_low=100.1, touch_idx=-6)


def mirror(candles, axis=210.0):
    return [Candle(c.t, axis - c.o, axis - c.l, axis - c.h, axis - c.c, c.v) for c in candles]


def info(trend=1, levels=None, tol=0.35, atr1h=1.0):
    lv = levels if levels is not None else [Level(100.0, 6.0, 5), Level(110.0, 6.0, 5)]
    return LevelInfo(levels=lv, tol=tol, atr1h=atr1h, trend=trend, updated_ms=BASE)
