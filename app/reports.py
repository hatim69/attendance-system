"""Attendance engine + reports. Every employee has their own shift (or none = flexible: never late)."""
import calendar
from datetime import date as Date
from datetime import datetime

from . import access, config, db
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
    frag, args = access.scope_sql("department")
    emps = [dict(r) for r in c.execute("SELECT * FROM employees WHERE active=1" + frag + " ORDER BY name", args)]
    holidays = {r["date"]: r["name"] for r in c.execute("SELECT * FROM holidays")}
    leaves = [dict(r) for r in c.execute("SELECT * FROM leaves WHERE status='approved'")]
    changes: dict[str, list[dict]] = {}
    for r in c.execute("SELECT * FROM shift_changes ORDER BY id"):
        changes.setdefault(r["device_user_id"], []).append(dict(r))
    return shifts, emps, holidays, leaves, changes


def shift_on(e: dict, d: str, shifts: dict, changes: dict) -> dict | None:
    """The shift an employee works on date `d`: the latest date-range change that covers it, else their normal shift."""
    for ch in reversed(changes.get(e["device_user_id"], [])):
        if ch["start_date"] <= d <= ch["end_date"]:
            return shifts.get(ch["shift_id"])
    return shifts.get(e["shift_id"])


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
    shifts, emps, holidays, leaves, changes = _load()
    pm, today = _punch_map(date, date, device), now_local().date().isoformat()
    rows = []
    for e in emps:
        sh = shift_on(e, date, shifts, changes)
        r = evaluate(e, sh, date, pm.get((e["device_user_id"], date)), holidays, leaves, today)
        r.update(device_user_id=e["device_user_id"], name=e["name"], department=e["department"],
                 designation=e["designation"], branch=e["branch"], shift=sh["name"] if sh else "")
        rows.append(r)
    return rows


def _month_results(month: str, device: str | None):
    shifts, emps, holidays, leaves, changes = _load()
    days, d1, d2 = _month_days(month)
    pm, today = _punch_map(d1, d2, device), now_local().date().isoformat()
    for e in emps:
        yield e, shifts.get(e["shift_id"]), [
            evaluate(e, shift_on(e, d, shifts, changes), d, pm.get((e["device_user_id"], d)), holidays, leaves, today) for d in days]


def _summary(e: dict, sh: dict | None, res: list[dict]) -> dict:
    cnt = lambda st: sum(r["status"] == st for r in res)
    paid_leave = sum(r["status"] == "Leave" and r["note"] != "Unpaid" for r in res)
    full = cnt("Present") + cnt("Late")
    return {
        "device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
        "designation": e["designation"], "branch": e["branch"], "shift": sh["name"] if sh else "",
        "days_present": full + cnt("Half Day"), "late_days": cnt("Late"), "half_days": cnt("Half Day"),
        "absent": cnt("Absent"), "leave": cnt("Leave"), "holidays": cnt("Holiday"), "weekly_offs": cnt("Weekly Off"),
        "total_hours": round(sum(r["hours"] for r in res), 2),
        "late_minutes": sum(r["late_min"] for r in res), "early_minutes": sum(r["early_min"] for r in res),
        "overtime_hours": round(sum(r["ot_min"] for r in res) / 60, 2),
        "paid_days": full + 0.5 * cnt("Half Day") + paid_leave + cnt("Holiday") + cnt("Weekly Off"),
    }


def monthly(month: str, device: str | None = None) -> list[dict]:
    """Per-employee summary for `month` (YYYY-MM)."""
    return [_summary(e, sh, res) for e, sh, res in _month_results(month, device)]


def grid(month: str, device: str | None = None) -> dict:
    """Attendance sheet: one row per employee, one code per day, plus the month's totals."""
    days, _, _ = _month_days(month)
    rows = []
    for e, sh, res in _month_results(month, device):
        t = _summary(e, sh, res)
        rows.append({"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
                     "designation": e["designation"], "father_name": e["father_name"],
                     "codes": [CODES[r["status"]] for r in res],
                     "totals": {"worked": t["days_present"], "weekly_offs": t["weekly_offs"], "holidays": t["holidays"],
                                "leave": t["leave"], "absent": t["absent"], "paid_days": t["paid_days"]}})
    return {"days": [int(d[8:]) for d in days], "rows": rows}


def muster(month: str, device: str | None = None) -> list[dict]:
    """Muster-roll style register: one row per employee, a code per day, then the statutory totals."""
    g = grid(month, device)
    out = []
    for i, r in enumerate(g["rows"], 1):
        row = {"Sr": i, "Code": r["device_user_id"], "Name": r["name"], "Father's Name": r["father_name"],
               "Designation": r["designation"], "Department": r["department"]}
        row.update({f"{d:02d}": c for d, c in zip(g["days"], r["codes"])})
        t = r["totals"]
        row.update({"Worked Days": t["worked"], "W.Days Off": t["weekly_offs"], "Holidays": t["holidays"],
                    "Leave": t["leave"], "Absent": t["absent"], "Total Salary Days": t["paid_days"]})
        out.append(row)
    return out


