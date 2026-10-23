"""Attendance engine + reports. Every employee has their own shift (or none = flexible: never late)."""
import calendar
from datetime import date as Date, timedelta
from datetime import datetime

from . import access, config, db, loans
from .timeutil import now_local

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WORKED = {"Present", "Late", "Half Day"}
CODES = {"Present": "P", "Late": "L", "Half Day": "HD", "Absent": "A", "Leave": "LV", "Holiday": "H", "Weekly Off": "WO", "": ""}
LEAVE_CODES = {"Informed": "IL", "Adhoc": "AL"}   # a leave day on the attendance sheet: Informed (yellow) / Adhoc (red)


# Excel colours (also used on screen): Informed yellow, Adhoc red, approved work on a weekly off blue
XLSX_FILLS = {"Informed": "FFEB9C", "Adhoc": "FFC7CE", "WeekOff": "DDEBF7"}
XLSX_LEGEND = [("Informed", "Yellow = Informed leave (IL)"), ("Adhoc", "Red = Adhoc leave (AL)"),
               ("WeekOff", "Blue = approved work on a weekly off (rostered)")]
LEAVE_COLUMNS = {"Informed": ("informed_leave", "Informed Leave"), "Adhoc": ("adhoc_leave", "Adhoc Leave")}


def day_code(r: dict) -> str:
    if r.get("in_progress"):
        return "IN"
    return LEAVE_CODES.get(r.get("leave_type"), CODES["Leave"]) if r["status"] == "Leave" else CODES[r["status"]]
LEAVE_TYPES = ["Informed", "Adhoc"]   # Informed (yellow) and Adhoc (red) follow the leave policy
WFH_PAY = 0.8   # a work-from-home day is paid at 0.8 of a day (leave policy)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _load(scoped: bool = True):
    c = db.conn()
    shifts = {r["id"]: dict(r) for r in c.execute("SELECT * FROM shifts")}
    frag, args = access.scope_sql("department") if scoped else ("", [])
    # resigned people with a last working day stay in the months they worked; days outside [count_from, left_on] are blank
    emps = [dict(r) for r in c.execute("SELECT * FROM employees WHERE (active=1 OR left_on IS NOT NULL)" + frag + " ORDER BY name", args)]
    started = c.execute("SELECT MIN(d) FROM (SELECT MIN(date(ts)) d FROM punches UNION ALL "
                        "SELECT MIN(start_date) FROM leaves WHERE status='approved')").fetchone()[0] or ""
    wfh: dict[str, set] = {}   # approved work from home: paid at 0.8 (WFH_PAY), with or without punches
    for r in c.execute("SELECT device_user_id, start_date, end_date FROM wfh_requests WHERE status='approved'"):
        x, end = Date.fromisoformat(r["start_date"]), Date.fromisoformat(r["end_date"])
        while x <= end:
            wfh.setdefault(r["device_user_id"], set()).add(x.isoformat())
            x += timedelta(days=1)
    for e in emps:   # nothing is counted before the first day this system has any attendance for
        e["count_from"] = max(e.get("joined") or "", started)
        e["_wfh"] = wfh.get(e["device_user_id"], set())
    holidays = {r["date"]: r["name"] for r in c.execute("SELECT * FROM holidays WHERE optional=0")}
    leaves = [dict(r) for r in c.execute("SELECT * FROM leaves WHERE status='approved'")]
    changes: dict = {}
    for r in c.execute("SELECT * FROM shift_changes ORDER BY id"):
        changes.setdefault(r["device_user_id"], []).append(dict(r))
    for r in c.execute("SELECT * FROM roster"):   # roster days sit in the same dict under ("R", id, date) keys
        changes[("R", r["device_user_id"], r["date"])] = dict(r)
    return shifts, emps, holidays, leaves, changes


ALL_DAYS = ",".join(WEEKDAYS)
OFF_SHIFT = {"id": None, "name": "Off (roster)", "start": "00:00", "end": "00:00", "grace_min": 0, "half_day_hours": 0,
             "ot_after_min": 0, "weekly_off": ALL_DAYS, "break_start": "", "break_end": ""}


