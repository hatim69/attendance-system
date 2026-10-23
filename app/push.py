"""Push reminders to the phone app (Web Push; inside the Play Store app via notification delegation).

  * Not checked in 1 hour after the shift started -> "You haven't checked in".
  * Checked in and 15 minutes before the shift ends -> "Don't forget to check out".

Only working days of employees with a shift: weekly offs, holidays and full-day leave are skipped. Each reminder is
sent at most once a day. The signing keys (VAPID) are created on first use and kept in the database."""
import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import config, db, reports
from .timeutil import now_local

IN_AFTER_MIN = 60      # remind to check in this long after the shift starts
OUT_BEFORE_MIN = 15    # remind to check out this long before the shift ends
log = logging.getLogger("push")
_vapid: tuple | None = None


def _keys() -> tuple:
    global _vapid
    if _vapid and _vapid[2] == config.DB_PATH:
        return _vapid
    c = db.conn()
    row = c.execute("SELECT value FROM settings WHERE key='vapid_private_pem'").fetchone()
    if not row:
        v = Vapid()
        v.generate_keys()
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('vapid_private_pem',?)", (v.private_pem().decode(),))
        c.commit()
        row = c.execute("SELECT value FROM settings WHERE key='vapid_private_pem'").fetchone()
    v = Vapid.from_pem(row["value"].encode())
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _vapid = (v, base64.urlsafe_b64encode(raw).rstrip(b"=").decode(), config.DB_PATH)
    return _vapid


def public_key() -> str:
    return _keys()[1]


def _claims() -> dict:
    return {"sub": f"https://{config.PUBLIC_DOMAIN}" if config.PUBLIC_DOMAIN else "mailto:admin@example.com"}


def send(uid: str, title: str, body: str) -> int:
    """Send to every phone the employee turned reminders on for. Returns how many accepted it."""
    vapid, _, _ = _keys()
    c = db.conn()
    ok = 0
    for s in c.execute("SELECT * FROM push_subscriptions WHERE device_user_id=?", (uid,)).fetchall():
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    json.dumps({"title": title, "body": body, "url": "/m/"}), vapid_private_key=vapid,
                    vapid_claims=_claims(), timeout=10, ttl=3600)
            ok += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):   # phone unsubscribed / app removed
                c.execute("DELETE FROM push_subscriptions WHERE id=?", (s["id"],))
                c.commit()
            else:
                log.warning("push to %s failed: %s", uid, e)
        except Exception as e:  # network trouble: try again next time
            log.warning("push to %s failed: %s", uid, e)
    return ok


def due(now) -> list[dict]:
    """Reminders to send at `now` (local time)."""
    day, mins = now.date().isoformat(), now.hour * 60 + now.minute
    c = db.conn()
    subs = {r[0] for r in c.execute("SELECT DISTINCT device_user_id FROM push_subscriptions")}
    if not subs:
        return []
    shifts, emps, holidays, leaves, changes = reports._load(scoped=False)
    sent = {(r[0], r[1]) for r in c.execute("SELECT device_user_id, kind FROM push_log WHERE date=?", (day,))}
    punches = {r[0]: r[1] for r in c.execute(
        "SELECT device_user_id, COUNT(*) FROM punches WHERE date(ts)=? AND COALESCE(flag,'')!='rejected' GROUP BY device_user_id", (day,))}
    wd = reports.WEEKDAYS[now.weekday()]
    out = []
    for e in emps:
        uid = e["device_user_id"]
        if not e["active"] or uid not in subs:
            continue
        sh = reports.shift_on(e, day, shifts, changes)
        if not sh or day in holidays or wd in [o.strip().upper() for o in (sh["weekly_off"] or "").split(",")]:
            continue
        leave = next((l for l in leaves if l["device_user_id"] == uid and l["start_date"] <= day <= l["end_date"]), None)
        if leave and not leave.get("half"):
            continue
        start, end = reports._hm(sh["start"]), reports._hm(sh["end"])
        n = punches.get(uid, 0)
        if n == 0 and not leave and start + IN_AFTER_MIN <= mins < end and (uid, "in") not in sent:
            out.append({"uid": uid, "kind": "in", "title": "You haven't checked in",
                        "body": f"Your shift started at {sh['start']}. Open the app to check in."})
        if n % 2 == 1 and end - OUT_BEFORE_MIN <= mins < end and (uid, "out") not in sent:
            out.append({"uid": uid, "kind": "out", "title": "Don't forget to check out",
                        "body": f"Your shift ends at {sh['end']}. Check out before you leave."})
    return out


def for_employee(uid: str, now, out_grace: int = 30) -> list[dict]:
    """The reminders that apply to one person right now (the native app asks for them; it shows each once a day).
    The check-out reminder stays on until out_grace minutes after the shift ends, as the phone checks every ~15 min."""
    day, mins = now.date().isoformat(), now.hour * 60 + now.minute
    shifts, emps, holidays, leaves, changes = reports._load(scoped=False)
    e = next((x for x in emps if x["device_user_id"] == uid and x["active"]), None)
    if not e:
        return []
    sh = reports.shift_on(e, day, shifts, changes)
    if not sh or day in holidays or reports.WEEKDAYS[now.weekday()] in [o.strip().upper() for o in (sh["weekly_off"] or "").split(",")]:
        return []
    leave = next((l for l in leaves if l["device_user_id"] == uid and l["start_date"] <= day <= l["end_date"]), None)
    if leave and not leave.get("half"):
        return []
    start, end = reports._hm(sh["start"]), reports._hm(sh["end"])
    n = db.conn().execute("SELECT COUNT(*) FROM punches WHERE device_user_id=? AND date(ts)=? AND COALESCE(flag,'')!='rejected'",
                          (uid, day)).fetchone()[0]
    out = []
    if n == 0 and not leave and start + IN_AFTER_MIN <= mins < end:
        out.append({"key": f"in-{day}", "title": "You haven't checked in",
                    "body": f"Your shift started at {sh['start']}. Open the app to check in."})
    if n % 2 == 1 and end - OUT_BEFORE_MIN <= mins < end + out_grace:
        out.append({"key": f"out-{day}", "title": "Don't forget to check out",
                    "body": f"Your shift ends at {sh['end']}. Check out before you leave."})
    return out


def run_once(now=None) -> int:
    now = now or now_local()
    c = db.conn()
    n = 0
    for r in due(now):
        send(r["uid"], r["title"], r["body"])
        c.execute("INSERT OR IGNORE INTO push_log(device_user_id,date,kind,sent_at) VALUES (?,?,?,?)",
                  (r["uid"], now.date().isoformat(), r["kind"], now.strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
        n += 1
    return n


async def loop():
    """Checks once a minute (started with the app)."""
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception("reminder check failed")
        await asyncio.sleep(60)
