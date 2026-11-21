from __future__ import annotations

from dataclasses import dataclass

INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
MAJORS = {"BTC", "ETH"}


@dataclass(frozen=True)
class Candle:
    t: int          # açılış zamanı (ms)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class Level:
    price: float
    strength: float
    touches: int


@dataclass
class LevelInfo:
    levels: list
    tol: float
    atr1h: float
    trend: int          # +1 yükseliş, -1 düşüş, 0 karışık (4s)
    updated_ms: int


@dataclass
class Contract:
    symbol: str
    price_precision: int = 4
    qty_precision: int = 0
    min_qty: float = 0.0
    min_notional: float = 5.0
    max_leverage: int = 20


@dataclass
class Signal:
    symbol: str
    side: str       # LONG | SHORT
    tf: str         # 15m | 1h
    entry: float
    sl: float
    tp: float
    level: float
    level_strength: float
    score: float
    atr_tf: float
    atr_1h: float
    candle_t: int
    reason: str

    @property
    def sl_pct(self) -> float:
        return abs(self.entry - self.sl) / self.entry * 100

    @property
    def tp_pct(self) -> float:
        return abs(self.tp - self.entry) / self.entry * 100

    @property
    def rr(self) -> float:
        return abs(self.tp - self.entry) / abs(self.entry - self.sl)


def symbol_class(symbol: str) -> str:
    base = symbol.split("-")[0].upper()
    if base.startswith("NCCO"):
        return "commodity"
    if base.startswith("NCSK"):
        return "stock"
    if base.startswith("NCFX"):
        return "forex"
    if base.startswith("NCSI"):
        return "index"
    if base in MAJORS:
        return "major"
    return "crypto"


def closed_only(candles: list, interval: str, now_ms: int) -> list:
    """Henüz kapanmamış son mumu at."""
    ms = INTERVAL_MS[interval]
    return [c for c in candles if c.t + ms <= now_ms]
