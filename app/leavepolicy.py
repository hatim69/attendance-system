"""MTT leave policy (financial year April - March). Two kinds of leave:

  * INFORMED leave (applied at least 7 days in advance) - shown in yellow.
  * ADHOC leave (emergency, uninformed, or less than 7 days' notice) - shown in red.

Leave is credited month by month: the first month of every quarter (April, July, October, January) gives
1 Informed + 1 Adhoc, the other two months give 2 Informed each, so a full year is 20 Informed + 4 Adhoc = 24.
Unused credits carry forward until 31 March, when what is left is encashed; everyone starts again at 0 in April.
There is no monthly limit: any credit already earned can be used.

  * Leave starts after 3 months of working: credits are earned from the month in which the person becomes eligible.
    Before that there is no balance, so every leave day is beyond the balance (see below).
  * Informed applied with less than 7 days' notice counts as Adhoc. When Informed credits run out, an Adhoc credit is
    used instead if there is one.
  * Beyond the balance the day is still leave, but salary is cut: 1.5 x a day's salary for Informed, 2 x for Adhoc.
    An Adhoc day beyond the Adhoc balance also uses up 1 Informed credit.
  * HALF DAY: half a day from the Informed (then Adhoc) balance, so the day is paid in full; with no balance,
    0.5 x 1.5 = 0.75 of a day's salary is cut. No notice needed.
  * A day's salary = monthly salary / calendar days in that month.

Only working days count (weekly offs and holidays inside a leave are free). Pending applications reserve balance;
rejected ones do not. Leave beyond the balance is recorded with a pay-cut multiplier (`lop`) that payroll applies."""
import calendar
from datetime import date, timedelta

from . import db, reports

WAIT_MONTHS = 3
INFORMED_PER_YEAR, ADHOC_PER_YEAR = 20, 4
ANNUAL_TOTAL = INFORMED_PER_YEAR + ADHOC_PER_YEAR
NOTICE_DAYS = 7
INFORMED_EXCESS_CUT = 1.5  # x a day's salary for each Informed day beyond the balance
ADHOC_EXCESS_CUT = 2.0     # x a day's salary for each Adhoc day beyond the balance
BACKDATE_DAYS = 7          # employees applying in the app may start a leave at most this many days in the past
MAX_RANGE_DAYS = 31
POLICY_TYPES = ("Informed", "Adhoc")
ALIASES = {"Casual": "Informed", "Normal": "Informed", "CL": "Informed", "Sick": "Adhoc"}


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def eligible_from(joined: str | None) -> date | None:
    """First day on which leave credits apply, or None when the joining date is not recorded."""
    return add_months(date.fromisoformat(joined), WAIT_MONTHS) if joined else None


def fy(d: date) -> int:
    """Financial year that contains d, named by its starting year (2026 = Apr 2026 - Mar 2027)."""
    return d.year if d.month >= 4 else d.year - 1


def quarter(d: date) -> tuple[int, int, date, date]:
    """(fy, quarter 1-4, first day, last day) of the financial-year quarter containing d."""
    y = fy(d)
    q = ((d.month - 4) % 12) // 3 + 1
    first = add_months(date(y, 4, 1), 3 * (q - 1))
    return y, q, first, add_months(first, 3) - timedelta(days=1)


def per_day_pay(salary: float, d: date) -> float:
    return round((salary or 0) / calendar.monthrange(d.year, d.month)[1], 2)


def adjustments(uid: str) -> dict[int, tuple[float, float]]:
    """Days HR added (or took away) per financial year: {fy: (informed, adhoc)}."""
    out: dict[int, tuple[float, float]] = {}
    for r in db.conn().execute("SELECT fy, SUM(informed) i, SUM(adhoc) a FROM leave_adjustments WHERE device_user_id=? "
                               "GROUP BY fy", (uid,)):
        out[r["fy"]] = (r["i"] or 0, r["a"] or 0)
    return out


def credited(emp: dict, d: date) -> tuple[float, float]:
    """(Informed, Adhoc) credited from 1 April of d's financial year up to and including d's month, plus any
    balance adjustment HR made for that year (usable from the start of the year, once the person is eligible)."""
    if "_adj" not in emp:
        emp["_adj"] = adjustments(emp["device_user_id"])
    elig = eligible_from(emp.get("joined"))
    first = date(elig.year, elig.month, 1) if elig else date.min
    inf, adh = emp["_adj"].get(fy(d), (0, 0))
    m = date(fy(d), 4, 1)
    while m <= d:
        if m >= first:
            k = (m.month - 4) % 3          # 0 = first month of the quarter
            inf += 1 if k == 0 else 2
            adh += 1 if k == 0 else 0
        m = add_months(m, 1)
    return inf, adh


