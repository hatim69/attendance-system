"""MTT leave policy (financial year April - March).

  * 24 paid leaves a year: 12 Casual, 4 Adhoc / Sick, 8 Festive.
  * Leave starts after 3 months of working (before that, days off are unpaid).
  * CASUAL: applied at least 7 days in advance; at most 2 a month and 12 a year. When it runs out, the Adhoc / Sick
    balance can be used instead (sick leave converts to casual). Beyond both, each day costs 1.5 x a day's salary.
  * ADHOC / SICK (emergency, uninformed, or less than 7 days' notice): 1 per quarter (Apr-Jun, Jul-Sep, Oct-Dec,
    Jan-Mar). Beyond the limit each day costs 2 x a day's salary and also uses 1 day of the Casual balance.
  * FESTIVE: only on a date from the office's festival list (holidays marked as festival / optional); 8 a year.
  * HALF DAY: half a day from the Casual (then Adhoc / Sick) balance, so the day is paid in full; with no balance,
    0.5 x 1.5 = 0.75 of a day's salary is cut. No notice needed.
  * A day's salary = monthly salary / calendar days in that month.

Only working days count (weekly offs and holidays inside a leave are free). Pending applications reserve balance;
rejected ones do not. Leave beyond the balance is recorded with a pay-cut multiplier (`lop`) that payroll applies."""
import calendar
from collections import Counter
from datetime import date, timedelta

from . import db, reports

WAIT_MONTHS = 3
CASUAL_PER_MONTH, CASUAL_PER_YEAR = 2, 12
ADHOC_PER_QUARTER = 1
FESTIVE_PER_YEAR = 8
ANNUAL_TOTAL = 24
NOTICE_DAYS = 7
CASUAL_EXCESS_CUT = 1.5    # x a day's salary for each Casual day beyond the balance
ADHOC_EXCESS_CUT = 2.0     # x a day's salary for each Adhoc day beyond the balance
BACKDATE_DAYS = 7          # employees applying in the app may start a leave at most this many days in the past
MAX_RANGE_DAYS = 31
POLICY_TYPES = ("Casual", "Adhoc", "Festive")
ALIASES = {"Normal": "Casual", "Sick": "Adhoc"}


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def eligible_from(joined: str | None) -> date | None:
    """First day on which paid leave can be taken, or None when the joining date is not recorded."""
    return add_months(date.fromisoformat(joined), WAIT_MONTHS) if joined else None


def fy(d: date) -> int:
    """Financial year that contains d, named by its starting year (2026 = Apr 2026 - Mar 2027)."""
    return d.year if d.month >= 4 else d.year - 1


def quarter(d: date) -> tuple[int, int, date, date]:
    """(fy, quarter 1-4, first day, last day) of the financial-year quarter containing d."""
    y = fy(d)
    q = ((d.month - 4) % 12) // 3 + 1
    first = date(y, 4, 1) if q == 1 else add_months(date(y, 4, 1), 3 * (q - 1))
    return y, q, first, add_months(first, 3) - timedelta(days=1)


def per_day_pay(salary: float, d: date) -> float:
    return round((salary or 0) / calendar.monthrange(d.year, d.month)[1], 2)


def festival_dates() -> dict[str, str]:
    return {r["date"]: r["name"] for r in db.conn().execute("SELECT date, name FROM holidays WHERE optional=1")}


def _context(uid: str):
    shifts, emps, holidays, _, changes = reports._load(scoped=False)
    emp = next((e for e in emps if e["device_user_id"] == uid), None)
    if emp is None:   # resigned without a last working day is not loaded by reports; leave still needs the person
        row = db.conn().execute("SELECT * FROM employees WHERE device_user_id=?", (uid,)).fetchone()
        emp = dict(row) if row else None
    return emp, shifts, holidays, changes


