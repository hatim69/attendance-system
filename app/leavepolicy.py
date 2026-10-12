"""Company leave policy.

  * Leave starts WAIT_MONTHS (3) months after the date of joining.
  * NORMAL leave: NORMAL_PER_MONTH (2) per calendar month; must be applied at least NOTICE_DAYS (7) days before the leave.
  * ADHOC leave: ADHOC_PER_PERIOD (1) per 3-month period (periods start on the eligibility date); meant for emergencies
    without 7 days' notice, so it has no notice requirement.

Assumptions to confirm with the business (all in this file, easy to change):
  * unused leave does not carry forward (each month / 3-month period has its own allowance)
  * the month in which a person becomes eligible counts in full
  * only working days count (weekly offs and holidays inside a leave are free)
  * pending applications reserve balance, rejected ones do not"""
import calendar
from datetime import date, timedelta

from . import db, reports

WAIT_MONTHS = 3
NORMAL_PER_MONTH = 2
ADHOC_PER_PERIOD = 1
PERIOD_MONTHS = 3
NOTICE_DAYS = 7
BACKDATE_DAYS = 7        # employees applying in the app may start a leave at most this many days in the past
MAX_RANGE_DAYS = 31
POLICY_TYPES = ("Normal", "Adhoc")


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def eligible_from(joined: str | None) -> date | None:
    """First day on which policy leave can be taken, or None when the joining date is not recorded."""
    return add_months(date.fromisoformat(joined), WAIT_MONTHS) if joined else None


def period(kind: str, d: date, start: date | None) -> tuple[str, date, date]:
    """(key, first day, last day) of the allowance period that contains `d`."""
    if kind == "Normal" or start is None:
        return d.strftime("%Y-%m"), d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])
    months = (d.year - start.year) * 12 + d.month - start.month - (1 if d.day < start.day else 0)
    k = max(months, 0) // PERIOD_MONTHS
    first = add_months(start, k * PERIOD_MONTHS)
    last = add_months(start, (k + 1) * PERIOD_MONTHS) - timedelta(days=1)
    return f"{first.isoformat()}", first, last


def quota(kind: str) -> float:
    return NORMAL_PER_MONTH if kind == "Normal" else ADHOC_PER_PERIOD


def _context(uid: str):
    shifts, emps, holidays, _, changes = reports._load(scoped=False)
    emp = next((e for e in emps if e["device_user_id"] == uid), None)
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


def used(emp: dict, kind: str, key: str, start: date | None, ctx, exclude_id: int | None = None) -> int:
    """Working days already taken or reserved (approved + pending) of `kind` in the period `key`."""
    n = 0
    for r in db.conn().execute("SELECT * FROM leaves WHERE device_user_id=? AND type=? AND status IN ('approved','pending')",
                               (emp["device_user_id"], kind)):
        if exclude_id and r["id"] == exclude_id:
            continue
        for d in working_days(emp, date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]), ctx):
            if period(kind, d, start)[0] == key:
                n += 1
    return n


LOP_MULTIPLIER = 1.0   # pay cut per unpaid day = monthly salary / calendar days in that month x this (to be set per policy)


def per_day_pay(salary: float, d: date) -> float:
    return round((salary or 0) / calendar.monthrange(d.year, d.month)[1] * LOP_MULTIPLIER, 2)