def _month_ends(d: date) -> list[date]:
    """d's month end and every later month end up to 31 March of d's financial year."""
    out, m = [], date(d.year, d.month, 1)
    end = date(fy(d) + 1, 3, 31)
    while m <= end:
        out.append(add_months(m, 1) - timedelta(days=1))
        m = add_months(m, 1)
    return out


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


def _bucket(t: str, lop: float) -> str | None:
    """Which credit one leave day of this kind uses: 'I'nformed, 'A'dhoc, or none (beyond the balance)."""
    t = ALIASES.get(t, t)
    if t == "Informed":
        return None if lop else "I"
    if t == "Adhoc":
        return "I" if lop >= ADHOC_EXCESS_CUT else "A"   # Adhoc beyond the balance also uses 1 Informed credit
    return None


def _leave_usage(emp: dict, ctx) -> list[tuple[date, str, float]]:
    """Credits used or reserved by leave records: approved, and pending for days still to come (a pending application
    for a day that has passed is an absence, counted by absences() instead). Days before the person is eligible use
    no credits (they had none)."""
    elig = eligible_from(emp.get("joined"))
    today = reports.now_local().date()
    out = []
    for r in db.conn().execute("SELECT * FROM leaves WHERE device_user_id=? AND status IN ('approved','pending')",
                               (emp["device_user_id"],)):
        unit = 0.5 if r["half"] else 1
        b = _bucket(r["type"], (r["lop"] or 0) / unit)   # lop is stored per leave day, so per full day here
        if not b:
            continue
        s, e = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        out += [(d, b, unit) for d in working_days(emp, s, e, ctx)
                if not (elig and d < elig) and not (r["status"] == "pending" and d < today)]
    return out


def absences(emp: dict, ctx, fy_start: int) -> dict[str, float]:
    """Working days of the year (up to yesterday) on which the person was absent without approved leave (none applied,
    still pending, or rejected). Each counts as Adhoc leave: {day: 0.0} when an Adhoc credit paid for it, {day: 2.0}
    when there was none left (2 x a day's salary cut and 1 Informed credit used, like any Adhoc beyond the balance)."""
    _, shifts, holidays, changes = ctx
    today = reports.now_local().date()
    first = max(date(fy_start, 4, 1), date.fromisoformat(emp["count_from"]) if emp.get("count_from") else date.min,
                date.fromisoformat(emp["joined"]) if emp.get("joined") else date.min, absence_rule_from())
    last = min(date(fy_start + 1, 3, 31), today - timedelta(days=1))
    if emp.get("left_on"):
        last = min(last, date.fromisoformat(emp["left_on"]))
    if last < first:
        return {}
    leaves = [dict(r) for r in db.conn().execute("SELECT * FROM leaves WHERE device_user_id=? AND status='approved'",
                                                 (emp["device_user_id"],))]
    pm = {k[1]: v for k, v in reports._punch_map(first.isoformat(), last.isoformat(), None).items()
          if k[0] == emp["device_user_id"]}
    elig = eligible_from(emp.get("joined"))
    used = _leave_usage(emp, ctx)
    out: dict[str, float] = {}
    d = first
    while d <= last:
        iso = d.isoformat()
        r = reports.evaluate(emp, reports.shift_on(emp, iso, shifts, changes), iso, pm.get(iso), holidays, leaves, today.isoformat())
        if r["status"] == "Absent" and "half_leave" not in r:
            if not (elig and d < elig) and left(emp, used, "A", d) >= 1:
                used.append((d, "A", 1))
                out[iso] = 0.0
            else:
                if not (elig and d < elig):
                    used.append((d, "I", 1))       # Adhoc beyond the balance also uses 1 Informed credit
                out[iso] = ADHOC_EXCESS_CUT
        d += timedelta(days=1)
    return out


def absence_rule_from() -> date:
    """First day on which an absence without approved leave counts as Adhoc (set when the rule was installed)."""
    row = db.conn().execute("SELECT value FROM settings WHERE key='absent_as_adhoc_from'").fetchone()
    return date.fromisoformat(row["value"]) if row else date.max


def usage(emp: dict, ctx, years: set[int] | None = None) -> list[tuple[date, str, float]]:
    """All credits used or reserved: leave records, plus absences without approved leave (which count as Adhoc) in
    the given financial years (default: the current one)."""
    out = _leave_usage(emp, ctx)
    elig = eligible_from(emp.get("joined"))
    for y in years or {fy(reports.now_local().date())}:
        for iso, lop in absences(emp, ctx, y).items():
            d = date.fromisoformat(iso)
            if not (elig and d < elig):
                out.append((d, "A" if not lop else "I", 1))
    return out


