import asyncio
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime

import os

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .timeutil import now_local

from . import adms, app_api, auth, config, db, fkweb, hub, reports, zk_poller

logging.basicConfig(level=logging.INFO)
security = HTTPBasic(auto_error=False)


def require_admin(cred: HTTPBasicCredentials | None = Depends(security)):
    if not config.ADMIN_PASSWORD:
        return
    if not cred or not secrets.compare_digest(cred.password, config.ADMIN_PASSWORD):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not config.ADMIN_PASSWORD:
        logging.warning('ADMIN_PASSWORD is not set: the admin dashboard is open to anyone who can reach it!')
    hub.bind_loop(asyncio.get_running_loop())
    zk_poller.start()
    fk_server = await fkweb.start()
    yield
    if fk_server:
        fk_server.close()


app = FastAPI(title="Attendance System", lifespan=lifespan)
app.include_router(adms.router)  # device-facing, no auth (devices can't do Basic auth)
app.include_router(app_api.router)  # employee app, token auth
admin = [Depends(require_admin)]



class EmployeeIn(BaseModel):
    device_user_id: str
    name: str
    department: str = ""
    active: bool = True


class DeviceIn(BaseModel):
    name: str = ""
    location: str = ""


class PinIn(BaseModel):
    pin: str


class SiteIn(BaseModel):
    name: str
    lat: float
    lng: float
    radius_m: float = 150


class ReviewIn(BaseModel):
    flag: str  # 'rejected' | 'approved' | ''


class PunchIn(BaseModel):
    device_user_id: str
    ts: str  # "YYYY-MM-DD HH:MM:SS"
    status: int = 0


@app.get("/api/employees", dependencies=admin)
def list_employees():
    return [dict(r) for r in db.conn().execute(
        "SELECT id, device_user_id, name, department, active, pin_hash IS NOT NULL AS pin_hash FROM employees ORDER BY name")]


@app.post("/api/employees", dependencies=admin)
def upsert_employee(e: EmployeeIn):
    c = db.conn()
    c.execute(
        "INSERT INTO employees(device_user_id,name,department,active) VALUES (?,?,?,?) "
        "ON CONFLICT(device_user_id) DO UPDATE SET name=excluded.name, department=excluded.department, active=excluded.active",
        (e.device_user_id, e.name, e.department, int(e.active)),
    )
    c.commit()
    return {"ok": True}


@app.delete("/api/employees/{device_user_id}", dependencies=admin)
def delete_employee(device_user_id: str):
    c = db.conn()
    c.execute("UPDATE employees SET active=0 WHERE device_user_id=?", (device_user_id,))
    c.commit()
    return {"ok": True}


@app.post("/api/employees/{device_user_id}/pin", dependencies=admin)
def set_pin(device_user_id: str, body: PinIn):
    if not auth.valid_pin(body.pin):
        raise HTTPException(400, "PIN must be 4-8 digits")
    c = db.conn()
    cur = c.execute("UPDATE employees SET pin_hash=? WHERE device_user_id=?", (auth.hash_pin(body.pin), device_user_id))
    c.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "No such employee")
    return {"ok": True}


ONLINE_SECONDS = 90  # devices poll every ~3 s, so silence for 90 s means offline


@app.get("/api/devices", dependencies=admin)
def list_devices():
    now, out = now_local(), []
    for r in db.conn().execute("SELECT * FROM devices ORDER BY COALESCE(NULLIF(name,''), dev_id)"):
        d = dict(r)
        d["online"] = bool(d["last_seen"]) and (now - datetime.fromisoformat(d["last_seen"])).total_seconds() <= ONLINE_SECONDS
        out.append(d)
    return out


@app.post("/api/devices/{dev_id}/info", dependencies=admin)
def update_device(dev_id: str, body: DeviceIn):
    c = db.conn()
    cur = c.execute("UPDATE devices SET name=?, location=? WHERE dev_id=?", (body.name, body.location, dev_id))
    c.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Unknown device")
    return {"ok": True}


@app.get("/api/dashboard", dependencies=admin)
def dashboard(day: str | None = None):
    day = day or now_local().date().isoformat()
    rows = reports.daily(day)
    depts: dict[str, int] = {}
    for r in rows:
        if r["status"] != "Absent":
            depts[r["department"] or "Unassigned"] = depts.get(r["department"] or "Unassigned", 0) + 1
    c = db.conn()
    stats = {r["source"]: (r["n"], r["last"]) for r in c.execute(
        "SELECT source, COUNT(*) n, MAX(ts) last FROM punches WHERE method='device' GROUP BY source")}
    devices = []
    for d in list_devices():
        n, last = stats.get(d["dev_id"], (0, None))
        devices.append({**d, "total_logs": n, "last_punch": last})
    gps = [dict(r) for r in c.execute(
        "SELECT p.id, p.ts, p.device_user_id, COALESCE(e.name,'Unknown') name, p.selfie, p.lat, p.lng, p.flag, s.name site "
        "FROM punches p LEFT JOIN employees e ON e.device_user_id=p.device_user_id LEFT JOIN sites s ON s.id=p.site_id "
        "WHERE p.method='app' AND date(p.ts)=? AND COALESCE(p.flag,'')!='rejected' ORDER BY p.ts DESC LIMIT 10", (day,))]
    return {"day": day, "total": len(rows),
            "present": sum(r["status"] != "Absent" for r in rows),
            "absent": sum(r["status"] == "Absent" for r in rows),
            "late": sum(r["status"] == "Late" for r in rows),
            "departments": [{"name": k, "present": v} for k, v in sorted(depts.items())],
            "devices": devices, "gps_punches": gps}


