import asyncio
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime

import os
import re

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .timeutil import now_local

from . import adms, app_api, auth, config, db, fkweb, hub, importer, reports, zk_poller

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
    shift_id: int | None = None
    salary: float = 0
    joined: str = ""  # optional YYYY-MM-DD; days before it are not counted


class ShiftIn(BaseModel):
    id: int | None = None
    name: str
    start: str
    end: str
    grace_min: int = 10
    half_day_hours: float = 4
    ot_after_min: int = 30
    weekly_off: str = "SUN"  # comma list of MON..SUN, empty for none


class HolidayIn(BaseModel):
    date: str
    name: str


class LeaveIn(BaseModel):
    device_user_id: str
    start_date: str
    end_date: str
    type: str = "Casual"
    note: str = ""


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
        "SELECT e.id, e.device_user_id, e.name, e.department, e.active, e.pin_hash IS NOT NULL AS pin_hash, e.shift_id, "
        "e.salary, COALESCE(e.joined,'') joined, COALESCE(s.name,'') shift FROM employees e LEFT JOIN shifts s ON s.id=e.shift_id ORDER BY e.name")]


@app.post("/api/employees", dependencies=admin)
def upsert_employee(e: EmployeeIn):
    e.device_user_id = db.canon_id(e.device_user_id)
    if not e.device_user_id:
        raise HTTPException(400, "Employee ID is required")
    if e.joined:
        _valid_date(e.joined)
    c = db.conn()
    c.execute(
        "INSERT INTO employees(device_user_id,name,department,active,shift_id,salary,joined) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(device_user_id) DO UPDATE SET name=excluded.name, department=excluded.department, "
        "active=excluded.active, shift_id=excluded.shift_id, salary=excluded.salary, joined=excluded.joined",
        (e.device_user_id, e.name, e.department, int(e.active), e.shift_id, e.salary, e.joined or None),
    )
    c.commit()
    return {"ok": True}


@app.delete("/api/employees/{device_user_id}", dependencies=admin)
def delete_employee(device_user_id: str):
    device_user_id = db.canon_id(device_user_id)
    c = db.conn()
    c.execute("UPDATE employees SET active=0 WHERE device_user_id=?", (device_user_id,))
    c.commit()
    return {"ok": True}


@app.post("/api/employees/{device_user_id}/pin", dependencies=admin)
def set_pin(device_user_id: str, body: PinIn):
    device_user_id = db.canon_id(device_user_id)
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
        if r["status"] in reports.WORKED:
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
            "present": sum(r["status"] in reports.WORKED for r in rows),
            "absent": sum(r["status"] == "Absent" for r in rows),
            "late": sum(r["status"] == "Late" for r in rows),
            "departments": [{"name": k, "present": v} for k, v in sorted(depts.items())],
            "devices": devices, "gps_punches": gps}


def _valid_date(d: str):
    try:
        date.fromisoformat(d)
    except ValueError:
        raise HTTPException(400, f"Invalid date {d!r} (use YYYY-MM-DD)")


@app.get("/api/shifts", dependencies=admin)
def list_shifts():
    return [dict(r) for r in db.conn().execute(
        "SELECT s.*, (SELECT COUNT(*) FROM employees e WHERE e.shift_id=s.id AND e.active=1) employees FROM shifts s ORDER BY s.name")]


@app.post("/api/shifts", dependencies=admin)
def save_shift(sh: ShiftIn):
    if not (re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", sh.start) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", sh.end)):
        raise HTTPException(400, "Times must be HH:MM (24-hour)")
    if sh.end <= sh.start:
        raise HTTPException(400, "Shift end must be after start (overnight shifts are not supported yet)")
    offs = [o.strip().upper() for o in sh.weekly_off.split(",") if o.strip()]
    if any(o not in reports.WEEKDAYS for o in offs):
        raise HTTPException(400, "Weekly off must be a comma list of MON,TUE,WED,THU,FRI,SAT,SUN")
    c = db.conn()
    vals = (sh.name.strip(), sh.start, sh.end, sh.grace_min, sh.half_day_hours, sh.ot_after_min, ",".join(offs))
    if sh.id:
        c.execute("UPDATE shifts SET name=?, start=?, end=?, grace_min=?, half_day_hours=?, ot_after_min=?, weekly_off=? WHERE id=?",
                  vals + (sh.id,))
    else:
        c.execute("INSERT INTO shifts(name,start,end,grace_min,half_day_hours,ot_after_min,weekly_off) VALUES (?,?,?,?,?,?,?)", vals)
    c.commit()
    return {"ok": True}


