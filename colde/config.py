"""Ayarlar. Hepsi ortam değişkeniyle (Railway Variables) değiştirilebilir."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _f(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(float(_env(name, str(default))))


def _b(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "evet", "on")


@dataclass
class Settings:
    # --- çalışma modu: paper (sahte para, anahtar gerekmez) | vst (BingX demo) | live (gerçek) ---
    mode: str = "paper"
    bingx_api_key: str = ""
    bingx_api_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    db_path: str = "colde.db"
    timezone: str = "Europe/Istanbul"
    paper_start_balance: float = 12.0
    balance_override: float = 0.0       # >0 ise bakiye bu sayıymış gibi boyutlandırır (vst için)
    trading_enabled: bool = True
    send_startup_message: bool = True

    # --- günlük sınırlar (kumar yok) ---
    max_trades_per_day: int = 5
    max_open_positions: int = 3
    max_consecutive_sl: int = 2          # gün içinde üst üste 2 SL -> o gün dur
    daily_loss_limit_pct: float = 3.0    # gün başı bakiyenin %3'ü kaybedilirse dur
    cooldown_after_sl_h: float = 12.0
    cooldown_after_win_h: float = 2.0
    max_hold_h: float = 24.0

    # --- evren ---
    min_quote_volume: float = 1_000_000  # sadece kripto için 24s hacim filtresi (USDT)
    exclude_prefixes: tuple = ()
    max_24h_move_pct: float = 15.0       # 24s'te %15+ oynayan coine girme

    # --- seviye / setup ---
    min_level_strength: float = 3.0
    min_score: float = 8.0
    confirm_closes: int = 2
    setup_lookback: int = 8
    min_tp_pct: float = 0.40
    tp_fraction: float = 0.85            # TP = giriş ile bir sonraki seviye arasının %85'i
    tp_atr_cap: float = 2.0
    min_rr: float = 0.5
    max_sl_pct: float = 2.5
    watch_atr: float = 1.5
    max_entry_drift_atr: float = 0.5

    # --- risk ---
    risk_low_pct: float = 1.0            # bakiye <= risk_low_balance iken işlem başına risk %
    risk_high_pct: float = 2.5           # bakiye >= risk_high_balance iken
    risk_low_balance: float = 25.0
    risk_high_balance: float = 100.0

    # --- maliyet / yönetim ---
    fee_pct: float = 0.05                # taker, tek yön
    slippage_pct: float = 0.03
    protect_arm: float = 0.5             # TP yolunun %50'sinde stop girişe çekilir
    trail_arm: float = 0.7               # %70'te kâr geri vermeye başlarsa çık
    giveback: float = 0.35
    be_buffer_pct: float = 0.12          # girişin biraz üstü (komisyonu karşılar)

    # --- zamanlama ---
    scan_delay_s: int = 15
    manage_interval_s: int = 10
    levels_refresh_min: int = 60
    report_hour: int = 23

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            mode=_env("MODE", "paper").lower(),
            bingx_api_key=_env("BINGX_API_KEY"),
            bingx_api_secret=_env("BINGX_API_SECRET"),
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            db_path=_env("DB_PATH", "colde.db"),
            timezone=_env("TIMEZONE", "Europe/Istanbul"),
            paper_start_balance=_f("PAPER_START_BALANCE", 12.0),
            balance_override=_f("BALANCE_OVERRIDE", 0.0),
            trading_enabled=_b("TRADING_ENABLED", True),
            send_startup_message=_b("SEND_STARTUP_MESSAGE", True),
            max_trades_per_day=_i("MAX_TRADES_PER_DAY", 5),
            max_open_positions=_i("MAX_OPEN_POSITIONS", 3),
            max_consecutive_sl=_i("MAX_CONSECUTIVE_SL", 2),
            daily_loss_limit_pct=_f("DAILY_LOSS_LIMIT_PCT", 3.0),
            cooldown_after_sl_h=_f("COOLDOWN_AFTER_SL_H", 12.0),
            cooldown_after_win_h=_f("COOLDOWN_AFTER_WIN_H", 2.0),
            max_hold_h=_f("MAX_HOLD_H", 24.0),
            min_quote_volume=_f("MIN_QUOTE_VOLUME", 1_000_000),
            exclude_prefixes=tuple(p for p in _env("EXCLUDE_PREFIXES").upper().split(",") if p),
            max_24h_move_pct=_f("MAX_24H_MOVE_PCT", 15.0),
            min_level_strength=_f("MIN_LEVEL_STRENGTH", 3.0),
            min_score=_f("MIN_SCORE", 8.0),
            confirm_closes=_i("CONFIRM_CLOSES", 2),
            min_tp_pct=_f("MIN_TP_PCT", 0.40),
            tp_fraction=_f("TP_FRACTION", 0.85),
            tp_atr_cap=_f("TP_ATR_CAP", 2.0),
            min_rr=_f("MIN_RR", 0.5),
            max_sl_pct=_f("MAX_SL_PCT", 2.5),
            risk_low_pct=_f("RISK_LOW_PCT", 1.0),
            risk_high_pct=_f("RISK_HIGH_PCT", 2.5),
            fee_pct=_f("FEE_PCT", 0.05),
            slippage_pct=_f("SLIPPAGE_PCT", 0.03),
            protect_arm=_f("PROTECT_ARM", 0.5),
            trail_arm=_f("TRAIL_ARM", 0.7),
            giveback=_f("GIVEBACK", 0.35),
            report_hour=_i("REPORT_HOUR", 23),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.mode not in ("paper", "vst", "live"):
            raise ValueError(f"MODE paper, vst veya live olmalı (şu an: {self.mode})")
        if self.mode in ("vst", "live") and not (self.bingx_api_key and self.bingx_api_secret):
            raise ValueError("vst/live modunda BINGX_API_KEY ve BINGX_API_SECRET zorunlu")
        if not 1 <= self.confirm_closes <= 4:
            raise ValueError("CONFIRM_CLOSES 1-4 arası olmalı")