def combine(parts: list[dict]) -> dict:
    """Several shifts on one day (e.g. 10:00-14:00 and 19:00-21:00) worked as one day: it starts with the first shift
    (late and grace from it) and ends with the last (early leaving and overtime from it). Time between the shifts is not
    counted as worked, and less than half the scheduled time makes a half day."""
    parts = sorted(parts, key=lambda s: s["start"])
    windows: list[list[str]] = []
    for s in parts:   # overlapping shifts merge into one window
        if windows and s["start"] <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], s["end"])
        else:
            windows.append([s["start"], s["end"]])
    last = max(parts, key=lambda s: s["end"])
    scheduled = sum(_hm(b) - _hm(a) for a, b in windows)
    return {**parts[0], "name": " + ".join(s["name"] for s in parts), "start": parts[0]["start"], "end": last["end"],
            "ot_after_min": last["ot_after_min"], "half_day_hours": round(scheduled / 120, 2), "weekly_off": "",
            "break_start": "", "break_end": "", "windows": [tuple(w) for w in windows]}


def shift_on(e: dict, d: str, shifts: dict, changes: dict) -> dict | None:
    """The shift an employee works on date `d`: their roster entry for that day, else the latest date-range shift change
    that covers it, else their normal shift. A roster day is a working day even if the shift's weekly off falls on it,
    and a roster 'off' day is a day off whatever the shift says."""
    r = changes.get(("R", e["device_user_id"], d))
    if r:
        if r["off"]:
            base = shifts.get(e["shift_id"])
            return {**base, "weekly_off": ALL_DAYS} if base else OFF_SHIFT
        parts = [shifts[i] for i in [r["shift_id"]] + [int(x) for x in (r.get("extra_shifts") or "").split(",") if x] if i in shifts]
        if parts:
            # rostered on what is normally their weekly off: approved work on a week off (shown in blue)
            base = _planned_shift(e, d, shifts, changes)
            offs = [o.strip().upper() for o in (base["weekly_off"] if base else config.WEEKLY_OFF).split(",") if o.strip()]
            duty = WEEKDAYS[Date.fromisoformat(d).weekday()] in offs
            return {**(combine(parts) if len(parts) > 1 else {**parts[0], "weekly_off": ""}), "week_off_duty": duty}
    return _planned_shift(e, d, shifts, changes)


def _planned_shift(e: dict, d: str, shifts: dict, changes: dict) -> dict | None:
    """The shift without the roster: the latest date-range shift change covering d, else the normal shift."""
    for ch in reversed(changes.get(e["device_user_id"], [])):
        if ch["start_date"] <= d <= ch["end_date"]:
            return shifts.get(ch["shift_id"])
    return shifts.get(e["shift_id"])


def _punch_map(d1: str, d2: str, device: str | None) -> dict:
    rows = db.conn().execute(
        "SELECT device_user_id u, date(ts) d, MIN(ts) a, MAX(ts) b, COUNT(*) n, SUM(COALESCE(note,'')='WFH') w FROM punches "
        "WHERE date(ts) BETWEEN ? AND ? AND COALESCE(flag,'')!='rejected' AND (? IS NULL OR source=?) GROUP BY u, d",
        (d1, d2, device, device))
    return {(r["u"], r["d"]): r for r in rows}


