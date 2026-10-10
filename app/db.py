import sqlite3
import threading
from datetime import datetime

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    department TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    ts TEXT NOT NULL,            -- local time, ISO "YYYY-MM-DD HH:MM:SS"
    status INTEGER DEFAULT 0,    -- device punch state (0 in, 1 out, ...)
    source TEXT DEFAULT '',      -- device serial / address / 'manual'
    UNIQUE(source, device_user_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_punches_ts ON punches(ts);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c, _local.path = c, config.DB_PATH
    return c


def init():
    conn().executescript(SCHEMA)
    conn().commit()


def add_punch(device_user_id: str, ts: datetime | str, status: int = 0, source: str = "") -> dict | None:
    """Insert a punch. Returns the stored record, or None if duplicate/debounced."""
    if isinstance(ts, datetime):
        ts = ts.strftime("%Y-%m-%d %H:%M:%S")
    c = conn()
    near = c.execute(
        "SELECT 1 FROM punches WHERE device_user_id=? AND ABS(strftime('%s', ts) - strftime('%s', ?)) < ? LIMIT 1",
        (device_user_id, ts, config.DEBOUNCE_SECONDS),
    ).fetchone()
    if near:
        return None
    cur = c.execute(
        "INSERT OR IGNORE INTO punches(device_user_id, ts, status, source) VALUES (?,?,?,?)",
        (device_user_id, ts, status, source),
    )
    c.commit()
    if cur.rowcount == 0:
        return None
    emp = c.execute("SELECT name, department FROM employees WHERE device_user_id=?", (device_user_id,)).fetchone()
    return {
        "device_user_id": device_user_id,
        "name": emp["name"] if emp else f"Unknown ({device_user_id})",
        "department": emp["department"] if emp else "",
        "ts": ts,
        "status": status,
        "source": source,
    }
