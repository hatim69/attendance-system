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
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'manager',      -- admin | hr | manager
    department TEXT NOT NULL DEFAULT '',       -- managers see only this department
    active INTEGER NOT NULL DEFAULT 1,
    permissions TEXT NOT NULL DEFAULT '*'      -- hr only: comma list of areas they may use, '*' = all
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    status INTEGER
);
CREATE TABLE IF NOT EXISTS shift_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    shift_id INTEGER NOT NULL,                 -- overrides the employee's normal shift on these dates
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payroll_months (
    month TEXT PRIMARY KEY,                    -- YYYY-MM; employees can open their payslip only once it is published
    published INTEGER NOT NULL DEFAULT 0,
    published_by TEXT DEFAULT '',
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS payroll_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    device_user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    amount REAL NOT NULL                       -- positive = extra pay, negative = deduction
);
CREATE TABLE IF NOT EXISTS employee_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    doc_type TEXT NOT NULL DEFAULT 'Other',     -- Aadhaar card, PAN card, Offer letter, ...
    filename TEXT NOT NULL,                     -- original name, for display/download
    stored TEXT NOT NULL,                       -- path under DOCS_DIR (generated, never user-controlled)
    size INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS masters (            -- pick lists for the employee form
    kind TEXT NOT NULL,                         -- department | designation | branch
    name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY(kind, name)
);
CREATE TABLE IF NOT EXISTS loans (              -- loans and salary advances, recovered from salary in monthly instalments
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'Loan',          -- Loan | Advance
    amount REAL NOT NULL,
    emi REAL NOT NULL,                          -- deducted each month from start_month until repaid
    start_month TEXT NOT NULL,                  -- YYYY-MM
    note TEXT NOT NULL DEFAULT '',
    closed INTEGER NOT NULL DEFAULT 0,          -- 1 = stopped / written off by HR
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loan_repayments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL,
    month TEXT NOT NULL,                        -- YYYY-MM
    amount REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'payroll',     -- payroll (recorded when the month is published) | manual (cash etc.)
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (             -- day-by-day shift plan; overrides shift changes and the normal shift
    device_user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    shift_id INTEGER,                           -- NULL with off=1 means a day off
    off INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(device_user_id, date)
);
CREATE TABLE IF NOT EXISTS leave_consents (     -- the employee's signed acknowledgement when a leave goes beyond their balance
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    device_user_id TEXT NOT NULL,
    text TEXT NOT NULL,                         -- exactly what the employee was shown and agreed to
    typed_name TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wfh_requests (      -- work-from-home days asked for in the phone app (leave policy: paid 0.8)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    applied_at TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT
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
    "note": "TEXT DEFAULT ''",   # remark on manual punches / corrections
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
    for col, decl in {"pin_hash": "TEXT", "shift_id": "INTEGER", "salary": "REAL NOT NULL DEFAULT 0", "joined": "TEXT",
                       "designation": "TEXT NOT NULL DEFAULT ''", "branch": "TEXT NOT NULL DEFAULT ''",
                       "mobile": "TEXT NOT NULL DEFAULT ''", "father_name": "TEXT NOT NULL DEFAULT ''",
                       "remote_ok": "INTEGER NOT NULL DEFAULT 0",   # may check in from anywhere (remote / field work)
                       "left_on": "TEXT"}.items():   # last working day of someone who resigned
        if col not in emp_cols:
            c.execute(f"ALTER TABLE employees ADD COLUMN {col} {decl}")
    leave_cols = {r["name"] for r in c.execute("PRAGMA table_info(leaves)")}
    for col, decl in {"status": "TEXT NOT NULL DEFAULT 'approved'",   # approved | pending | rejected
                      "applied_on": "TEXT", "requested_by": "TEXT NOT NULL DEFAULT ''",
                      "decided_by": "TEXT NOT NULL DEFAULT ''", "decided_at": "TEXT",
                      "group_id": "TEXT",   # one application split into several parts (Casual / Adhoc / ...) shares a group
                      "lop": "REAL NOT NULL DEFAULT 0",
                      "half": "INTEGER NOT NULL DEFAULT 0"}.items():   # half-day leave (one date, counts 0.5)   # pay cut in days' salary per leave day (1.5 / 2 beyond balance)
        if col not in leave_cols:
            c.execute(f"ALTER TABLE leaves ADD COLUMN {col} {decl}")
    c.execute("UPDATE leaves SET type='Casual' WHERE type='Normal'")   # the policy calls it Casual
    for table, cols in {"payroll_adjustments": {"date": "TEXT"},                   # when the bonus / fine / ... was given
                        "holidays": {"optional": "INTEGER NOT NULL DEFAULT 0"},   # festival list: taken as Festive leave
                        "shifts": {"break_start": "TEXT NOT NULL DEFAULT ''", "break_end": "TEXT NOT NULL DEFAULT ''"},
                        "sites": {"kind": "TEXT NOT NULL DEFAULT 'office'",      # office | home
                                  "device_user_id": "TEXT"}}.items():           # home sites belong to one employee
        have_cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have_cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    if "permissions" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '*'")
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
        "SELECT 1 FROM punches WHERE device_user_id=? AND COALESCE(flag,'')!='rejected' "
        "AND ABS(strftime('%s', ts) - strftime('%s', ?)) < ? LIMIT 1",
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


def audit(user: str, action: str, status: int | None = None):
    from .timeutil import now_local
    c = conn()
    c.execute("INSERT INTO audit(ts,user,action,status) VALUES (?,?,?,?)",
              (now_local().strftime("%Y-%m-%d %H:%M:%S"), user, action[:300], status))
    c.commit()