def evaluate(emp: dict, shift: dict | None, d: str, p, holidays: dict, leaves: list, today: str) -> dict:
    """Status and numbers for one employee on one day. `p` is the day's punch summary row or None."""
    wd = WEEKDAYS[Date.fromisoformat(d).weekday()]
    offs = [o.strip().upper() for o in (shift["weekly_off"] if shift else config.WEEKLY_OFF).split(",") if o.strip()]
    off = wd in offs
    out = {"date": d, "status": "", "first_in": "", "last_out": "", "hours": 0.0, "punches": 0,
           "late_min": 0, "early_min": 0, "early_in_min": 0, "ot_min": 0, "note": ""}
    leave = next((l for l in leaves if l["device_user_id"] == emp["device_user_id"] and l["start_date"] <= d <= l["end_date"]), None)
    half = leave if leave and leave.get("half") and not off and d not in holidays else None

    def half_leave():   # half-day leave: the other half is paid from the leave balance (or cut if beyond it)
        out["half_leave"] = 0 if half["type"] == "Unpaid" else 0.5
        out["leave_type"] = half["type"]
        if half.get("lop"):
            out["lop"] = half["lop"]
        out["note"] = (out["note"] + "; " if out["note"] else "") + f"half-day {half['type']} leave" + \
            (f" ({half['lop']:g}x pay cut)" if half.get("lop") else "")
    if p:
        first, last, n = datetime.fromisoformat(p["a"]), datetime.fromisoformat(p["b"]), p["n"]
        fm, lm = first.hour * 60 + first.minute, last.hour * 60 + last.minute
        working = bool(shift) and not off and d not in holidays
        # A single punch still counts as present. If it falls in the second half of the shift it was the out punch.
        single_out = n == 1 and working and fm >= (_hm(shift["start"]) + _hm(shift["end"])) / 2
        if n > 1:
            out.update(first_in=p["a"][11:19], last_out=p["b"][11:19], punches=n)
            worked = (last - first).total_seconds() / 60   # breaks are paid, so not taken off
            for (_, a), (b, _) in zip((shift or {}).get("windows", []), (shift or {}).get("windows", [])[1:]):
                worked -= max(0, min(lm, _hm(b)) - max(fm, _hm(a)))   # the gap between two shifts is not work
            out["hours"] = round(worked / 60, 2)
        elif single_out:
            out.update(last_out=p["b"][11:19], punches=n, note="missing in punch")
        else:
            out.update(first_in=p["a"][11:19], punches=n, note="missing out punch")
        status = "Present"
        if working:
            start, end = _hm(shift["start"]), _hm(shift["end"])
            if not single_out and fm > start + shift["grace_min"]:
                out["late_min"], status = fm - start, "Late"
            elif not single_out and fm < start:
                out["early_in_min"] = start - fm      # came in before the shift started
            if n > 1 or single_out:
                if lm < end:
                    out["early_min"] = end - lm
                if lm - end >= shift["ot_after_min"]:
                    out["ot_min"] = lm - end
            if n > 1 and out["hours"] < shift["half_day_hours"]:
                status = "Half Day"
        else:  # came in on a holiday / weekly off: all worked time counts as overtime
            out["ot_min"] = int(out["hours"] * 60)
            kind = "holiday" if d in holidays else "weekly off"
            out["note"] = (out["note"] + "; " if out["note"] else "") + f"worked on {kind}"
        if shift and shift.get("week_off_duty"):
            out["week_off_duty"] = True
            out["note"] = (out["note"] + "; " if out["note"] else "") + "rostered on weekly off"
        if dict(p).get("w"):   # checked in from home in the app; a punch at the office on a WFH day is a full day
            out["wfh"] = True
            out["note"] = "WFH" + ("; " + out["note"] if out["note"] else "")
        if half:            # worked the other half: a half day, without late / early marks
            status, out["late_min"], out["early_min"] = "Half Day", 0, 0
            half_leave()
        if d == today and n == 1 and not single_out:
            # still at work: shown (and late is flagged) but not counted in any total until they punch out
            out["in_progress"] = True
            out["note"] = out["note"].replace("missing out punch", "at work, no punch-out yet")
        out["status"] = status
        return out
    if d > today or d < (emp.get("joined") or "") or (emp.get("left_on") and d > emp["left_on"]):
        return out  # in the future, before joining, or after leaving: not counted
    if d < (emp.get("count_from") or ""):   # before the system has any attendance: weekly offs and holidays are still
        if d in holidays:                   # paid, but working days are not marked absent
            out["status"], out["note"] = "Holiday", holidays[d]
        elif off:
            out["status"] = "Weekly Off"
        return out
    if d in holidays:     # holidays and weekly offs inside a leave stay holidays / weekly offs (only working days are leave)
        out["status"], out["note"] = "Holiday", holidays[d]
    elif off:
        out["status"] = "Weekly Off"
    elif half:              # half-day leave but no punch: the worked half is absent
        out["status"] = "Absent"
        half_leave()
    elif leave:
        out["status"], out["note"], out["leave_type"] = "Leave", leave["type"], leave["type"]
        if leave.get("lop"):     # beyond the balance: still a leave day, but salary is cut lop x a day
            out["lop"], out["note"] = leave["lop"], f"{leave['type']} beyond balance ({leave['lop']:g}x pay cut)"
    elif d in emp.get("_wfh", ()):   # approved work from home without punches: worked, paid at 0.8
        out["status"], out["wfh"], out["note"] = "Present", True, "WFH (approved, no punches)"
    else:
        out["status"] = "Absent"
    return out