def plan(uid: str, kind: str, start_s: str, end_s: str, applied_on: date, strict: bool = True) -> dict:
    """Work out how an application would be taken: each working day becomes Normal, Adhoc or Unpaid.

    Normal (with 7 days' notice) uses the Normal balance first, then falls back to Adhoc; Adhoc uses the Adhoc balance;
    anything left over, and days before leave starts (3 months after joining), is Unpaid. Returns the split, the pay
    deduction, warnings to show, and whether the employee must sign a consent (any unpaid day), or {"error": "..."}.
    strict=True is for employees applying themselves (limits back-dating)."""
    emp, *rest = ctx = _context(uid)
    if not emp:
        return {"error": "Employee not found"}
    if kind not in POLICY_TYPES:
        return {"error": "Leave type must be Normal or Adhoc"}
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
    days = working_days(emp, start, end, ctx)
    if not days:
        return {"error": "There are no working days in that range (weekly offs and holidays are not counted)"}
    elig = eligible_from(emp.get("joined"))
    notice_ok = (start - applied_on).days >= NOTICE_DAYS
    left: dict[tuple, float] = {}

    def slot(k: str, d: date) -> tuple:
        key = (k, period(k, d, elig)[0])
        if key not in left:
            left[key] = quota(k) - used(emp, k, key[1], elig, ctx)
        return key

    alloc = []
    for d in days:
        t = "Unpaid"
        if not (elig and d < elig):
            for k in (["Normal"] if kind == "Normal" and notice_ok else []) + ["Adhoc"]:
                key = slot(k, d)
                if left[key] > 0:
                    left[key] -= 1
                    t = k
                    break
        alloc.append((d, t))
    n = {t: sum(1 for _, x in alloc if x == t) for t in ("Normal", "Adhoc", "Unpaid")}
    unpaid = [d for d, t in alloc if t == "Unpaid"]
    deduction = round(sum(per_day_pay(emp.get("salary") or 0, d) for d in unpaid), 2)
    per_day = round(deduction / len(unpaid), 2) if unpaid else 0
    adhoc_left = max(left.get(slot("Adhoc", days[0]), 0), 0) if not (elig and days[0] < elig) else 0

    warnings = []
    if elig and days[0] < elig:
        warnings.append(f"Your leave starts on {elig:%d %b %Y} (3 months after joining), so days before that are unpaid.")
    if kind == "Normal" and not notice_ok and n["Adhoc"]:
        warnings.append(f"Normal leave needs {NOTICE_DAYS} days' notice, so {n['Adhoc']} day(s) will use your Adhoc "
                        f"(emergency) leave. Adhoc left after this: {adhoc_left:g}.")
    elif kind == "Normal" and n["Adhoc"]:
        warnings.append(f"Your Normal leave balance is not enough, so {n['Adhoc']} day(s) will use your Adhoc leave. "
                        f"Adhoc left after this: {adhoc_left:g}.")
    if unpaid:
        cut = (f"Rs {per_day:,.2f} per day x {len(unpaid)} = Rs {deduction:,.2f} will be deducted from your salary"
               if emp.get("salary") else "your daily salary will be deducted for each of these days")
        warnings.append(f"You don't have enough leave balance: {len(unpaid)} extra day(s) will be unpaid, and {cut}.")
    consent = ""
    if unpaid:
        consent = (f"I, {emp['name']} (employee ID {emp['device_user_id']}), am applying for leave from {start:%d %b %Y} to "
                   f"{end:%d %b %Y} ({len(days)} working day(s)). I understand that my leave balance is not enough and "
                   f"{len(unpaid)} day(s) will be unpaid"
                   + (f", and that Rs {deduction:,.2f} (Rs {per_day:,.2f} per day) will be deducted from my salary"
                      if emp.get("salary") else ", and that my daily salary will be deducted for each of these days")
                   + " as per the company's leave policy. I agree to this deduction.")
    # consecutive calendar days of the same kind become one leave record (weekends/holidays split records,
    # so an unpaid stretch never swallows a paid weekly off)
    parts: list[dict] = []
    for d, t in alloc:
        if parts and parts[-1]["type"] == t and date.fromisoformat(parts[-1]["end_date"]) + timedelta(days=1) == d:
            parts[-1]["end_date"] = d.isoformat()
        else:
            parts.append({"type": t, "start_date": d.isoformat(), "end_date": d.isoformat()})
    return {"days": [d.isoformat() for d in days], "working_days": len(days), "normal_days": n["Normal"],
            "adhoc_days": n["Adhoc"], "unpaid_days": n["Unpaid"], "per_day": per_day, "deduction": deduction,
            "salary_known": bool(emp.get("salary")), "warnings": warnings, "needs_consent": bool(unpaid),
            "consent_text": consent, "parts": parts}


def balance(uid: str, on: date) -> dict | None:
    emp, *rest = ctx = _context(uid)
    if not emp:
        return None
    elig = eligible_from(emp.get("joined"))
    out = {"eligible_from": elig.isoformat() if elig else None, "notice_days": NOTICE_DAYS,
           "eligible": elig is None or on >= elig, "joined_known": elig is not None}
    for kind in POLICY_TYPES:
        key, first, last = period(kind, on, elig)
        u = used(emp, kind, key, elig, ctx)
        q = quota(kind) if out["eligible"] else 0
        out[kind.lower()] = {"period_start": first.isoformat(), "period_end": last.isoformat(), "quota": q,
                             "used": u, "left": max(q - u, 0)}
    return out