def working_days(emp: dict, start: date, end: date, ctx) -> list[date]:
    _, shifts, holidays, changes = ctx
    out, d = [], start
    while d <= end:
        iso = d.isoformat()
        sh = reports.shift_on(emp, iso, shifts, changes)
        offs = [o.strip().upper() for o in (sh["weekly_off"] if sh else reports.config.WEEKLY_OFF).split(",") if o.strip()]
        if reports.WEEKDAYS[d.weekday()] not in offs and iso not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def _keys(t: str, lop: float, d: date) -> list[tuple]:
    """Balance buckets one leave day of this type uses."""
    t = ALIASES.get(t, t)
    y, q, _, _ = quarter(d)
    casual = [("C-M", d.strftime("%Y-%m")), ("C-Y", fy(d))]
    if t == "Casual":
        return casual
    if t == "Adhoc":
        return casual if lop >= ADHOC_EXCESS_CUT else [("A-Q", (y, q)), ("A-Y", y)]   # beyond the limit: uses Casual
    if t == "Festive":
        return [("F-Y", fy(d))]
    return []


def usage(emp: dict, ctx) -> Counter:
    """Leave days taken or reserved (approved + pending), per balance bucket."""
    u: Counter = Counter()
    for r in db.conn().execute("SELECT * FROM leaves WHERE device_user_id=? AND status IN ('approved','pending')",
                               (emp["device_user_id"],)):
        t = ALIASES.get(r["type"], r["type"])
        if t not in POLICY_TYPES:
            continue
        s, e = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        days = [s] if t == "Festive" else working_days(emp, s, e, ctx)
        unit = 0.5 if r["half"] else 1
        lop = (r["lop"] or 0) / unit                 # per full day, so a half-day Adhoc beyond balance is still 2x
        for d in days:
            for k in _keys(t, lop, d):
                u[k] += unit
    return u


def left(u: Counter, kind: str, d: date) -> float:
    if kind == "Casual":
        return min(CASUAL_PER_MONTH - u[("C-M", d.strftime("%Y-%m"))], CASUAL_PER_YEAR - u[("C-Y", fy(d))])
    if kind == "Adhoc":
        y, q, _, _ = quarter(d)
        return ADHOC_PER_QUARTER - u[("A-Q", (y, q))]
    return FESTIVE_PER_YEAR - u[("F-Y", fy(d))]