def mark_absences(e: dict, res: list[dict], shifts: dict, holidays: dict, changes: dict, cache: dict | None = None):
    """Leave policy: a past working day absent without approved leave (none applied, pending or rejected) stays
    'Absent' but counts as Adhoc leave: paid from an Adhoc credit, or beyond the balance a 2 x day's salary cut."""
    from . import leavepolicy
    cache = {} if cache is None else cache
    today = now_local().date().isoformat()
    for r in res:
        if r["status"] != "Absent" or r["date"] >= today or "half_leave" in r:
            continue
        y = leavepolicy.fy(Date.fromisoformat(r["date"]))
        key = (e["device_user_id"], y)
        if key not in cache:
            cache[key] = leavepolicy.absences(e, (e, shifts, holidays, changes), y)
        lop = cache[key].get(r["date"])
        if lop is None:
            continue
        r["absent_adhoc"] = True
        if lop:
            r["lop"] = lop
        r["note"] = "no approved leave: counted as Adhoc" + (f" beyond balance ({lop:g}x pay cut)" if lop else " (paid from the Adhoc balance)")
    return res


def in_period(e: dict, d1: str, d2: str) -> bool:
    """Active people always; a resigned person only for periods that start on or before their last working day."""
    return bool(e["active"]) or (bool(e.get("left_on")) and d1 <= e["left_on"])


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
        if not in_period(e, date, date):
            continue
        sh = shift_on(e, date, shifts, changes)
        r = evaluate(e, sh, date, pm.get((e["device_user_id"], date)), holidays, leaves, today)
        mark_absences(e, [r], shifts, holidays, changes)
        r.update(device_user_id=e["device_user_id"], name=e["name"], department=e["department"],
                 designation=e["designation"], branch=e["branch"], shift=sh["name"] if sh else "")
        rows.append(r)
    return rows


def _month_results(month: str, device: str | None):
    shifts, emps, holidays, leaves, changes = _load()
    days, d1, d2 = _month_days(month)
    pm, today = _punch_map(d1, d2, device), now_local().date().isoformat()
    for e in emps:
        if not in_period(e, d1, d2):
            continue
        yield e, shifts.get(e["shift_id"]), mark_absences(e, [
            evaluate(e, shift_on(e, d, shifts, changes), d, pm.get((e["device_user_id"], d)), holidays, leaves, today) for d in days],
            shifts, holidays, changes)


def leave_days(res: list[dict], kind: str) -> float:
    """Leave days of one kind (Informed / Adhoc) in a list of evaluated days; half-day leave counts 0.5."""
    return sum(0.5 if "half_leave" in r else 1 for r in res if r.get("leave_type") == kind)


