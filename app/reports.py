from datetime import datetime

from . import config, db


def daily(date: str) -> list[dict]:
    """One row per active employee for `date` (YYYY-MM-DD): first in, last out, hours, status."""
    c = db.conn()
    h, m = map(int, config.SHIFT_START.split(":"))
    limit = f"{h * 60 + m + config.GRACE_MINUTES}"
    rows = []
    for e in c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name"):
        p = c.execute(
            "SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM punches WHERE device_user_id=? AND date(ts)=?",
            (e["device_user_id"], date),
        ).fetchone()
        first, last, n = p["a"], p["b"], p["n"]
        hours, status = 0.0, "Absent"
        if n:
            f = datetime.fromisoformat(first)
            status = "Late" if f.hour * 60 + f.minute > int(limit) else "Present"
            if n > 1:
                hours = round((datetime.fromisoformat(last) - f).total_seconds() / 3600, 2)
        rows.append({
            "device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
            "first_in": first[11:19] if first else "", "last_out": last[11:19] if last and n > 1 else "",
            "hours": hours, "punches": n, "status": status,
        })
    return rows


def monthly(month: str) -> list[dict]:
    """Per-employee totals for `month` (YYYY-MM)."""
    c = db.conn()
    out = []
    for e in c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name"):
        days = c.execute(
            "SELECT date(ts) d, MIN(ts) a, MAX(ts) b, COUNT(*) n FROM punches "
            "WHERE device_user_id=? AND strftime('%Y-%m', ts)=? GROUP BY date(ts)",
            (e["device_user_id"], month),
        ).fetchall()
        total = sum(
            (datetime.fromisoformat(d["b"]) - datetime.fromisoformat(d["a"])).total_seconds() / 3600
            for d in days if d["n"] > 1
        )
        out.append({"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
                    "days_present": len(days), "total_hours": round(total, 2)})
    return out