@app.delete("/api/shifts/{shift_id}", dependencies=admin)
def delete_shift(shift_id: int):
    c = db.conn()
    c.execute("UPDATE employees SET shift_id=NULL WHERE shift_id=?", (shift_id,))
    c.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
    c.commit()
    return {"ok": True}


@app.get("/api/holidays", dependencies=admin)
def list_holidays(year: str | None = None):
    year = year or now_local().strftime("%Y")
    return [dict(r) for r in db.conn().execute("SELECT * FROM holidays WHERE substr(date,1,4)=? ORDER BY date", (year,))]


@app.post("/api/holidays", dependencies=admin)
def save_holiday(h: HolidayIn):
    _valid_date(h.date)
    c = db.conn()
    c.execute("INSERT INTO holidays(date,name) VALUES (?,?) ON CONFLICT(date) DO UPDATE SET name=excluded.name", (h.date, h.name))
    c.commit()
    return {"ok": True}


@app.delete("/api/holidays/{day}", dependencies=admin)
def delete_holiday(day: str):
    c = db.conn()
    c.execute("DELETE FROM holidays WHERE date=?", (day,))
    c.commit()
    return {"ok": True}


@app.get("/api/leaves", dependencies=admin)
def list_leaves(month: str | None = None):
    month = month or now_local().strftime("%Y-%m")
    return [dict(r) for r in db.conn().execute(
        "SELECT l.*, COALESCE(e.name, l.device_user_id) name FROM leaves l LEFT JOIN employees e ON e.device_user_id=l.device_user_id "
        "WHERE substr(l.start_date,1,7)<=? AND substr(l.end_date,1,7)>=? ORDER BY l.start_date DESC", (month, month))]


@app.post("/api/leaves", dependencies=admin)
def add_leave(l: LeaveIn):
    l.device_user_id = db.canon_id(l.device_user_id)
    _valid_date(l.start_date)
    _valid_date(l.end_date)
    if l.end_date < l.start_date:
        raise HTTPException(400, "End date is before start date")
    if l.type not in reports.LEAVE_TYPES:
        raise HTTPException(400, "Leave type must be one of " + ", ".join(reports.LEAVE_TYPES))
    c = db.conn()
    if not c.execute("SELECT 1 FROM employees WHERE device_user_id=?", (l.device_user_id,)).fetchone():
        raise HTTPException(404, "No such employee")
    c.execute("INSERT INTO leaves(device_user_id,start_date,end_date,type,note) VALUES (?,?,?,?,?)",
              (l.device_user_id, l.start_date, l.end_date, l.type, l.note))
    c.commit()
    return {"ok": True}


@app.delete("/api/leaves/{leave_id}", dependencies=admin)
def delete_leave(leave_id: int):
    c = db.conn()
    c.execute("DELETE FROM leaves WHERE id=?", (leave_id,))
    c.commit()
    return {"ok": True}


MAX_IMPORT_BYTES = 15 * 1024 * 1024


@app.post("/api/import/{kind}", dependencies=admin)
async def import_file(kind: str, file: UploadFile = File(...), dry_run: bool = True):
    data = await file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "File too large (max 15 MB)")
    try:
        return importer.run(kind, file.filename or "", data, dry_run)
    except importer.ImportError_ as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # corrupt xlsx etc.: report, never a stack trace
        logging.exception("import failed")
        raise HTTPException(400, f"Could not read the file: {type(e).__name__}")


