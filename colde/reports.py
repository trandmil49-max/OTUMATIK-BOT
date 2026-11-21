from __future__ import annotations

from .messages import money, pct, sym

TAG = {"tp": "TP", "sl": "SL", "protected": "korumada", "trail": "korumada", "time": "süre", "external": "borsa"}


def build_report(title: str, trades: list, balance: float, list_trades: bool = True) -> str:
    n = len(trades)
    if n == 0:
        return f"📊 {title}\nİşlem yok.\nBakiye: {money(balance)}"
    tp = sum(1 for t in trades if t["exit_reason"] == "tp")
    sl = sum(1 for t in trades if t["exit_reason"] == "sl")
    prot = n - tp - sl
    wins = sum(1 for t in trades if t["pnl"] > 0)
    net = sum(t["pnl"] for t in trades)
    start = trades[0]["balance_open"] or 0.0
    lines = [
        f"📊 {title}",
        f"İşlem: {n} (✅ {tp} TP · 🛡 {prot} korumada · ❌ {sl} SL)",
        f"Kazanma oranı: {pct(wins / n * 100, sign=False)}",
        f"Net: {money(net, True)}" + (f" ({pct(net / start * 100)})" if start else ""),
        f"Bakiye: {money(balance)}",
    ]
    if list_trades:
        lines.append("")
        for t in trades[:15]:
            lines.append(f"{sym(t['symbol'])} {t['side'].lower()} {money(t['pnl'], True)} · {TAG.get(t['exit_reason'], '')}")
        if n > 15:
            lines.append(f"… +{n - 15} işlem daha")
    return "\n".join(lines)
