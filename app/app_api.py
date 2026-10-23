"""Employee-facing API for the mobile web app: login with ID + PIN, then GPS + selfie punches."""
import os
import re
import uuid
from datetime import date, timedelta

from fastapi.responses import Response
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel

from . import auth, config, db, geo, hub, lates, leavepolicy, payslip, push, reports
from .timeutil import now_local

router = APIRouter(prefix="/api/app")

MAGIC = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}  # webp is RIFF....WEBP


class Login(BaseModel):
    employee_id: str
    pin: str


def current_employee(authorization: str = Header(default="")) -> dict:
    emp_id = auth.read_token(authorization.removeprefix("Bearer ").strip(), "emp")
    row = db.conn().execute("SELECT * FROM employees WHERE device_user_id=? AND active=1", (db.canon_id(emp_id),)).fetchone()
    if not row:
        raise HTTPException(401, "Please log in again")
    return dict(row)


def active_sites(uid: str | None = None) -> list[dict]:
    """Offices, plus this employee's own home location (work from home)."""
    return [dict(r) for r in db.conn().execute(
        "SELECT id,name,lat,lng,radius_m,kind FROM sites WHERE active=1 AND (kind!='home' OR device_user_id=?)", (uid,))]


@router.post("/login")
def login(body: Login):
    emp_id = db.canon_id(body.employee_id)
    if auth.locked(emp_id):
        raise HTTPException(429, "Too many wrong PINs. Try again in 5 minutes.")
    row = db.conn().execute("SELECT * FROM employees WHERE device_user_id=? AND active=1", (emp_id,)).fetchone()
    if not row or not auth.check_pin(body.pin, row["pin_hash"]):
        auth.record_fail(emp_id)
        raise HTTPException(401, "Wrong employee ID or PIN")
    auth.record_ok(emp_id)
    return {"token": auth.make_token(emp_id, "emp"), "name": row["name"]}


@router.get("/me")
def me(emp: dict = Depends(current_employee)):
    today = now_local().strftime("%Y-%m-%d")
    rows = db.conn().execute(
        "SELECT ts, status, method, flag FROM punches WHERE device_user_id=? AND date(ts)=? "
        "AND COALESCE(flag,'')!='rejected' ORDER BY ts", (emp["device_user_id"], today)).fetchall()
    return {"name": emp["name"], "employee_id": emp["device_user_id"], "today": [dict(r) for r in rows],
            "wfh_today": wfh_approved(emp["device_user_id"], today),
            "sites": active_sites(emp["device_user_id"]), "remote_ok": bool(emp.get("remote_ok")),
            "geofence_mode": config.GEOFENCE_MODE, "server_time": now_local().isoformat(" ")}


@router.get("/lates")
def my_lates(emp: dict = Depends(current_employee)):
    """My late arrivals in the current tracking period (the same count HR sees)."""
    mine = lates.counts(None, emp["device_user_id"])
    p = lates.policy()
    return mine[0] if mine else {"count": 0, "limit": p["limit"], "state": "ok", "period_label": p["period_label"], "lates": []}


def wfh_approved(uid: str, day: str) -> bool:
    return db.conn().execute("SELECT 1 FROM wfh_requests WHERE device_user_id=? AND status='approved' AND ? BETWEEN start_date AND end_date",
                             (uid, day)).fetchone() is not None


def _ext(data: bytes) -> str | None:
    for magic, ext in MAGIC.items():
        if data.startswith(magic) and (ext != "webp" or data[8:12] == b"WEBP"):
            return ext
    return None


