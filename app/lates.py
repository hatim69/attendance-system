"""Late arrivals: a counter per employee over an HR-chosen tracking period, flags at the limit, and e-mails.

A late arrival is a day the attendance engine marks late (first punch after shift start + the shift's grace, 10 minutes
by default). Counts are always worked out from the attendance records, so a corrected punch or an approved leave
changes them everywhere at once. Nothing here touches salary: reaching or passing the limit only flags the person for
HR review.

E-mails go out on the day itself, within a minute of a late check-in, to the employee, their team lead(s) and HR; each
person-day is e-mailed at most once. Corrections or imports of past days never send e-mails."""
import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from . import config, db, leavepolicy, mailer, reports
from .timeutil import now_local

log = logging.getLogger("lates")
PERIODS = {"month": "Calendar month", "quarter": "Quarter (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar)",
           "fy": "Financial year (April to March)", "rolling30": "Last 30 days"}
DEFAULT = {"period": "month", "limit": 3}
MAX_ATTEMPTS = 3


def policy() -> dict:
    row = db.conn().execute("SELECT value FROM settings WHERE key='late_policy'").fetchone()
    p = {**DEFAULT, **(json.loads(row["value"]) if row else {})}
    return {**p, "period_label": PERIODS[p["period"]]}


def save_policy(period: str, limit: int) -> dict:
    if period not in PERIODS:
        raise ValueError("Tracking period must be one of: " + ", ".join(PERIODS))
    if not 1 <= limit <= 31:
        raise ValueError("The limit must be between 1 and 31")
    c = db.conn()
    c.execute("INSERT INTO settings(key,value) VALUES ('late_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (json.dumps({"period": period, "limit": limit}),))
    c.commit()
    return policy()


def bounds(d: date, period: str) -> tuple[date, date]:
    """First and last day of the tracking period that contains d."""
    if period == "rolling30":
        return d - timedelta(days=29), d
    if period == "quarter":
        _, _, a, b = leavepolicy.quarter(d)
        return a, b
    if period == "fy":
        y = leavepolicy.fy(d)
        return date(y, 4, 1), date(y + 1, 3, 31)
    a = d.replace(day=1)
    return a, leavepolicy.add_months(a, 1) - timedelta(days=1)


def state(count: int, limit: int) -> str:
    return "over" if count > limit else "limit" if count == limit else "ok"


def counts(as_of: date | None = None, employee_id: str | None = None) -> list[dict]:
    """Qualifying lates per employee in the period up to as_of (default today), worst first. Respects the team-lead
    scope of the caller (through the attendance engine)."""
    p = policy()
    as_of = min(as_of or now_local().date(), now_local().date())
    a, _ = bounds(as_of, p["period"])
    per: dict[str, dict] = {}
    for r in reports.timecards(a.isoformat(), as_of.isoformat(), employee_id):
        x = per.setdefault(r["device_user_id"], {"device_user_id": r["device_user_id"], "name": r["name"],
                                                  "department": r["department"], "count": 0, "lates": []})
        if r["late_min"] > 0:
            x["count"] += 1
            x["lates"].append({"date": r["date"], "late_min": r["late_min"], "scheduled_in": r["scheduled_in"], "in": r["in"]})
    out = []
    for x in per.values():
        out.append({**x, "limit": p["limit"], "state": state(x["count"], p["limit"]), "period": p["period"],
                    "period_label": p["period_label"], "from": a.isoformat(), "to": as_of.isoformat()})
    return sorted(out, key=lambda x: (-x["count"], x["name"].lower()))


def _recipients(emp: dict) -> list[str]:
    c = db.conn()
    to = [emp.get("work_email") or emp.get("email") or ""]
    to += [r["email"] for r in c.execute("SELECT email FROM users WHERE role='manager' AND active=1 AND department=? "
                                         "AND email!=''", (emp["department"] or "-",))]
    to.append(config.HR_EMAIL)
    seen, out = set(), []
    for t in to:
        t = (t or "").strip().lower()
        if t and "@" in t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def message(emp: dict, late: dict, count: int, p: dict) -> tuple[str, str]:
    d = datetime.strptime(late["date"], "%Y-%m-%d")
    head = f"Late arrival: {emp['name']} ({emp['device_user_id']}) on {d:%d %b %Y}"
    st = state(count, p["limit"])
    if st == "limit":
        subject = f"{head} - {_ordinal(count)} late arrival, limit of {p['limit']} reached"
    elif st == "over":
        subject = f"{head} - {_ordinal(count)} late arrival, over the limit: for HR review"
    else:
        subject = f"{head} ({_ordinal(count)} this period)"
    body = (f"{emp['name']} (employee ID {emp['device_user_id']}) arrived late.\n\n"
            f"Date: {d:%A, %d %B %Y}\n"
            f"Scheduled login: {late['scheduled_in']}\n"
            f"Actual login: {late['in']}\n"
            f"Minutes late: {late['late_min']}\n"
            f"Late arrivals this tracking period ({p['period_label'].lower()}): {count} of {p['limit']} allowed\n")
    if st == "limit":
        body += f"\nThis is the {_ordinal(count)} late arrival: the limit of {p['limit']} has been reached.\n"
    elif st == "over":
        body += f"\nThis is beyond the limit of {p['limit']} late arrivals and is flagged for HR review.\n"
    body += ("\nLate means checking in more than the shift's grace period (10 minutes) after the shift starts. "
             "No salary is deducted automatically.\n")
    return subject, body


def run_once(now: datetime | None = None) -> int:
    """E-mail today's new late arrivals. Returns how many e-mails were sent."""
    if not mailer.configured():
        return 0
    now = now or now_local()
    today = now.date().isoformat()
    c = db.conn()
    done = {r["device_user_id"]: r for r in c.execute("SELECT * FROM late_notices WHERE date=?", (today,))}
    late_today = [r for r in reports.daily(today) if r["late_min"] > 0]
    sent = 0
    p = policy()
    for r in late_today:
        uid = r["device_user_id"]
        prev = done.get(uid)
        if prev and (prev["sent_at"] or prev["attempts"] >= MAX_ATTEMPTS):
            continue
        emp = dict(c.execute("SELECT * FROM employees WHERE device_user_id=?", (uid,)).fetchone())
        mine = counts(now.date(), uid)
        info = mine[0] if mine else None
        late = next((x for x in (info["lates"] if info else []) if x["date"] == today), None)
        if not late:
            continue
        count = info["count"]
        to = _recipients(emp)
        subject, body = message(emp, late, count, p)
        err, at = "", None
        try:
            mailer.send(to, subject, body)
            at, sent = now.strftime("%Y-%m-%d %H:%M:%S"), sent + 1
        except Exception as e:
            log.warning("late e-mail for %s failed: %s", uid, e)
            err = f"{type(e).__name__}: {e}"[:300]
        c.execute("INSERT INTO late_notices(device_user_id,date,late_min,count,recipients,sent_at,error,attempts) "
                  "VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(device_user_id,date) DO UPDATE SET late_min=excluded.late_min, "
                  "count=excluded.count, recipients=excluded.recipients, sent_at=excluded.sent_at, error=excluded.error, "
                  "attempts=late_notices.attempts+1",
                  (uid, today, late["late_min"], count, ", ".join(to), at, err))
        c.commit()
    return sent


async def loop():
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception("late-arrival check failed")
        await asyncio.sleep(60)