def left(emp: dict, used: list, bucket: str, d: date) -> float:
    """Credit of this kind free to use on day d: what was earned by d's month, minus what is used up to then, and
    without taking credit that a later booking in the same year already relies on."""
    i = 0 if bucket == "I" else 1
    y = fy(d)
    return min(credited(emp, m)[i] - sum(a for x, b, a in used if b == bucket and fy(x) == y and x <= m)
               for m in _month_ends(d))


def plan(uid: str, kind: str, start_s: str, end_s: str, applied_on: date, strict: bool = True, half: bool = False) -> dict:
    """Work out how an application would be taken. Each working day becomes Informed or Adhoc within the balance,
    or Informed / Adhoc beyond it (still leave, with a pay cut of 1.5x / 2x a day's salary).
    Returns the split, the pay cut, warnings to show, and whether the employee must sign a consent, or {"error": ...}.
    strict=True is for employees applying themselves (limits back-dating)."""
    kind = ALIASES.get(kind, kind)
    emp, *rest = ctx = _context(uid)
    if not emp:
        return {"error": "Employee not found"}
    if kind not in POLICY_TYPES:
        return {"error": "Leave type must be Informed or Adhoc"}
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
        kind = "Informed"   # half days use Informed, then Adhoc, with no notice needed
    days = working_days(emp, start, end, ctx)
    if not days:
        return {"error": "There are no working days in that range (weekly offs and holidays are not counted)"}
    elig = eligible_from(emp.get("joined"))
    notice_ok = half or (start - applied_on).days >= NOTICE_DAYS
    unit = 0.5 if half else 1
    used = usage(emp, ctx)
    alloc = []   # (day, type, lop per full day)
    for d in days:
        early = bool(elig and d < elig)
        if not early and kind == "Informed" and notice_ok and left(emp, used, "I", d) >= unit:
            t, lop = "Informed", 0.0
        elif not early and left(emp, used, "A", d) >= unit:
            t, lop = "Adhoc", 0.0
        elif kind == "Informed" and notice_ok:
            t, lop = "Informed", INFORMED_EXCESS_CUT
        else:
            t, lop = "Adhoc", ADHOC_EXCESS_CUT
        b = None if early else _bucket(t, lop)
        if b:
            used.append((d, b, unit))
        alloc.append((d, t, lop))

    salary = emp.get("salary") or 0
    cnt = lambda t, ex=False: sum(1 for _, x, lop in alloc if x == t and (lop > 0) == ex)
    excess = [(d, lop) for d, t, lop in alloc if lop > 0]
    before = [d for d in days if elig and d < elig]
    deduction = round(sum(per_day_pay(salary, d) * lop * unit for d, lop in excess), 2)
    rs = lambda v: f"Rs {v:,.2f}"

    warnings = []
    if before:
        warnings.append(f"Leave credits start on {elig:%d %b %Y} (after 3 months of working), so {len(before)} day(s) "
                        "before that have no balance.")
    converted = cnt("Adhoc")
    if kind == "Informed" and not notice_ok and (converted or cnt("Adhoc", True)):
        warnings.append(f"Informed leave needs {NOTICE_DAYS} days' notice, so this counts as Adhoc (emergency) leave.")
    elif kind == "Informed" and converted:
        warnings.append(f"Your Informed balance is not enough, so {converted} day(s) will use your Adhoc leave.")
    if half:
        warnings.insert(0, "Half day: half of the day comes from your leave balance, so the day is paid in full"
                        + (" (not this time: see below)." if excess else "."))
    if excess and half:
        warnings.append("You don't have enough leave balance for the half day: 0.5 x 1.5 = 0.75 of a day's salary"
                        + (f" ({rs(deduction)})" if salary else "") + " will be cut.")
    elif excess:
        n15, n2 = cnt("Informed", True), cnt("Adhoc", True)
        bits = ([f"{n15} Informed day(s) x 1.5 day's salary"] if n15 else []) + \
               ([f"{n2} Adhoc day(s) x 2 days' salary (each also uses 1 Informed credit)"] if n2 else [])
        warnings.append("You don't have enough leave balance. Beyond it: " + " and ".join(bits) +
                        (f" = {rs(deduction)} will be cut from your salary." if salary else " will be cut from your salary."))
    consent = ""
    if excess:
        per = per_day_pay(salary, days[0])
        consent = (f"I, {emp['name']} (employee ID {emp['device_user_id']}), am applying for leave from {start:%d %b %Y} to "
                   f"{end:%d %b %Y} ({len(days)} working day(s)). I understand that my leave balance is not enough"
                   + (f" and that {rs(deduction)} will be cut from my salary (a day's salary is about {rs(per)})" if salary
                      else " and that salary will be cut for these days")
                   + ", as per the MTT leave policy: 1.5x a day's salary for each Informed day and 2x for each Adhoc day "
                     "beyond the balance. I agree to this deduction.")
    # consecutive calendar days of the same kind become one leave record (weekends / holidays split records)
    parts: list[dict] = []
    for d, t, lop in alloc:
        if parts and (parts[-1]["type"], parts[-1]["lop"]) == (t, round(lop * unit, 2)) and \
                date.fromisoformat(parts[-1]["end_date"]) + timedelta(days=1) == d:
            parts[-1]["end_date"] = d.isoformat()
        else:
            parts.append({"type": t, "lop": round(lop * unit, 2), "half": int(half),
                          "start_date": d.isoformat(), "end_date": d.isoformat()})
    return {"days": [d.isoformat() for d in days], "working_days": len(days) * unit, "half": half,
            "informed_days": cnt("Informed"), "adhoc_days": cnt("Adhoc"),
            "excess_days": len(excess), "deduction": deduction,
            "per_day": per_day_pay(salary, days[0]), "salary_known": bool(salary), "warnings": warnings,
            "needs_consent": bool(excess), "consent_text": consent, "parts": parts}