@app.get("/api/sites", dependencies=admin)
def list_sites():
    return [dict(r) for r in db.conn().execute("SELECT * FROM sites WHERE active=1 ORDER BY name")]


@app.post("/api/sites", dependencies=admin)
def add_site(s: SiteIn):
    if not (-90 <= s.lat <= 90 and -180 <= s.lng <= 180 and s.radius_m > 0):
        raise HTTPException(400, "Invalid latitude/longitude/radius")
    c = db.conn()
    c.execute("INSERT INTO sites(name,lat,lng,radius_m) VALUES (?,?,?,?)", (s.name, s.lat, s.lng, s.radius_m))
    c.commit()
    return {"ok": True}


@app.delete("/api/sites/{site_id}", dependencies=admin)
def delete_site(site_id: int):
    c = db.conn()
    c.execute("UPDATE sites SET active=0 WHERE id=?", (site_id,))
    c.commit()
    return {"ok": True}


@app.get("/api/punches", dependencies=admin)
def list_punches(day: str | None = None, limit: int = 500, device: str | None = None):
    day = day or now_local().date().isoformat()
    rows = db.conn().execute(
        "SELECT p.id, p.ts, p.device_user_id, COALESCE(e.name,'Unknown') name, p.status, p.source, p.method, "
        "p.flag, p.selfie, p.lat, p.lng, p.accuracy, p.distance_m, s.name site FROM punches p "
        "LEFT JOIN employees e ON e.device_user_id=p.device_user_id LEFT JOIN sites s ON s.id=p.site_id "
        "WHERE date(p.ts)=? AND (? IS NULL OR p.source=?) ORDER BY p.ts DESC LIMIT ?",
        (day, device, device, limit),
    )
    return [dict(r) for r in rows]


@app.post("/api/punches/{punch_id}/review", dependencies=admin)
def review_punch(punch_id: int, body: ReviewIn):
    if body.flag not in ("rejected", "approved", ""):
        raise HTTPException(400, "flag must be rejected, approved or empty")
    c = db.conn()
    c.execute("UPDATE punches SET flag=? WHERE id=?", (body.flag, punch_id))
    c.commit()
    return {"ok": True}


@app.get("/api/selfies/{year_month}/{filename}", dependencies=admin)
def get_selfie(year_month: str, filename: str):
    base = os.path.realpath(config.SELFIE_DIR)
    path = os.path.realpath(os.path.join(base, year_month, filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/punches", dependencies=admin)
def manual_punch(p: PunchIn):
    rec = db.add_punch(p.device_user_id, p.ts, p.status, "manual")
    if rec:
        hub.publish(rec)
    return {"stored": rec is not None}


@app.get("/api/reports/daily", dependencies=admin)
def report_daily(day: str | None = None, device: str | None = None):
    return reports.daily(day or now_local().date().isoformat(), device)


@app.get("/api/reports/monthly", dependencies=admin)
def report_monthly(month: str | None = None, device: str | None = None):
    return reports.monthly(month or now_local().strftime("%Y-%m"), device)


@app.get("/api/reports/daily.csv", dependencies=admin)
def report_daily_csv(day: str | None = None):
    rows = reports.daily(day or now_local().date().isoformat())
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=attendance.csv"})


@app.get("/api/reports/monthly.csv", dependencies=admin)
def report_monthly_csv(month: str | None = None):
    rows = reports.monthly(month or now_local().strftime("%Y-%m"))
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=attendance-monthly.csv"})


@app.post("/api/devices/sync", dependencies=admin)
def sync_now():
    """Trigger an immediate pull from all configured ZK devices."""
    return {a: zk_poller.sync_device(a) for a in config.ZK_DEVICES}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await hub.register(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.unregister(websocket)


@app.get("/", dependencies=admin)
def index():
    return FileResponse("app/static/index.html")


@app.get("/m")
def mobile_redirect():
    return RedirectResponse("/m/")


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/m", StaticFiles(directory="app/static/m", html=True), name="mobile")  # employee PWA (public shell)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