@router.post("/punch")
async def punch(lat: float = Form(...), lng: float = Form(...), accuracy: float = Form(0),
                selfie: UploadFile = File(...), emp: dict = Depends(current_employee)):
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "Invalid location")
    data = await selfie.read(config.MAX_SELFIE_BYTES + 1)
    if len(data) > config.MAX_SELFIE_BYTES:
        raise HTTPException(413, "Selfie too large")
    ext = _ext(data)
    if not ext:
        raise HTTPException(400, "Selfie must be a JPEG, PNG or WebP image")

    sites = active_sites(emp["device_user_id"])
    site, dist, inside = geo.nearest_site(lat, lng, accuracy, sites)
    flag, note = "", ""
    at_office = inside and site and site.get("kind") != "home"
    if not at_office and wfh_approved(emp["device_user_id"], now_local().strftime("%Y-%m-%d")):
        note = "WFH"                      # an approved work-from-home day: anywhere, paid 0.8 (leave policy)
    elif inside and site and site.get("kind") == "home":
        note = "WFH"
    elif sites and not inside:
        if emp.get("remote_ok"):          # field work: allowed anywhere, location recorded, full day
            note = "Remote"
        elif config.GEOFENCE_MODE == "block":
            raise HTTPException(403, f"You are about {int(dist)} m from {site['name']}. Move closer, or request "
                                     "work from home below.")
        else:
            flag = "outside_geofence"

    now = now_local()
    c = db.conn()
    count = c.execute("SELECT COUNT(*) FROM punches WHERE device_user_id=? AND date(ts)=? AND COALESCE(flag,'')!='rejected'",
                      (emp["device_user_id"], now.strftime("%Y-%m-%d"))).fetchone()[0]
    folder = os.path.join(config.SELFIE_DIR, now.strftime("%Y-%m"))
    os.makedirs(folder, exist_ok=True)
    name = f"{now.strftime('%Y-%m')}/{re.sub(r'[^A-Za-z0-9_-]', '_', emp['device_user_id'])}_{now.strftime('%Y%m%d%H%M%S')}.{ext}"
    path = os.path.join(config.SELFIE_DIR, name)
    rec = db.add_punch(emp["device_user_id"], now, count % 2, "app", method="app", lat=lat, lng=lng,
                       accuracy=accuracy, selfie=name, site_id=site["id"] if site else None,
                       distance_m=dist, flag=flag, note=note)
    if rec is None:
        raise HTTPException(409, "You already punched a moment ago")
    with open(path, "wb") as f:
        f.write(data)
    hub.publish(rec)
    return {"ok": True, "time": rec["ts"], "type": "Check out" if count % 2 else "Check in",
            "site": site["name"] if site and inside else "Work from home" if note == "WFH" else note or None,
            "flag": flag}


@router.get("/attendance")
def my_attendance(month: str = "", emp: dict = Depends(current_employee)):
    """My month day by day (the phone app's Attendance screen): in, out, hours and the status code."""
    month = month or now_local().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(400, "Month must look like 2026-10")
    rows = reports.timecard(month, emp["device_user_id"])
    return [{"date": r["date"], "weekday": r["weekday"], "in": r["in"][:5], "out": r["out"][:5],
             "hours": f"{round(r['hours'] * 60) // 60}:{round(r['hours'] * 60) % 60:02d}" if r["hours"] else "",
             "status": r["status"], "code": r["code"], "late_min": r["late_min"], "note": r["note"]} for r in rows]


@router.get("/notifications")
def my_notifications(emp: dict = Depends(current_employee)):
    """What the phone app should show now: check-in / check-out reminders, and HR's decisions on my leave and
    work-from-home requests from the last 3 days. Each item has a key; the app shows a key only once."""
    now = now_local()
    out = push.for_employee(emp["device_user_id"], now)
    since = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    c = db.conn()
    seen = set()
    for r in c.execute("SELECT id, group_id, start_date, end_date, type, status FROM leaves WHERE device_user_id=? AND requested_by=? "
                       "AND status IN ('approved','rejected') AND decided_at>=? ORDER BY id", (emp["device_user_id"], emp["device_user_id"], since)):
        g = r["group_id"] or r["id"]
        if g in seen:
            continue
        seen.add(g)
        days = _span(r["start_date"], r["end_date"])
        out.append({"key": f"leave-{g}-{r['status']}", "title": f"Leave {r['status']}",
                    "body": f"Your leave for {days} was {r['status']} by HR."})
    for r in c.execute("SELECT id, start_date, end_date, status FROM wfh_requests WHERE device_user_id=? AND status IN ('approved','rejected') "
                       "AND decided_at>=?", (emp["device_user_id"], since)):
        out.append({"key": f"wfh-{r['id']}-{r['status']}", "title": f"Work from home {r['status']}",
                    "body": f"Your work-from-home request for {_span(r['start_date'], r['end_date'])} was {r['status']}."})
    return out


def _span(a: str, b: str) -> str:
    fa = date.fromisoformat(a).strftime("%d %b")
    return fa if a == b else f"{fa} to {date.fromisoformat(b).strftime('%d %b')}"


