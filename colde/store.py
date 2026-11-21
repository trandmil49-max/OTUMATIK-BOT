from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 symbol TEXT, side TEXT, tf TEXT, status TEXT, mode TEXT,
 entry REAL, sl REAL, tp REAL, init_sl REAL, qty REAL, leverage INTEGER,
 risk_usd REAL, notional REAL, opened_ms INTEGER, closed_ms INTEGER,
 exit_price REAL, exit_reason TEXT, pnl REAL, balance_open REAL, balance_close REAL,
 peak REAL, protected INTEGER DEFAULT 0, trailing INTEGER DEFAULT 0,
 sl_order_id TEXT, tp_order_id TEXT, last_check_ms INTEGER, level REAL, score REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _rows(self, sql: str, args: tuple = ()) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def insert_trade(self, t: dict) -> int:
        cols = ",".join(t.keys())
        q = ",".join("?" for _ in t)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO trades({cols}) VALUES({q})", tuple(t.values()))
            self.db.commit()
            return cur.lastrowid

    def update_trade(self, tid: int, **fields) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), tid))
            self.db.commit()

    def get_trade(self, tid: int) -> Optional[dict]:
        r = self._rows("SELECT * FROM trades WHERE id=?", (tid,))
        return r[0] if r else None

    def open_trades(self) -> list:
        return self._rows("SELECT * FROM trades WHERE status='open' ORDER BY id")

    def opened_since(self, ms: int) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM trades WHERE opened_ms>=?", (ms,))[0]["n"]

    def closed_between(self, a: int, b: int) -> list:
        return self._rows("SELECT * FROM trades WHERE status='closed' AND closed_ms>=? AND closed_ms<? ORDER BY closed_ms", (a, b))

    def last_closed(self, symbol: str) -> Optional[dict]:
        r = self._rows("SELECT * FROM trades WHERE status='closed' AND symbol=? ORDER BY closed_ms DESC LIMIT 1", (symbol,))
        return r[0] if r else None

    def kv_get(self, k: str, default: Optional[str] = None) -> Optional[str]:
        r = self._rows("SELECT v FROM kv WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def kv_set(self, k: str, v: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.db.commit()
