"""Telegram mesajları: kısa ve net. Raporlar reports.py'de."""
from __future__ import annotations

import math


def tr_num(x: float, decimals: int, strip: bool = True) -> str:
    s = f"{abs(x):,.{decimals}f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    if strip and "," in s:
        s = s.rstrip("0").rstrip(",")
    return s


def price(p: float) -> str:
    if p <= 0:
        return "0"
    dec = max(1, min(8, 5 - int(math.floor(math.log10(p)))))
    return tr_num(p, dec)


def money(x: float, sign: bool = False) -> str:
    s = tr_num(x, 3 if 0 < abs(x) < 0.01 else 2, strip=False)
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


MODE_TAG = {"paper": " [PAPER]", "vst": " [VST]", "live": ""}


def _tag(t: dict) -> str:
    return MODE_TAG.get(t.get("mode", "live"), "")


def open_msg(t: dict, reason: str) -> str:
    long = t["side"] == "LONG"
    e = t["entry"]
    tp_p = (t["tp"] - e) / e * 100
    sl_p = (t["sl"] - e) / e * 100
    return (
        f"{'📈' if long else '📉'} {sym(t['symbol'])} · {t['side']} açıldı{_tag(t)}\n"
        f"🎯 Giriş: {price(e)}\n"
        f"✅ Hedef (TP): {price(t['tp'])} ({pct(tp_p)})\n"
        f"🛑 Stop (SL): {price(t['sl'])} ({pct(sl_p)})\n"
        f"⚙️ {t['leverage']}x · Risk: {money(t['risk_usd'])}\n"
        f"💼 Bakiye: {money(t['balance_open'])}\n"
        f"💡 {reason}"
    )


EXIT_NOTE = {
    "protected": "Stop girişe çekilmişti, fiyat geri dönünce çıkıldı.",
    "trail": "Fiyat TP'ye yaklaşıp geri dönmeye başladı, TP beklenmeden çıkıldı.",
    "time": "Süre doldu, işlem kapatıldı.",
    "external": "Pozisyon borsada kapanmış.",
}


def close_msg(t: dict) -> str:
    reason, pnl = t["exit_reason"], t["pnl"]
    acct = pnl / t["balance_open"] * 100 if t["balance_open"] else 0.0
    name = f"{sym(t['symbol'])} · {t['side']}"
    tag = _tag(t)
    if reason == "tp":
        head, note = f"✅ TP oldu · {name}{tag}", None
    elif reason == "sl":
        head, note = f"🛑 SL oldu · {name}{tag}", None
    elif reason in ("trail", "protected"):
        head = (f"🔒 Kâr korundu · {name}{tag}" if pnl >= 0.005 else f"⚖️ Başa baş çıkış · {name}{tag}")
        note = EXIT_NOTE[reason]
    elif reason == "time":
        head, note = f"⏱ Süre doldu · {name}{tag}", EXIT_NOTE[reason]
    else:
        head, note = f"ℹ️ Borsada kapandı · {name}{tag}", EXIT_NOTE["external"]
    label = "💰 Kâr" if pnl > 0 else "📉 Zarar" if pnl < 0 else "➖ Sonuç"
    lines = [head, f"{label}: {money(pnl, True)} ({pct(acct)})",
             f"📍 Giriş {price(t['entry'])} → Çıkış {price(t['exit_price'])}",
             f"💼 Bakiye: {money(t['balance_close'])}"]
    if note:
        lines.append(f"💡 {note}")
    return "\n".join(lines)