@router.get("/payslips")
def my_payslips(emp: dict = Depends(current_employee)):
    """Months HR has published that include this employee's pay (newest first)."""
    out = []
    for m in db.conn().execute("SELECT month FROM payroll_months WHERE published=1 ORDER BY month DESC LIMIT 12"):
        d = reports.payslip(m["month"], emp["device_user_id"])
        if d and d["employee"] and emp["salary"]:
            out.append({"month": m["month"], "net_pay": d["net_pay"]})
    return out


@router.get("/payslip/{month}.pdf")
def my_payslip_pdf(month: str, emp: dict = Depends(current_employee)):
    row = db.conn().execute("SELECT published FROM payroll_months WHERE month=?", (month,)).fetchone()
    d = reports.payslip(month, emp["device_user_id"]) if row and row["published"] else None
    if not d or not emp["salary"]:
        raise HTTPException(404, "No payslip available for that month")
    return Response(payslip.build_pdf(d), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=payslip-{month}.pdf", "Cache-Control": "private, no-store"})


class LeaveRequest(BaseModel):
    start_date: str
    end_date: str = ""         # empty = one day
    type: str          # Informed | Adhoc
    reason: str = ""
    consent_name: str = ""     # typed full name = signature, required when part of the leave is unpaid
    half: bool = False         # half-day leave (one date)


@router.get("/leave-balance")
def my_leave_balance(emp: dict = Depends(current_employee)):
    return leavepolicy.balance(emp["device_user_id"], now_local().date())


@router.get("/leaves")
def my_leaves(emp: dict = Depends(current_employee)):
    rows = db.conn().execute(
        "SELECT id, start_date, end_date, type, lop, half, note, status, applied_on FROM leaves WHERE device_user_id=? "
        "AND end_date >= date('now','-400 day') ORDER BY start_date DESC LIMIT 50", (emp["device_user_id"],))
    return [dict(r) for r in rows]


@router.get("/leave/preview")
def preview_leave(type: str, start_date: str, end_date: str = "", half: bool = False, emp: dict = Depends(current_employee)):
    """What applying would mean: Informed / Adhoc split, pay cut, warnings, consent text."""
    p = leavepolicy.plan(emp["device_user_id"], type, start_date, end_date or start_date, now_local().date(), strict=True, half=half)
    if "error" in p:
        raise HTTPException(400, p["error"])
    return p


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


@router.post("/leave")
def apply_leave(body: LeaveRequest, request: Request, emp: dict = Depends(current_employee)):
    """Apply for Informed or Adhoc leave. It stays pending (and reserves the balance) until HR approves it.
    Beyond the balance the days carry a pay cut, which needs the employee's signed consent."""
    today = now_local().date()
    p = leavepolicy.plan(emp["device_user_id"], body.type, body.start_date, body.end_date or body.start_date, today, strict=True,
                         half=body.half)
    if "error" in p:
        raise HTTPException(400, p["error"])
    if p["needs_consent"] and _norm(body.consent_name) != _norm(emp["name"]):
        raise HTTPException(400, f"Part of this leave is beyond your balance. To agree to the pay cut, type your full name exactly as {emp['name']!r}.")
    group, now = uuid.uuid4().hex[:12], now_local().strftime("%Y-%m-%d %H:%M:%S")
    c = db.conn()
    for part in p["parts"]:
        c.execute("INSERT INTO leaves(device_user_id,start_date,end_date,type,note,status,applied_on,requested_by,group_id,lop,half) "
                  "VALUES (?,?,?,?,?, 'pending', ?, ?, ?, ?, ?)",
                  (emp["device_user_id"], part["start_date"], part["end_date"], part["type"], body.reason[:200],
                   today.isoformat(), emp["device_user_id"], group, part["lop"], part["half"]))
    if p["needs_consent"]:
        c.execute("INSERT INTO leave_consents(group_id,device_user_id,text,typed_name,ip,user_agent,at) VALUES (?,?,?,?,?,?,?)",
                  (group, emp["device_user_id"], p["consent_text"], body.consent_name.strip()[:100],
                   request.headers.get("x-forwarded-for", request.client.host if request.client else "")[:100],
                   request.headers.get("user-agent", "")[:300], now))
    c.commit()
    return {"ok": True, "group_id": group, "days": p["working_days"], "informed_days": p["informed_days"],
            "adhoc_days": p["adhoc_days"], "excess_days": p["excess_days"], "deduction": p["deduction"], "status": "pending"}


@router.delete("/leave/{leave_id}")
def cancel_leave(leave_id: int, emp: dict = Depends(current_employee)):
    c = db.conn()
    row = c.execute("SELECT status, group_id FROM leaves WHERE id=? AND device_user_id=? AND requested_by=?",
                    (leave_id, emp["device_user_id"], emp["device_user_id"])).fetchone()
    if not row:
        raise HTTPException(404, "No such leave application")
    if row["status"] != "pending":
        raise HTTPException(400, "Only a pending application can be cancelled. Ask HR about approved leave.")
    if row["group_id"]:   # the whole application (all its parts) goes; its consent stays on record
        c.execute("DELETE FROM leaves WHERE group_id=? AND status='pending'", (row["group_id"],))
    else:
        c.execute("DELETE FROM leaves WHERE id=?", (leave_id,))
    c.commit()
    return {"ok": True}


class WfhRequest(BaseModel):
    start_date: str
    end_date: str = ""
    reason: str = ""


@router.get("/wfh")
def my_wfh(emp: dict = Depends(current_employee)):
    return [dict(r) for r in db.conn().execute(
        "SELECT id, start_date, end_date, reason, status, decided_by FROM wfh_requests WHERE device_user_id=? "
        "ORDER BY start_date DESC LIMIT 20", (emp["device_user_id"],))]


@router.post("/wfh")
def request_wfh(body: WfhRequest, emp: dict = Depends(current_employee)):
    """Ask to work from home. A team lead, HR, management or admin approves it; approved days are paid 0.8."""
    today = now_local().date()
    try:
        s = date.fromisoformat(body.start_date)
        e = date.fromisoformat(body.end_date or body.start_date)
    except ValueError:
        raise HTTPException(400, "Choose the dates")
    if e < s or (e - s).days > 30:
        raise HTTPException(400, "Choose an end date on or after the start, at most 31 days")
    if s < today - timedelta(days=1):
        raise HTTPException(400, "Work from home can be requested from yesterday onwards")
    c = db.conn()
    if c.execute("SELECT 1 FROM wfh_requests WHERE device_user_id=? AND status!='rejected' AND start_date<=? AND end_date>=?",
                 (emp["device_user_id"], e.isoformat(), s.isoformat())).fetchone():
        raise HTTPException(400, "You already have a request for some of these days")
    cur = c.execute("INSERT INTO wfh_requests(device_user_id,start_date,end_date,reason,applied_at) VALUES (?,?,?,?,?)",
                    (emp["device_user_id"], s.isoformat(), e.isoformat(), body.reason.strip()[:200],
                     now_local().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    return {"ok": True, "id": cur.lastrowid, "status": "pending"}


@router.delete("/wfh/{req_id}")
def cancel_wfh(req_id: int, emp: dict = Depends(current_employee)):
    c = db.conn()
    row = c.execute("SELECT status FROM wfh_requests WHERE id=? AND device_user_id=?", (req_id, emp["device_user_id"])).fetchone()
    if not row:
        raise HTTPException(404, "No such request")
    if row["status"] != "pending":
        raise HTTPException(400, "Only a pending request can be cancelled")
    c.execute("DELETE FROM wfh_requests WHERE id=?", (req_id,))
    c.commit()
    return {"ok": True}


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushKeys


@router.get("/push/key")
def push_key(emp: dict = Depends(current_employee)):
    return {"key": push.public_key()}


@router.post("/push/subscribe")
def push_subscribe(sub: PushSubscription, emp: dict = Depends(current_employee)):
    """Turn on check-in / check-out reminders for this phone. A phone belongs to whoever subscribed it last."""
    if not sub.endpoint.startswith("https://") or len(sub.endpoint) > 1000:
        raise HTTPException(400, "Invalid subscription")
    c = db.conn()
    c.execute("INSERT INTO push_subscriptions(device_user_id,endpoint,p256dh,auth,created_at) VALUES (?,?,?,?,?) "
              "ON CONFLICT(endpoint) DO UPDATE SET device_user_id=excluded.device_user_id, p256dh=excluded.p256dh, auth=excluded.auth",
              (emp["device_user_id"], sub.endpoint, sub.keys.p256dh[:200], sub.keys.auth[:100], now_local().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    return {"ok": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(sub: PushSubscription, emp: dict = Depends(current_employee)):
    c = db.conn()
    c.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND device_user_id=?", (sub.endpoint, emp["device_user_id"]))
    c.commit()
    return {"ok": True}