def timecard(month: str, employee_id: str, device: str | None = None) -> list[dict]:
    """One employee's day-by-day card for the month."""
    shifts, emps, holidays, leaves, changes = _load()
    e = next((x for x in emps if x["device_user_id"] == employee_id), None)
    if not e:
        return []
    days, d1, d2 = _month_days(month)
    pm, today = _punch_map(d1, d2, device), now_local().date().isoformat()
    rows = []
    for d in days:
        sh = shift_on(e, d, shifts, changes)
        offs = [o.strip().upper() for o in (sh["weekly_off"] if sh else config.WEEKLY_OFF).split(",") if o.strip()]
        r = evaluate(e, sh, d, pm.get((employee_id, d)), holidays, leaves, today)
        wd = WEEKDAYS[Date.fromisoformat(d).weekday()]
        rows.append({"date": d, "weekday": wd.title(),
                     "day_type": "HOLIDAY" if d in holidays else "WEEKLY OFF" if wd in offs else "WORKDAY",
                     "schedule": f"{sh['start']}-{sh['end']}" if sh else "flexible",
                     "in": r["first_in"], "out": r["last_out"], "hours": r["hours"], "late_min": r["late_min"],
                     "early_min": r["early_min"], "overtime_min": r["ot_min"], "status": r["status"], "note": r["note"]})
    return rows


def _adjustments(month: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in db.conn().execute("SELECT * FROM payroll_adjustments WHERE month=? ORDER BY id", (month,)):
        out.setdefault(r["device_user_id"], []).append(dict(r))
    return out


def payroll(month: str, device: str | None = None) -> list[dict]:
    """Simple pay sheet: monthly salary / calendar days x paid days, plus any manual adjustments (bonus, advance...).
    No tax, PF/ESI or allowances."""
    days, _, d2 = _month_days(month)
    complete = d2 < now_local().date().isoformat()
    sal = {r["device_user_id"]: r["salary"] for r in db.conn().execute("SELECT device_user_id, salary FROM employees")}
    adj = _adjustments(month)
    out = []
    for r in monthly(month, device):
        base = sal.get(r["device_user_id"], 0) or 0
        extra = round(sum(a["amount"] for a in adj.get(r["device_user_id"], [])), 2)
        out.append({"device_user_id": r["device_user_id"], "name": r["name"], "department": r["department"],
                    "monthly_salary": base, "days_in_month": len(days), "paid_days": r["paid_days"],
                    "unpaid_days": round(len(days) - r["paid_days"], 2) if complete else "",
                    "adjustments": extra, "net_pay": round(base / len(days) * r["paid_days"] + extra, 2),
                    "month_complete": complete})
    return out


def payslip(month: str, uid: str) -> dict | None:
    """Everything a payslip shows for one employee and month (None if the employee has no payroll row)."""
    row = next((r for r in payroll(month) if r["device_user_id"] == uid), None)
    summ = next((r for r in monthly(month) if r["device_user_id"] == uid), None)
    emp = db.conn().execute("SELECT * FROM employees WHERE device_user_id=?", (uid,)).fetchone()
    if not row or not summ or not emp:
        return None
    base, n = row["monthly_salary"], row["days_in_month"]
    adjs = _adjustments(month).get(uid, [])
    earnings = [("Salary for the month", round(base, 2))] + [(a["label"], round(a["amount"], 2)) for a in adjs if a["amount"] > 0]
    lop = round(base - base / n * row["paid_days"], 2)
    deductions = ([(f"Loss of pay ({round(n - row['paid_days'], 2):g} unpaid days)", lop)] if lop > 0 else []) + \
                 [(a["label"], round(-a["amount"], 2)) for a in adjs if a["amount"] < 0]
    return {
        "company": config.COMPANY_NAME, "address": config.COMPANY_ADDRESS, "month": month,
        "employee": {"code": uid, "name": emp["name"], "department": emp["department"], "designation": emp["designation"],
                     "branch": emp["branch"], "joined": emp["joined"] or ""},
        "attendance": {"days_in_month": n, "paid_days": row["paid_days"], "present": summ["days_present"], "late_days": summ["late_days"],
                       "half_days": summ["half_days"], "absent": summ["absent"], "leave": summ["leave"],
                       "holidays": summ["holidays"], "weekly_offs": summ["weekly_offs"], "overtime_hours": summ["overtime_hours"]},
        "earnings": earnings, "deductions": deductions,
        "gross": round(sum(a for _, a in earnings), 2), "total_deductions": round(sum(a for _, a in deductions), 2),
        "net_pay": row["net_pay"],
    }