def plan(uid: str, kind: str, start_s: str, end_s: str, applied_on: date, strict: bool = True, half: bool = False) -> dict:
    """Work out how an application would be taken. Each working day becomes Casual, Adhoc or Festive (paid), Casual /
    Adhoc beyond the balance (paid, then a pay cut of 1.5x / 2x a day's salary), or Unpaid (before leave starts).
    Returns the split, the pay cut, warnings to show, and whether the employee must sign a consent, or {"error": ...}.
    strict=True is for employees applying themselves (limits back-dating)."""
    kind = ALIASES.get(kind, kind)
    emp, *rest = ctx = _context(uid)
    if not emp:
        return {"error": "Employee not found"}
    if kind not in POLICY_TYPES:
        return {"error": "Leave type must be Casual, Adhoc or Festive"}
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        return {"error": "Choose the dates"}
    if end < start:
        return {"error": "The end date is before the start date"}
    if (end - start).days >= MAX_RANGE_DAYS:
        return {"error": f"A leave can cover at most {MAX_RANGE_DAYS} days"}
    if strict and start < applied_on - timedelta(days=BACKDATE_DAYS):
        return {"error": f"You can apply for leave that started at most {BACKDATE_DAYS} days ago. Ask HR to record older leave."}
    if half:
        if end != start:
            return {"error": "A half day is a single date"}
        if kind == "Festive":
            return {"error": "Festive leave is for a whole day"}
        kind = "Casual"   # half days use Casual, then Adhoc / Sick, with no notice needed
    if kind == "Festive":
        fest = festival_dates()
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        bad = [d for d in days if d.isoformat() not in fest]
        if bad:
            return {"error": f"{bad[0]:%d %b %Y} is not on the festival holiday list. Festive leave can only be taken "
                             "on those dates."}
    else:
        days = working_days(emp, start, end, ctx)
    if not days:
        return {"error": "There are no working days in that range (weekly offs and holidays are not counted)"}
    elig = eligible_from(emp.get("joined"))
    notice_ok = half or (start - applied_on).days >= NOTICE_DAYS
    unit = 0.5 if half else 1
    u = usage(emp, ctx)
    alloc = []   # (day, type, lop per full day)
    for d in days:
        if elig and d < elig:
            t, lop = "Unpaid", 0.0
        elif kind == "Festive":
            if left(u, "Festive", d) < 1:
                return {"error": f"All {FESTIVE_PER_YEAR} festive leaves of this year are used. Apply for Casual leave instead."}
            t, lop = "Festive", 0.0
        elif kind == "Casual" and notice_ok and left(u, "Casual", d) >= unit:
            t, lop = "Casual", 0.0
        elif left(u, "Adhoc", d) >= unit:
            t, lop = "Adhoc", 0.0
        elif kind == "Casual" and notice_ok:
            t, lop = "Casual", CASUAL_EXCESS_CUT
        else:
            t, lop = "Adhoc", ADHOC_EXCESS_CUT
        for k in _keys(t, lop, d):
            u[k] += unit
        alloc.append((d, t, lop))

    salary = emp.get("salary") or 0
    cnt = lambda t, ex=False: sum(1 for _, x, lop in alloc if x == t and (lop > 0) == ex)
    unpaid = [d for d, t, _ in alloc if t == "Unpaid"]
    excess = [(d, lop) for d, t, lop in alloc if lop > 0]
    cut_unpaid = round(sum(per_day_pay(salary, d) * unit for d in unpaid), 2)
    cut_excess = round(sum(per_day_pay(salary, d) * lop * unit for d, lop in excess), 2)
    deduction = round(cut_unpaid + cut_excess, 2)
    adhoc_left = max(left(u, "Adhoc", days[0]), 0)
    rs = lambda v: f"Rs {v:,.2f}"

    warnings = []
    if unpaid:
        warnings.append(f"Paid leave starts on {elig:%d %b %Y} (after 3 months of working), so {len(unpaid)} day(s) "
                        f"before that are unpaid" + (f": {rs(cut_unpaid)} less salary." if salary else "."))
    converted = cnt("Adhoc")
    if kind == "Casual" and not notice_ok and (converted or cnt("Adhoc", True)):
        warnings.append(f"Casual leave needs {NOTICE_DAYS} days' notice, so this counts as Adhoc (emergency) leave. "
                        f"Adhoc left this quarter after this: {adhoc_left:g}.")
    elif kind == "Casual" and converted:
        warnings.append(f"Your Casual balance is not enough, so {converted} day(s) will use your Adhoc / Sick leave. "
                        f"Adhoc left this quarter after this: {adhoc_left:g}.")
    if half:
        warnings.insert(0, "Half day: half of the day comes from your leave balance, so the day is paid in full"
                        + (" (not this time: see below)." if excess or unpaid else "."))
    if excess and half:
        warnings.append("You don't have enough leave balance for the half day: 0.5 x 1.5 = 0.75 of a day's salary"
                        + (f" ({rs(cut_excess)})" if salary else "") + " will be cut.")
    elif excess:
        n15, n2 = cnt("Casual", True), cnt("Adhoc", True)
        bits = ([f"{n15} Casual day(s) x 1.5 day's salary"] if n15 else []) + \
               ([f"{n2} Adhoc day(s) x 2 days' salary (each also uses 1 Casual day)"] if n2 else [])
        warnings.append("You don't have enough leave balance. Beyond it: " + " and ".join(bits) +
                        (f" = {rs(cut_excess)} will be cut from your salary." if salary else " will be cut from your salary."))
    consent = ""
    if unpaid or excess:
        per = per_day_pay(salary, days[0])
        consent = (f"I, {emp['name']} (employee ID {emp['device_user_id']}), am applying for leave from {start:%d %b %Y} to "
                   f"{end:%d %b %Y} ({len(days)} working day(s)). I understand that my leave balance is not enough"
                   + (f" and that {rs(deduction)} will be cut from my salary (a day's salary is about {rs(per)})" if salary
                      else " and that salary will be cut for these days")
                   + ", as per the MTT leave policy: unpaid days before 3 months of service, 1.5x a day's salary for each "
                     "Casual day and 2x for each Adhoc day beyond the balance. I agree to this deduction.")
    # consecutive calendar days of the same kind become one leave record (weekends / holidays split records)
    parts: list[dict] = []
    for d, t, lop in alloc:
        if parts and (parts[-1]["type"], parts[-1]["lop"]) == (t, lop) and \
                date.fromisoformat(parts[-1]["end_date"]) + timedelta(days=1) == d:
            parts[-1]["end_date"] = d.isoformat()
        else:
            parts.append({"type": t, "lop": round(lop * unit, 2), "half": int(half),
                          "start_date": d.isoformat(), "end_date": d.isoformat()})
    return {"days": [d.isoformat() for d in days], "working_days": len(days) * unit, "half": half,
            "casual_days": cnt("Casual"), "adhoc_days": cnt("Adhoc"), "festive_days": cnt("Festive"),
            "excess_days": len(excess), "unpaid_days": len(unpaid), "deduction": deduction,
            "per_day": per_day_pay(salary, days[0]), "salary_known": bool(salary), "warnings": warnings,
            "needs_consent": bool(unpaid or excess), "consent_text": consent, "parts": parts}