@app.get("/api/import/{kind}/template.csv", dependencies=admin)
def import_template(kind: str):
    if kind not in importer.TEMPLATES:
        raise HTTPException(404)
    return PlainTextResponse(importer.template_csv(kind), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={kind}-template.csv"})


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


def _safe(v):
    """Stop spreadsheet formula injection: a text cell starting with = + - @ would be executed by Excel."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def _csv(rows: list[dict], filename: str) -> PlainTextResponse:
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows([{k: _safe(v) for k, v in r.items()} for r in rows])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


def _xlsx(rows: list[dict], filename: str, title: str = "Report") -> Response:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    if rows:
        ws.append(list(rows[0].keys()))
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([_safe(v) for v in r.values()])
        for col in ws.columns:  # readable column widths
            ws.column_dimensions[col[0].column_letter].width = min(40, max(len(str(c.value or "")) for c in col) + 2)
        ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _grid_rows(g: dict) -> list[dict]:
    return [{"ID": r["device_user_id"], "Name": r["name"], "Department": r["department"],
             **{str(d): c for d, c in zip(g["days"], r["codes"])}, **{k: v for k, v in r["totals"].items()}} for r in g["rows"]]


def _day(day): return day or now_local().date().isoformat()
def _month(month): return month or now_local().strftime("%Y-%m")


@app.get("/api/reports/daily", dependencies=admin)
def report_daily(day: str | None = None, device: str | None = None):
    return reports.daily(_day(day), device)


@app.get("/api/reports/daily.csv", dependencies=admin)
def report_daily_csv(day: str | None = None, device: str | None = None):
    return _csv(reports.daily(_day(day), device), "attendance-daily.csv")


@app.get("/api/reports/monthly", dependencies=admin)
def report_monthly(month: str | None = None, device: str | None = None):
    return reports.monthly(_month(month), device)


@app.get("/api/reports/monthly.csv", dependencies=admin)
def report_monthly_csv(month: str | None = None, device: str | None = None):
    return _csv(reports.monthly(_month(month), device), "attendance-monthly.csv")


@app.get("/api/reports/grid", dependencies=admin)
def report_grid(month: str | None = None, device: str | None = None):
    return reports.grid(_month(month), device)


@app.get("/api/reports/grid.csv", dependencies=admin)
def report_grid_csv(month: str | None = None, device: str | None = None):
    return _csv(_grid_rows(reports.grid(_month(month), device)), "attendance-sheet.csv")


@app.get("/api/reports/daily.xlsx", dependencies=admin)
def report_daily_xlsx(day: str | None = None, device: str | None = None):
    return _xlsx(reports.daily(_day(day), device), "attendance-daily.xlsx", "Daily")


@app.get("/api/reports/monthly.xlsx", dependencies=admin)
def report_monthly_xlsx(month: str | None = None, device: str | None = None):
    return _xlsx(reports.monthly(_month(month), device), "attendance-monthly.xlsx", "Monthly")


@app.get("/api/reports/grid.xlsx", dependencies=admin)
def report_grid_xlsx(month: str | None = None, device: str | None = None):
    return _xlsx(_grid_rows(reports.grid(_month(month), device)), "attendance-sheet.xlsx", "Sheet")


@app.get("/api/reports/payroll.xlsx", dependencies=admin)
def report_payroll_xlsx(month: str | None = None, device: str | None = None):
    return _xlsx(reports.payroll(_month(month), device), "payroll.xlsx", "Payroll")


@app.get("/api/reports/muster", dependencies=admin)
def report_muster(month: str | None = None, device: str | None = None):
    return reports.muster(_month(month), device)


@app.get("/api/reports/muster.csv", dependencies=admin)
def report_muster_csv(month: str | None = None, device: str | None = None):
    return _csv(reports.muster(_month(month), device), "muster-roll.csv")


@app.get("/api/reports/muster.xlsx", dependencies=admin)
def report_muster_xlsx(month: str | None = None, device: str | None = None):
    return _xlsx(reports.muster(_month(month), device), "muster-roll.xlsx", "Muster Roll")


@app.get("/api/reports/timecard", dependencies=admin)
def report_timecard(employee: str, month: str | None = None):
    return reports.timecard(_month(month), db.canon_id(employee))


@app.get("/api/reports/timecard.xlsx", dependencies=admin)
def report_timecard_xlsx(employee: str, month: str | None = None):
    return _xlsx(reports.timecard(_month(month), db.canon_id(employee)), "time-card.xlsx", "Time Card")


@app.get("/api/reports/payroll", dependencies=admin)
def report_payroll(month: str | None = None, device: str | None = None):
    return reports.payroll(_month(month), device)


@app.get("/api/reports/payroll.csv", dependencies=admin)
def report_payroll_csv(month: str | None = None, device: str | None = None):
    return _csv(reports.payroll(_month(month), device), "payroll.csv")


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