def _summary(e: dict, sh: dict | None, res: list[dict]) -> dict:
    res = [r for r in res if not r.get("in_progress")]   # today counts once the person has punched out
    cnt = lambda st: sum(r["status"] == st for r in res)
    paid_leave = sum(r["status"] == "Leave" and r["note"] != "Unpaid" for r in res)
    full = cnt("Present") + cnt("Late")
    wfh_cut = sum((1 if r["status"] != "Half Day" else 0.5) * (1 - WFH_PAY) for r in res if r.get("wfh"))
    return {
        "device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
        "designation": e["designation"], "branch": e["branch"], "shift": sh["name"] if sh else "",
        "days_present": full + cnt("Half Day"), "late_days": cnt("Late"), "half_days": cnt("Half Day"),
        "absent": cnt("Absent"), "leave": cnt("Leave") + 0.5 * sum(1 for r in res if "half_leave" in r), "holidays": cnt("Holiday"), "weekly_offs": cnt("Weekly Off"),
        "informed_leave": leave_days(res, "Informed"), "adhoc_leave": leave_days(res, "Adhoc"),
        "absent_as_adhoc": sum(1 for r in res if r.get("absent_adhoc")),
        "total_hours": round(sum(r["hours"] for r in res), 2),
        "late_minutes": sum(r["late_min"] for r in res), "early_minutes": sum(r["early_min"] for r in res),
        "overtime_hours": round(sum(r["ot_min"] for r in res) / 60, 2),
        "wfh_days": sum(1 for r in res if r.get("wfh")),
        "leave_cut_days": sum(r.get("lop", 0) for r in res),   # days' salary cut for leave beyond the balance
        "paid_days": round(full + 0.5 * cnt("Half Day") + paid_leave + cnt("Holiday") + cnt("Weekly Off") - wfh_cut
                           + sum(1 for r in res if r.get("absent_adhoc"))   # absences counted as Adhoc are paid (then cut beyond balance)
                           + sum(r.get("half_leave", 0) for r in res), 2),
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
                     "codes": [day_code(r) for r in res],
                     "totals": {"worked": t["days_present"], "weekly_offs": t["weekly_offs"], "holidays": t["holidays"],
                                "informed_leave": t["informed_leave"], "adhoc_leave": t["adhoc_leave"],
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
                    "Informed Leave": t["informed_leave"], "Adhoc Leave": t["adhoc_leave"],
                    "Absent": t["absent"], "Total Salary Days": t["paid_days"]})
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
    rows, cache = [], {}
    for d in days:
        sh = shift_on(e, d, shifts, changes)
        offs = [o.strip().upper() for o in (sh["weekly_off"] if sh else config.WEEKLY_OFF).split(",") if o.strip()]
        r = evaluate(e, sh, d, pm.get((employee_id, d)), holidays, leaves, today)
        mark_absences(e, [r], shifts, holidays, changes, cache)
        wd = WEEKDAYS[Date.fromisoformat(d).weekday()]
        rows.append({"date": d, "weekday": wd.title(),
                     "day_type": "HOLIDAY" if d in holidays else "WEEKLY OFF" if wd in offs else "WORKDAY",
                     "schedule": ", ".join(f"{a}-{b}" for a, b in sh.get("windows", [(sh["start"], sh["end"])])) if sh else "flexible",
                     "in": r["first_in"], "out": r["last_out"], "hours": r["hours"], "late_min": r["late_min"],
                     "early_min": r["early_min"], "overtime_min": r["ot_min"], "status": r["status"], "note": r["note"],
                     "code": perf_code(r) if d <= today else ""})
    return rows


MAX_TIMECARD_DAYS = 366
TIMECARD_STATUSES = ("Present", "Late", "Half Day", "Absent", "Leave", "Informed", "Adhoc", "Holiday", "Weekly Off",
                     "Week-off duty", "Missing punch")


