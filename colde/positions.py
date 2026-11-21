"""Açık pozisyon yönetimi: saf fonksiyonlar (test edilebilir)."""
from __future__ import annotations

from typing import Optional

from .config import Settings


def calc_pnl(side: str, entry: float, exit_price: float, qty: float, fee_pct: float) -> float:
    gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
    fees = (entry + exit_price) * qty * fee_pct / 100
    return gross - fees


def manage_step(t: dict, hi: float, lo: float, last: float, now_ms: int, s: Settings) -> tuple:
    """Döner: (güncellemeler:dict, çıkış:dict|None). Çıkış = {'reason','price'}.

    Sıra: önce mevcut SL/TP kontrolü (aynı mumda ikisi de varsa SL önce sayılır = temkinli),
    sonra zirve güncelleme, koruma (stop girişe), geri verme (trail) ve süre sınırı.
    """
    long = t["side"] == "LONG"
    entry, sl, tp = t["entry"], t["sl"], t["tp"]
    upd: dict = {}

    if (lo <= sl) if long else (hi >= sl):
        reason = "sl" if not t["protected"] else ("trail" if t["trailing"] else "protected")
        return upd, {"reason": reason, "price": sl}
    if (hi >= tp) if long else (lo <= tp):
        return upd, {"reason": "tp", "price": tp}

    peak = t["peak"] or entry
    peak = max(peak, hi) if long else min(peak, lo)
    upd["peak"] = peak
    full = abs(tp - entry)
    fav = (peak - entry) if long else (entry - peak)
    prog = max(0.0, fav) / full if full > 0 else 0.0

    buf = s.be_buffer_pct / 100
    be = entry * (1 + buf) if long else entry * (1 - buf)
    new_sl, protected, trailing = sl, t["protected"], t["trailing"]
    if prog >= s.protect_arm:
        new_sl = max(new_sl, be) if long else min(new_sl, be)
        protected = 1
    if prog >= s.trail_arm:
        trail = peak - s.giveback * (peak - entry) if long else peak + s.giveback * (entry - peak)
        new_sl = max(new_sl, trail) if long else min(new_sl, trail)
        trailing = 1

    if new_sl != sl:
        upd["sl"] = new_sl
    if protected != t["protected"]:
        upd["protected"] = protected
    if trailing != t["trailing"]:
        upd["trailing"] = trailing

    # Yeni stop zaten fiyatın ters tarafında kaldıysa (aynı mumda zirve + geri dönüş) hemen çık
    if new_sl != sl and ((new_sl >= last) if long else (new_sl <= last)):
        return upd, {"reason": "trail" if trailing else "protected", "price": last}

    if now_ms - t["opened_ms"] >= s.max_hold_h * 3_600_000:
        return upd, {"reason": "time", "price": last}
    return upd, None
