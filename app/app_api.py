"""Employee-facing API for the mobile web app: login with ID + PIN, then GPS + selfie punches."""
import os
import re

from fastapi.responses import Response
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from . import auth, config, db, geo, hub, leavepolicy, payslip, reports
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


def active_sites() -> list[dict]:
    return [dict(r) for r in db.conn().execute("SELECT id,name,lat,lng,radius_m FROM sites WHERE active=1")]


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
            "sites": active_sites(), "geofence_mode": config.GEOFENCE_MODE, "server_time": now_local().isoformat(" ")}


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

    sites = active_sites()
    site, dist, inside = geo.nearest_site(lat, lng, accuracy, sites)
    flag = ""
    if sites and not inside:
        if config.GEOFENCE_MODE == "block":
            raise HTTPException(403, f"You are about {int(dist)} m from {site['name']}. Move closer to punch.")
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
                       distance_m=dist, flag=flag)
    if rec is None:
        raise HTTPException(409, "You already punched a moment ago")
    with open(path, "wb") as f:
        f.write(data)
    hub.publish(rec)
    return {"ok": True, "time": rec["ts"], "type": "Check out" if count % 2 else "Check in",
            "site": site["name"] if site and inside else None, "flag": flag}


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
    end_date: str
    type: str          # Normal | Adhoc
    reason: str = ""


@router.get("/leave-balance")
def my_leave_balance(emp: dict = Depends(current_employee)):
    return leavepolicy.balance(emp["device_user_id"], now_local().date())


@router.get("/leaves")
def my_leaves(emp: dict = Depends(current_employee)):
    rows = db.conn().execute(
        "SELECT id, start_date, end_date, type, note, status, applied_on FROM leaves WHERE device_user_id=? "
        "AND end_date >= date('now','-400 day') ORDER BY start_date DESC LIMIT 50", (emp["device_user_id"],))
    return [dict(r) for r in rows]


@router.post("/leave")
def apply_leave(body: LeaveRequest, emp: dict = Depends(current_employee)):
    """Apply for Normal or Adhoc leave. It stays pending (and reserves the balance) until HR approves it."""
    today = now_local().date()
    check = leavepolicy.validate(emp["device_user_id"], body.type, body.start_date, body.end_date, today, strict=True)
    if "error" in check:
        raise HTTPException(400, check["error"])
    c = db.conn()
    cur = c.execute("INSERT INTO leaves(device_user_id,start_date,end_date,type,note,status,applied_on,requested_by) "
                    "VALUES (?,?,?,?,?, 'pending', ?, ?)",
                    (emp["device_user_id"], body.start_date, body.end_date, body.type, body.reason[:200], today.isoformat(),
                     emp["device_user_id"]))
    c.commit()
    return {"ok": True, "id": cur.lastrowid, "days": len(check["days"]), "status": "pending"}


@router.delete("/leave/{leave_id}")
def cancel_leave(leave_id: int, emp: dict = Depends(current_employee)):
    c = db.conn()
    row = c.execute("SELECT status FROM leaves WHERE id=? AND device_user_id=? AND requested_by=?",
                    (leave_id, emp["device_user_id"], emp["device_user_id"])).fetchone()
    if not row:
        raise HTTPException(404, "No such leave application")
    if row["status"] != "pending":
        raise HTTPException(400, "Only a pending application can be cancelled. Ask HR about approved leave.")
    c.execute("DELETE FROM leaves WHERE id=?", (leave_id,))
    c.commit()
    return {"ok": True}