def timecards(d1: str, d2: str, employee_id: str | None = None, department: str | None = None,
              status: str | None = None) -> list[dict]:
    """Time card rows for everyone (or one person) from d1 to d2, sorted by date and then name. Days before someone
    joined, after they left, and in the future are left out; a missing punch stays blank (never invented)."""
    shifts, emps, holidays, leaves, changes = _load()
    pm, today = _punch_map(d1, d2, None), now_local().date().isoformat()
    cache: dict = {}
    days, x = [], Date.fromisoformat(d1)
    while x.isoformat() <= d2:
        days.append(x.isoformat())
        x += timedelta(days=1)
    rows = []
    for e in emps:
        if (employee_id and e["device_user_id"] != employee_id) or (department and e["department"] != department) \
                or not in_period(e, d1, d2):
            continue
        for d in days:
            if d > today or d < (e.get("joined") or "") or (e.get("left_on") and d > e["left_on"]):
                continue
            sh = shift_on(e, d, shifts, changes)
            r = evaluate(e, sh, d, pm.get((e["device_user_id"], d)), holidays, leaves, today)
            mark_absences(e, [r], shifts, holidays, changes, cache)
            wd = WEEKDAYS[Date.fromisoformat(d).weekday()]
            offs = [o.strip().upper() for o in (sh["weekly_off"] if sh else config.WEEKLY_OFF).split(",") if o.strip()]
            wins = sh.get("windows", [(sh["start"], sh["end"])]) if sh else []
            rows.append({"name": e["name"], "device_user_id": e["device_user_id"], "department": e["department"],
                         "date": d, "weekday": wd.title(),
                         "day_type": "HOLIDAY" if d in holidays else "WEEKLY OFF" if wd in offs else "WORKDAY",
                         "scheduled_in": wins[0][0] if wins else "", "scheduled_out": wins[-1][1] if wins else "",
                         "schedule": ", ".join(f"{a}-{b}" for a, b in wins) if wins else "flexible",
                         "in": r["first_in"][:5], "out": r["last_out"][:5], "late_min": r["late_min"],
                         "early_in_min": r["early_in_min"], "early_min": r["early_min"], "hours": r["hours"],
                         "overtime_min": r["ot_min"], "status": r["status"], "leave_type": r.get("leave_type", ""),
                         "week_off_duty": bool(r.get("week_off_duty")), "absent_adhoc": bool(r.get("absent_adhoc")),
                         "note": r["note"]})
    if status:
        keep = {"Informed": lambda r: r["leave_type"] == "Informed", "Adhoc": lambda r: r["leave_type"] == "Adhoc",
                "Week-off duty": lambda r: r["week_off_duty"],
                "Missing punch": lambda r: "missing" in r["note"]}.get(status, lambda r: r["status"] == status)
        rows = [r for r in rows if keep(r)]
    return sorted(rows, key=lambda r: (r["date"], r["name"].lower(), r["device_user_id"]))


PERF_MAX_DAYS = 31


def perf_code(r: dict) -> str:
    """Status code in the style of the old machine software's Monthly Performance Report."""
    if r.get("in_progress"):
        return "IN"
    st, lv = r["status"], LEAVE_CODES.get(r.get("leave_type", ""), "LV")
    if st == "Half Day":
        return "P/2-" + lv if "half_leave" in r else "P/2"
    if r.get("week_off_duty") and st in ("Present", "Late"):
        return "POW"
    return {"Present": "P", "Late": "P-LT", "Absent": "A", "Leave": lv, "Holiday": "HL", "Weekly Off": "WO"}.get(st, "")