def _next_credit(emp: dict, on: date) -> dict | None:
    """When the next credit arrives and what it brings (None after March: a new year starts at 0)."""
    m = add_months(date(on.year, on.month, 1), 1)
    elig = eligible_from(emp.get("joined"))
    while m <= date(fy(on) + 1, 3, 1):
        if not elig or m >= date(elig.year, elig.month, 1):
            k = (m.month - 4) % 3
            return {"date": m.isoformat(), "informed": 1 if k == 0 else 2, "adhoc": 1 if k == 0 else 0}
        m = add_months(m, 1)
    return None


def balance(uid: str, on: date) -> dict | None:
    emp, *rest = ctx = _context(uid)
    if not emp:
        return None
    elig = eligible_from(emp.get("joined"))
    used = usage(emp, ctx)
    y = fy(on)
    ci, ca = credited(emp, on)
    ui = sum(a for d, b, a in used if b == "I" and fy(d) == y)
    ua = sum(a for d, b, a in used if b == "A" and fy(d) == y)
    adj = emp["_adj"].get(y, (0, 0))
    return {"eligible_from": elig.isoformat() if elig else None, "eligible": elig is None or on >= elig,
            "joined_known": elig is not None, "notice_days": NOTICE_DAYS, "fy": f"{y}-{str(y + 1)[2:]}",
            "adjusted": {"informed": adj[0], "adhoc": adj[1]},
            "informed": {"credited": ci, "used": ui, "left": max(left(emp, used, "I", on), 0)},
            "adhoc": {"credited": ca, "used": ua, "left": max(left(emp, used, "A", on), 0)},
            "next_credit": _next_credit(emp, on),
            "year": {"entitlement": ANNUAL_TOTAL, "credited": ci + ca, "used": ui + ua}}


def year_summary(fy_start: int) -> list[dict]:
    """Leave used and the credit left for encashment at the end of the financial year (31 March)."""
    shifts, emps, holidays, _, changes = reports._load()
    out = []
    end = date(fy_start + 1, 3, 31)
    for e in emps:
        if not e["active"] and (e.get("left_on") or "") < f"{fy_start}-04-01":   # left before this year
            continue
        used = [u for u in usage(e, (e, shifts, holidays, changes), {fy_start}) if fy(u[0]) == fy_start]
        ci, ca = credited(e, end)
        ui = sum(a for _, b, a in used if b == "I")
        ua = sum(a for _, b, a in used if b == "A")
        bal = max(ci - ui, 0) + max(ca - ua, 0)
        rate = round((e.get("salary") or 0) / 31, 2)   # a day's salary in March
        adj = e["_adj"].get(fy_start, (0, 0))
        out.append({"device_user_id": e["device_user_id"], "name": e["name"], "department": e["department"],
                    "informed": ui, "informed_credit": ci, "adhoc": ua, "adhoc_credit": ca, "used": ui + ua,
                    "adjusted_informed": adj[0], "adjusted_adhoc": adj[1],
                    "balance": bal, "day_rate": rate, "encashment": round(bal * rate, 2)})
    return out
