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


def validate(uid: str, kind: str, start_s: str, end_s: str, applied_on: date, strict: bool = True) -> dict:
    """Check an application against the policy. Returns {"days": [...]} or {"error": "..."}.
    strict=True is for employees applying themselves (also limits back-dating)."""
    emp, *rest = ctx = _context(uid)
    if not emp:
        return {"error": "Employee not found"}
    if kind not in POLICY_TYPES:
        return {"error": "Leave type must be Normal or Adhoc"}
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        return {"error": "Dates must look like 2026-10-20"}
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
    if elig and days[0] < elig:
        return {"error": f"Leave under the policy starts on {elig.isoformat()} (3 months after joining)"}
    if kind == "Normal" and (start - applied_on).days < NOTICE_DAYS:
        return {"error": f"Normal leave must be applied at least {NOTICE_DAYS} days before the leave date. "
                         "For an emergency use Adhoc leave."}
    per_period: dict[str, list[date]] = {}
    for d in days:
        per_period.setdefault(period(kind, d, elig)[0], []).append(d)
    for key, ds in per_period.items():
        left = quota(kind) - used(emp, kind, key, elig, ctx)
        if len(ds) > left:
            _, first, last = period(kind, ds[0], elig)
            span = first.strftime("%b %Y") if kind == "Normal" else f"{first:%d %b %Y} to {last:%d %b %Y}"
            return {"error": f"Not enough {kind} leave for {span}: {max(left, 0):g} left, {len(ds)} requested"}
    return {"days": [d.isoformat() for d in days]}


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