def performance(d1: str, d2: str, employee_id: str | None = None, department: str | None = None) -> list[dict]:
    """Monthly Performance Report: per employee a summary line (present, holidays, weekly offs, absent, leave, paid
    days, early going, late, work and overtime hours) and every day's arrival, departure, hours, status and shift.
    Sorted by branch, department and name. Days in the future, before joining or after leaving are blank."""
    shifts, emps, holidays, leaves, changes = _load()
    pm, today = _punch_map(d1, d2, None), now_local().date().isoformat()
    cache: dict = {}
    days, x = [], Date.fromisoformat(d1)
    while x.isoformat() <= d2:
        days.append(x.isoformat())
        x += timedelta(days=1)
    out = []
    for e in emps:
        if (employee_id and e["device_user_id"] != employee_id) or (department and e["department"] != department) \
                or not in_period(e, d1, d2):
            continue
        res, per_day = [], {}
        for d in days:
            if d > today or d < (e.get("joined") or "") or (e.get("left_on") and d > e["left_on"]):
                continue
            sh = shift_on(e, d, shifts, changes)
            r = evaluate(e, sh, d, pm.get((e["device_user_id"], d)), holidays, leaves, today)
            mark_absences(e, [r], shifts, holidays, changes, cache)
            res.append(r)
            per_day[d] = {"in": r["first_in"][:5], "out": r["last_out"][:5], "hours_min": round(r["hours"] * 60),
                          "ot_min": r["ot_min"], "code": perf_code(r), "shift": sh["name"] if sh else "",
                          "leave_type": r.get("leave_type", ""), "absent_adhoc": bool(r.get("absent_adhoc")),
                          "week_off_duty": bool(r.get("week_off_duty"))}
        summ = _summary(e, None, res)
        halves = sum(1 for r in res if r["status"] == "Half Day")
        out.append({"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"] or "",
                    "designation": e["designation"] or "", "branch": e["branch"] or "", "days": per_day,
                    "present": summ["days_present"] - 0.5 * halves, "holidays": summ["holidays"],
                    "weekly_offs": summ["weekly_offs"], "absent": summ["absent"], "leave": summ["leave"],
                    "paid_days": summ["paid_days"], "early_min": summ["early_minutes"], "late_min": summ["late_minutes"],
                    "work_min": round(summ["total_hours"] * 60), "ot_min": sum(r["ot_min"] for r in res)})
    return sorted(out, key=lambda x: (x["branch"].lower(), x["department"].lower(), x["name"].lower(), x["device_user_id"]))


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
    adj, emi = _adjustments(month), loans.deductions(month)
    out = []
    for r in monthly(month, device):
        base = sal.get(r["device_user_id"], 0) or 0
        extra = round(sum(a["amount"] for a in adj.get(r["device_user_id"], [])), 2)
        loan_cut = round(sum(x["amount"] for x in emi.get(r["device_user_id"], [])), 2)
        earned = round(base / len(days) * r["paid_days"] + extra - loan_cut, 2)
        # the leave-policy cut never takes pay below zero
        leave_cut = min(round(base / len(days) * r["leave_cut_days"], 2), max(earned, 0))
        out.append({"device_user_id": r["device_user_id"], "name": r["name"], "department": r["department"],
                    "monthly_salary": base, "days_in_month": len(days),
                    "informed_leave": r["informed_leave"], "adhoc_leave": r["adhoc_leave"], "paid_days": r["paid_days"],
                    "unpaid_days": round(len(days) - r["paid_days"], 2) if complete else "",
                    "adjustments": extra, "loan_deduction": loan_cut, "leave_cut": leave_cut,
                    "net_pay": round(earned - leave_cut, 2),
                    "month_complete": complete, "payslip_ready": payslip_ready(month, r["device_user_id"])})
    return out


def payslip_ready(month: str, uid: str) -> bool:
    """Payslips only for a finished month, or for someone whose last working day (in that month) has passed."""
    today = now_local().date().isoformat()
    if _month_days(month)[2] < today:
        return True
    row = db.conn().execute("SELECT active, left_on FROM employees WHERE device_user_id=?", (uid,)).fetchone()
    return bool(row and not row["active"] and row["left_on"] and row["left_on"][:7] == month and row["left_on"] < today)


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
                 ([(f"Leave beyond balance ({summ['leave_cut_days']:g} days' salary)", row["leave_cut"])] if row["leave_cut"] else []) + \
                 [(a["label"], round(-a["amount"], 2)) for a in adjs if a["amount"] < 0] + \
                 [(x["label"], x["amount"]) for x in loans.deductions(month).get(uid, [])]
    return {
        "company": config.COMPANY_NAME, "address": config.COMPANY_ADDRESS, "month": month,
        "employee": {"code": uid, "name": emp["name"], "department": emp["department"], "designation": emp["designation"],
                     "branch": emp["branch"], "joined": emp["joined"] or ""},
        "attendance": {"days_in_month": n, "paid_days": row["paid_days"], "present": summ["days_present"], "late_days": summ["late_days"],
                       "half_days": summ["half_days"], "absent": summ["absent"], "leave": summ["leave"],
                       "informed_leave": summ["informed_leave"], "adhoc_leave": summ["adhoc_leave"],
                       "holidays": summ["holidays"], "weekly_offs": summ["weekly_offs"], "overtime_hours": summ["overtime_hours"]},
        "earnings": earnings, "deductions": deductions,
        "gross": round(sum(a for _, a in earnings), 2), "total_deductions": round(sum(a for _, a in deductions), 2),
        "net_pay": row["net_pay"],
    }
