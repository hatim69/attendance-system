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
    active INTEGER NOT NULL DEFAULT 1,
    pin_hash TEXT
);
CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    ts TEXT NOT NULL,            -- local time, ISO "YYYY-MM-DD HH:MM:SS"
    status INTEGER DEFAULT 0,    -- 0 in, 1 out, ...
    source TEXT DEFAULT '',      -- device id / 'app' / 'manual'
    UNIQUE(source, device_user_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_punches_ts ON punches(ts);
CREATE TABLE IF NOT EXISTS devices (
    dev_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    location TEXT DEFAULT '',
    model TEXT DEFAULT '',
    firmware TEXT DEFAULT '',
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start TEXT NOT NULL,              -- "HH:MM"
    end TEXT NOT NULL,                -- "HH:MM" (same day; overnight shifts are not supported yet)
    grace_min INTEGER NOT NULL DEFAULT 10,
    half_day_hours REAL NOT NULL DEFAULT 4,
    ot_after_min INTEGER NOT NULL DEFAULT 30,   -- overtime counts once out-time is this many minutes past shift end
    weekly_off TEXT NOT NULL DEFAULT 'SUN'      -- comma list: MON,TUE,WED,THU,FRI,SAT,SUN
);
CREATE TABLE IF NOT EXISTS holidays (
    date TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'Casual',        -- 'Unpaid' is unpaid, anything else paid
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 150,
    active INTEGER NOT NULL DEFAULT 1
);
"""
# Columns added after the first release; applied idempotently.
PUNCH_COLUMNS = {
    "method": "TEXT DEFAULT 'device'",
    "lat": "REAL", "lng": "REAL", "accuracy": "REAL",
    "selfie": "TEXT",
    "site_id": "INTEGER", "distance_m": "REAL",
    "flag": "TEXT DEFAULT ''",   # '', 'outside_geofence', 'rejected', 'approved'
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c, _local.path = c, config.DB_PATH
    return c


def canon_id(value) -> str:
    """Canonical employee/punch id: trimmed, and for all-digit ids leading zeros are ignored, so 00217315 and 217315
    are the same person (the vendor system treated them as two employees)."""
    v = str(value or "").strip()
    return (v.lstrip("0") or "0") if v.isdigit() else v


def _canonicalize(c):
    """One-time (idempotent) merge of ids that differ only by leading zeros."""
    ids = {r[0] for r in c.execute("SELECT device_user_id FROM employees UNION SELECT device_user_id FROM punches "
                                   "UNION SELECT device_user_id FROM leaves")}
    for old in sorted(i for i in ids if canon_id(i) != i):
        new = canon_id(old)
        c.execute("UPDATE OR IGNORE punches SET device_user_id=? WHERE device_user_id=?", (new, old))
        c.execute("DELETE FROM punches WHERE device_user_id=?", (old,))   # exact duplicates of already-present punches
        c.execute("UPDATE leaves SET device_user_id=? WHERE device_user_id=?", (new, old))
        keep = c.execute("SELECT 1 FROM employees WHERE device_user_id=?", (new,)).fetchone()
        if keep:   # both rows exist: fill any gaps in the canonical row from the old one, then drop the old one
            c.execute("UPDATE employees SET pin_hash=COALESCE(pin_hash,(SELECT pin_hash FROM employees WHERE device_user_id=?)), "
                      "shift_id=COALESCE(shift_id,(SELECT shift_id FROM employees WHERE device_user_id=?)), "
                      "joined=COALESCE(joined,(SELECT joined FROM employees WHERE device_user_id=?)) WHERE device_user_id=?",
                      (old, old, old, new))
            c.execute("DELETE FROM employees WHERE device_user_id=?", (old,))
        else:
            c.execute("UPDATE employees SET device_user_id=? WHERE device_user_id=?", (new, old))


def init():
    c = conn()
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(punches)")}
    for col, decl in PUNCH_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE punches ADD COLUMN {col} {decl}")
    emp_cols = {r["name"] for r in c.execute("PRAGMA table_info(employees)")}
    for col, decl in {"pin_hash": "TEXT", "shift_id": "INTEGER", "salary": "REAL NOT NULL DEFAULT 0", "joined": "TEXT"}.items():
        if col not in emp_cols:
            c.execute(f"ALTER TABLE employees ADD COLUMN {col} {decl}")
    _canonicalize(c)
    c.commit()


def add_punch(device_user_id: str, ts: datetime | str, status: int = 0, source: str = "", **extra) -> dict | None:
    """Insert a punch. Returns the stored record, or None if duplicate/debounced.
    extra may hold: method, lat, lng, accuracy, selfie, site_id, distance_m, flag."""
    device_user_id = canon_id(device_user_id)
    if isinstance(ts, datetime):
        ts = ts.strftime("%Y-%m-%d %H:%M:%S")
    c = conn()
    near = c.execute(
        "SELECT 1 FROM punches WHERE device_user_id=? AND ABS(strftime('%s', ts) - strftime('%s', ?)) < ? LIMIT 1",
        (device_user_id, ts, config.DEBOUNCE_SECONDS),
    ).fetchone()
    if near:
        return None
    extra.setdefault("method", "device")
    cols = ["device_user_id", "ts", "status", "source"] + list(extra)
    cur = c.execute(
        f"INSERT OR IGNORE INTO punches({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [device_user_id, ts, status, source] + list(extra.values()),
    )
    c.commit()
    if cur.rowcount == 0:
        return None
    emp = c.execute("SELECT name, department FROM employees WHERE device_user_id=?", (device_user_id,)).fetchone()
    return {
        "id": cur.lastrowid,
        "device_user_id": device_user_id,
        "name": emp["name"] if emp else f"Unknown ({device_user_id})",
        "department": emp["department"] if emp else "",
        "ts": ts,
        "status": status,
        "source": source,
        "method": extra["method"],
        "flag": extra.get("flag", ""),
    }


def touch_device(dev_id: str, seen: str, model: str = "", firmware: str = ""):
    """Register a fingerprint device on first contact and record when it was last heard from."""
    c = conn()
    c.execute(
        "INSERT INTO devices(dev_id, name, model, firmware, last_seen) VALUES (?,?,?,?,?) "
        "ON CONFLICT(dev_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "model=CASE WHEN excluded.model!='' THEN excluded.model ELSE model END, "
        "firmware=CASE WHEN excluded.firmware!='' THEN excluded.firmware ELSE firmware END",
        (dev_id, dev_id, model, firmware, seen),
    )
    c.commit()