def balance(uid: str, on: date) -> dict | None:
    emp, *rest = ctx = _context(uid)
    if not emp:
        return None
    elig = eligible_from(emp.get("joined"))
    u = usage(emp, ctx)
    y, q, q1, q2 = quarter(on)
    ok = elig is None or on >= elig
    used_year = u[("C-Y", y)] + u[("A-Y", y)] + u[("F-Y", y)]
    fest = festival_dates()
    return {"eligible_from": elig.isoformat() if elig else None, "eligible": ok, "joined_known": elig is not None,
            "notice_days": NOTICE_DAYS, "fy": f"{y}-{str(y + 1)[2:]}",
            "casual": {"month_left": max(left(u, "Casual", on), 0) if ok else 0, "month_quota": CASUAL_PER_MONTH,
                       "year_used": u[("C-Y", y)], "year_quota": CASUAL_PER_YEAR},
            "adhoc": {"left": max(left(u, "Adhoc", on), 0) if ok else 0, "quota": ADHOC_PER_QUARTER,
                      "period_start": q1.isoformat(), "period_end": q2.isoformat(), "year_used": u[("A-Y", y)]},
            "festive": {"left": max(FESTIVE_PER_YEAR - u[("F-Y", y)], 0) if ok else 0, "quota": FESTIVE_PER_YEAR,
                        "dates": [{"date": d, "name": n} for d, n in sorted(fest.items()) if d >= on.isoformat()][:20]},
            "year": {"entitlement": ANNUAL_TOTAL, "used": used_year, "left": max(ANNUAL_TOTAL - used_year, 0)}}


def year_summary(fy_start: int) -> list[dict]:
    """Leave used and the balance left for encashment at the end of the financial year (31 March)."""
    shifts, emps, holidays, _, changes = reports._load()
    out = []
    for e in emps:
        if not e["active"] and (e.get("left_on") or "") < f"{fy_start}-04-01":   # left before this year
            continue
        u = usage(e, (e, shifts, holidays, changes))
        c, a, f = u[("C-Y", fy_start)], u[("A-Y", fy_start)], u[("F-Y", fy_start)]
        bal = max(ANNUAL_TOTAL - c - a - f, 0)
        rate = round((e.get("salary") or 0) / 31, 2)   # a day's salary in March
        out.append({"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
                    "casual": c, "adhoc": a, "festive": f, "used": c + a + f, "balance": bal,
                    "day_rate": rate, "encashment": round(bal * rate, 2)})
    return out
