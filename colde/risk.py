from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .models import Contract, Signal, symbol_class


def risk_pct(balance: float, s: Settings) -> float:
    """Küçük bakiyede küçük risk; bakiye büyüdükçe yavaş yavaş artar."""
    lo_b, hi_b = s.risk_low_balance, s.risk_high_balance
    if balance <= lo_b:
        return s.risk_low_pct
    if balance >= hi_b:
        return s.risk_high_pct
    f = (balance - lo_b) / (hi_b - lo_b)
    return s.risk_low_pct + f * (s.risk_high_pct - s.risk_low_pct)


def choose_leverage(symbol: str, atr1h_pct: float, sl_pct: float, max_lev: int) -> int:
    cls = symbol_class(symbol)
    if cls == "major":
        cap = 10
    elif cls in ("commodity", "index", "forex"):
        cap = 5
    elif cls == "stock":
        cap = 3
    else:
        cap = 7 if atr1h_pct < 0.5 else 5 if atr1h_pct < 1.0 else 3 if atr1h_pct < 2.0 else 2
    by_sl = int(0.4 / (sl_pct / 100)) if sl_pct > 0 else cap   # likidasyon stopun çok ötesinde kalsın
    return max(1, min(cap, by_sl, max_lev))


@dataclass
class Sizing:
    qty: float
    notional: float
    margin: float
    risk_usd: float


def floor_to(x: float, decimals: int) -> float:
    m = 10 ** decimals
    return math.floor(x * m + 1e-9) / m


def size_position(balance: float, available: float, sig: Signal, leverage: int,
                  c: Contract, s: Settings) -> Optional[Sizing]:
    sl_dist = abs(sig.entry - sig.sl)
    if sl_dist <= 0 or balance <= 0:
        return None
    risk_usd = balance * risk_pct(balance, s) / 100
    qty = risk_usd / sl_dist
    max_margin = min(available * 0.9, balance * s.max_margin_pct / 100)
    if qty * sig.entry / leverage > max_margin:
        qty = max_margin * leverage / sig.entry
    qty = floor_to(qty, c.qty_precision)
    notional = qty * sig.entry
    if qty <= 0 or qty < c.min_qty or notional < c.min_notional:
        return None
    return Sizing(qty=qty, notional=notional, margin=notional / leverage, risk_usd=qty * sl_dist)
