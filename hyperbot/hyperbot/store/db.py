"""SQLite persistence: leader snapshots, orders, positions and the daily ledger.

Every order is written BEFORE it is sent and updated after, so a crash mid-flight
leaves a record of intent rather than a silent gap.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..log import get_logger
from ..util import now_ms, utc_day

log = get_logger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    label TEXT,
    elite_score REAL,
    rising_score REAL,
    perp_capital REAL,
    account_value REAL,
    roi_month REAL,
    max_drawdown REAL,
    consistency REAL,
    r_squared REAL,
    pace_ratio REAL,
    days_active REAL,
    breakdown TEXT,
    updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS roster (
    address TEXT PRIMARY KEY,
    label TEXT,
    allocation REAL,
    source TEXT,
    added_ms INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER,
    coin TEXT,
    side TEXT,
    size REAL,
    limit_price REAL,
    notional REAL,
    kind TEXT,
    reduce_only INTEGER,
    dry_run INTEGER,
    status TEXT,
    filled_size REAL,
    avg_price REAL,
    order_id INTEGER,
    error TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts_ms INTEGER PRIMARY KEY,
    day TEXT,
    account_value REAL,
    gross_notional REAL,
    day_pnl REAL,
    positions INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER,
    severity TEXT,
    title TEXT,
    body TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts_ms);
CREATE INDEX IF NOT EXISTS idx_equity_day ON equity(day);
"""


class Store:
    def __init__(self, path: str = "hyperbot.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # ---- traders ---------------------------------------------------------- #

    def save_trader(self, scored: Any) -> None:
        metrics = scored.metrics
        month = metrics.window("month")
        self.connection.execute(
            """INSERT INTO traders (address,label,elite_score,rising_score,perp_capital,
               account_value,roi_month,max_drawdown,consistency,r_squared,pace_ratio,
               days_active,breakdown,updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET
                 label=excluded.label, elite_score=excluded.elite_score,
                 rising_score=excluded.rising_score, perp_capital=excluded.perp_capital,
                 account_value=excluded.account_value, roi_month=excluded.roi_month,
                 max_drawdown=excluded.max_drawdown, consistency=excluded.consistency,
                 r_squared=excluded.r_squared, pace_ratio=excluded.pace_ratio,
                 days_active=excluded.days_active, breakdown=excluded.breakdown,
                 updated_ms=excluded.updated_ms""",
            (
                metrics.address, metrics.label, scored.elite.total, scored.rising.total,
                metrics.perp_capital, metrics.account_value, month.roi, month.max_drawdown,
                month.consistency, month.r_squared, metrics.pace_ratio, metrics.days_active,
                json.dumps({"elite": scored.elite.components, "rising": scored.rising.components}),
                now_ms(),
            ),
        )
        self.connection.commit()

    def replace_roster(self, entries: list[tuple[str, str, float, str]]) -> None:
        self.connection.execute("DELETE FROM roster")
        self.connection.executemany(
            "INSERT INTO roster (address,label,allocation,source,added_ms) VALUES (?,?,?,?,?)",
            [(a, l, alloc, src, now_ms()) for a, l, alloc, src in entries],
        )
        self.connection.commit()

    def load_roster(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM roster ORDER BY allocation DESC"))

    def top_traders(self, column: str = "elite_score", limit: int = 20) -> list[sqlite3.Row]:
        if column not in ("elite_score", "rising_score"):
            raise ValueError(f"refusing to sort by unvetted column {column!r}")
        return list(
            self.connection.execute(
                f"SELECT * FROM traders ORDER BY {column} DESC LIMIT ?", (limit,)
            )
        )

    # ---- orders ----------------------------------------------------------- #

    def record_order_intent(self, order: Any, kind: str, dry_run: bool) -> int:
        cursor = self.connection.execute(
            """INSERT INTO orders (ts_ms,coin,side,size,limit_price,notional,kind,
               reduce_only,dry_run,status,reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_ms(), order.coin, order.side, order.size, order.limit_price,
                order.notional, kind, int(order.reduce_only), int(dry_run),
                "intent", order.reason,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def record_order_result(self, row_id: int, result: Any) -> None:
        status = "dry_run" if result.dry_run else ("filled" if result.ok else "rejected")
        if result.ok and not result.dry_run and not result.filled_size:
            status = "resting" if result.resting else "no_fill"
        self.connection.execute(
            "UPDATE orders SET status=?,filled_size=?,avg_price=?,order_id=?,error=? WHERE id=?",
            (status, result.filled_size, result.avg_price, result.order_id, result.error, row_id),
        )
        self.connection.commit()

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.connection.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        )

    # ---- equity / events -------------------------------------------------- #

    def record_equity(
        self, account_value: float, gross: float, day_pnl: float, positions: int
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?)",
            (now_ms(), utc_day(), account_value, gross, day_pnl, positions),
        )
        self.connection.commit()

    def record_event(self, severity: str, title: str, body: str = "") -> None:
        self.connection.execute(
            "INSERT INTO events (ts_ms,severity,title,body) VALUES (?,?,?,?)",
            (now_ms(), severity, title, body),
        )
        self.connection.commit()

    def recent_events(self, limit: int = 60) -> list[sqlite3.Row]:
        """Newest last, so the feed reads in chronological order."""
        rows = list(
            self.connection.execute(
                "SELECT ts_ms,severity,title,body FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )
        return list(reversed(rows))
