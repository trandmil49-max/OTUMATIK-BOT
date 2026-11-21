"""Telegram mesajları: kısa ve net. Raporlar reports.py'de."""
from __future__ import annotations

import math


def tr_num(x: float, decimals: int) -> str:
    s = f"{abs(x):,.{decimals}f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s


def price(p: float) -> str:
    if p <= 0:
        return "0"
    dec = max(1, min(8, 5 - int(math.floor(math.log10(p)))))
    return tr_num(p, dec)


def money(x: float, sign: bool = False) -> str:
    s = tr_num(x, 2)
    if sign:
        return ("+" if x >= 0 else "−") + "$" + s
    return ("−" if x < 0 else "") + "$" + s


def pct(x: float, sign: bool = True) -> str:
    s = tr_num(x, 1)
    if not sign:
        return s + "%"
    return ("+" if x >= 0 else "−") + s + "%"


def sym(symbol: str) -> str:
    return symbol.replace("-", "")


def open_msg(t: dict, reason: str) -> str:
    long = t["side"] == "LONG"
    e = t["entry"]
    tp_p = (t["tp"] - e) / e * 100
    sl_p = (t["sl"] - e) / e * 100
    return (
        f"{'🟢 LONG' if long else '🔴 SHORT'} açıldı · {sym(t['symbol'])}\n"
        f"Giriş: {price(e)}\n"
        f"TP: {price(t['tp'])} ({pct(tp_p)})\n"
        f"SL: {price(t['sl'])} ({pct(sl_p)})\n"
        f"Kaldıraç: {t['leverage']}x · Risk: {money(t['risk_usd'])}\n"
        f"Bakiye: {money(t['balance_open'])}\n"
        f"Neden: {reason}"
    )


PROTECT_TEXT = {
    "protected": "Stop girişe çekilmişti, kâr korunarak çıkıldı",
    "trail": "Fiyat geri dönmeye başladı, kâr korunarak çıkıldı",
    "time": "Süre doldu, çıkıldı",
    "external": "Pozisyon borsada kapanmış",
}


def close_msg(t: dict) -> str:
    reason = t["exit_reason"]
    side = t["side"].lower()
    pnl = t["pnl"]
    acct = pnl / t["balance_open"] * 100 if t["balance_open"] else 0.0
    head = sym(t["symbol"])
    if reason == "tp":
        return f"✅ TP oldu · {head} {side}\nKâr: {money(pnl, True)} ({pct(acct)})\nBakiye: {money(t['balance_close'])}"
    if reason == "sl":
        return f"❌ SL oldu · {head} {side}\nZarar: {money(pnl, True)} ({pct(acct)})\nBakiye: {money(t['balance_close'])}"
    return (f"🛡 Korumada çıkış · {head} {side}\n{PROTECT_TEXT.get(reason, 'Çıkıldı')}\n"
            f"Sonuç: {money(pnl, True)}\nBakiye: {money(t['balance_close'])}")
