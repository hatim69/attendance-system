"""Attendance engine + reports. Every employee has their own shift (or none = flexible: never late)."""
import calendar
from datetime import date as Date
from datetime import datetime

from . import config, db
from .timeutil import now_local

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WORKED = {"Present", "Late", "Half Day"}
CODES = {"Present": "P", "Late": "L", "Half Day": "HD", "Absent": "A", "Leave": "LV", "Holiday": "H", "Weekly Off": "WO", "": ""}
LEAVE_TYPES = ["Casual", "Sick", "Paid", "Unpaid", "Other"]


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _load():
    c = db.conn()
    shifts = {r["id"]: dict(r) for r in c.execute("SELECT * FROM shifts")}
    emps = [dict(r) for r in c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name")]
    holidays = {r["date"]: r["name"] for r in c.execute("SELECT * FROM holidays")}
    leaves = [dict(r) for r in c.execute("SELECT * FROM leaves")]
    return shifts, emps, holidays, leaves


def _punch_map(d1: str, d2: str, device: str | None) -> dict:
    rows = db.conn().execute(
        "SELECT device_user_id u, date(ts) d, MIN(ts) a, MAX(ts) b, COUNT(*) n FROM punches "
        "WHERE date(ts) BETWEEN ? AND ? AND COALESCE(flag,'')!='rejected' AND (? IS NULL OR source=?) GROUP BY u, d",
        (d1, d2, device, device))
    return {(r["u"], r["d"]): r for r in rows}


def evaluate(emp: dict, shift: dict | None, d: str, p, holidays: dict, leaves: list, today: str) -> dict:
    """Status and numbers for one employee on one day. `p` is the day's punch summary row or None."""
    wd = WEEKDAYS[Date.fromisoformat(d).weekday()]
    offs = [o.strip().upper() for o in (shift["weekly_off"] if shift else config.WEEKLY_OFF).split(",") if o.strip()]
    off = wd in offs
    out = {"date": d, "status": "", "first_in": "", "last_out": "", "hours": 0.0, "punches": 0,
           "late_min": 0, "early_min": 0, "ot_min": 0, "note": ""}
    if p:
        first, last, n = datetime.fromisoformat(p["a"]), datetime.fromisoformat(p["b"]), p["n"]
        out.update(first_in=p["a"][11:19], punches=n)
        if n > 1:
            out["last_out"] = p["b"][11:19]
            out["hours"] = round((last - first).total_seconds() / 3600, 2)
        else:
            out["note"] = "missing out punch"
        status = "Present"
        if shift and not off and d not in holidays:
            start, end = _hm(shift["start"]), _hm(shift["end"])
            fm = first.hour * 60 + first.minute
            if fm > start + shift["grace_min"]:
                out["late_min"], status = fm - start, "Late"
            if n > 1:
                lm = last.hour * 60 + last.minute
                if lm < end:
                    out["early_min"] = end - lm
                if lm - end >= shift["ot_after_min"]:
                    out["ot_min"] = lm - end
                if out["hours"] < shift["half_day_hours"]:
                    status = "Half Day"
        else:  # came in on a holiday / weekly off: all worked time counts as overtime
            out["ot_min"] = int(out["hours"] * 60)
            kind = "holiday" if d in holidays else "weekly off"
            out["note"] = (out["note"] + "; " if out["note"] else "") + f"worked on {kind}"
        out["status"] = status
        return out
    if d > today or (emp.get("joined") and d < emp["joined"]):
        return out  # in the future, or before the employee's joining date: not counted
    leave = next((l for l in leaves if l["device_user_id"] == emp["device_user_id"] and l["start_date"] <= d <= l["end_date"]), None)
    if leave:
        out["status"], out["note"] = "Leave", leave["type"]
    elif d in holidays:
        out["status"], out["note"] = "Holiday", holidays[d]
    elif off:
        out["status"] = "Weekly Off"
    else:
        out["status"] = "Absent"
    return out


def _month_days(month: str) -> tuple[list[str], str, str]:
    y, m = map(int, month.split("-"))
    n = calendar.monthrange(y, m)[1]
    days = [f"{y:04d}-{m:02d}-{i:02d}" for i in range(1, n + 1)]
    return days, days[0], days[-1]


def daily(date: str, device: str | None = None) -> list[dict]:
    """One row per active employee for `date` (YYYY-MM-DD)."""
    shifts, emps, holidays, leaves = _load()
    pm, today = _punch_map(date, date, device), now_local().date().isoformat()
    rows = []
    for e in emps:
        sh = shifts.get(e["shift_id"])
        r = evaluate(e, sh, date, pm.get((e["device_user_id"], date)), holidays, leaves, today)
        r.update(device_user_id=e["device_user_id"], name=e["name"], department=e["department"],
                 shift=sh["name"] if sh else "")
        rows.append(r)
    return rows


def _month_results(month: str, device: str | None):
    shifts, emps, holidays, leaves = _load()
    days, d1, d2 = _month_days(month)
    pm, today = _punch_map(d1, d2, device), now_local().date().isoformat()
    for e in emps:
        sh = shifts.get(e["shift_id"])
        yield e, sh, [evaluate(e, sh, d, pm.get((e["device_user_id"], d)), holidays, leaves, today) for d in days]


def monthly(month: str, device: str | None = None) -> list[dict]:
    """Per-employee summary for `month` (YYYY-MM)."""
    out = []
    for e, sh, res in _month_results(month, device):
        cnt = lambda st: sum(r["status"] == st for r in res)
        paid_leave = sum(r["status"] == "Leave" and r["note"] != "Unpaid" for r in res)
        full = cnt("Present") + cnt("Late")
        out.append({
            "device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
            "shift": sh["name"] if sh else "",
            "days_present": full + cnt("Half Day"), "late_days": cnt("Late"), "half_days": cnt("Half Day"),
            "absent": cnt("Absent"), "leave": cnt("Leave"), "holidays": cnt("Holiday"), "weekly_offs": cnt("Weekly Off"),
            "total_hours": round(sum(r["hours"] for r in res), 2),
            "late_minutes": sum(r["late_min"] for r in res), "early_minutes": sum(r["early_min"] for r in res),
            "overtime_hours": round(sum(r["ot_min"] for r in res) / 60, 2),
            "paid_days": full + 0.5 * cnt("Half Day") + paid_leave + cnt("Holiday") + cnt("Weekly Off"),
        })
    return out


def grid(month: str, device: str | None = None) -> dict:
    """Attendance sheet: one row per employee, one code per day."""
    days, _, _ = _month_days(month)
    rows = [{"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
             "codes": [CODES[r["status"]] for r in res]} for e, _, res in _month_results(month, device)]
    return {"days": [int(d[8:]) for d in days], "rows": rows}


def payroll(month: str, device: str | None = None) -> list[dict]:
    """Simple pay sheet: monthly salary / calendar days x paid days. No tax, PF/ESI or allowances."""
    days, _, d2 = _month_days(month)
    complete = d2 < now_local().date().isoformat()
    sal = {r["device_user_id"]: r["salary"] for r in db.conn().execute("SELECT device_user_id, salary FROM employees")}
    out = []
    for r in monthly(month, device):
        base = sal.get(r["device_user_id"], 0) or 0
        per_day = base / len(days)
        out.append({"device_user_id": r["device_user_id"], "name": r["name"], "department": r["department"],
                    "monthly_salary": base, "days_in_month": len(days), "paid_days": r["paid_days"],
                    "unpaid_days": round(len(days) - r["paid_days"], 2) if complete else "",
                    "net_pay": round(per_day * r["paid_days"], 2), "month_complete": complete})
    return out
