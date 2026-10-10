import asyncio
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adms, config, db, fkweb, hub, reports, zk_poller

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
    hub.bind_loop(asyncio.get_running_loop())
    zk_poller.start()
    yield


app = FastAPI(title="Attendance System", lifespan=lifespan)
app.include_router(adms.router)  # device-facing, no auth (devices can't do Basic auth)
admin = [Depends(require_admin)]


@app.middleware("http")
async def fkweb_middleware(request, call_next):
    # FkWeb devices (Realtime/FK series) POST to arbitrary paths with a request_code header.
    if "request_code" in request.headers:
        return await fkweb.handle(request)
    return await call_next(request)


class EmployeeIn(BaseModel):
    device_user_id: str
    name: str
    department: str = ""
    active: bool = True


class PunchIn(BaseModel):
    device_user_id: str
    ts: str  # "YYYY-MM-DD HH:MM:SS"
    status: int = 0


@app.get("/api/employees", dependencies=admin)
def list_employees():
    return [dict(r) for r in db.conn().execute("SELECT * FROM employees ORDER BY name")]


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


@app.get("/api/punches", dependencies=admin)
def list_punches(day: str | None = None, limit: int = 200):
    day = day or date.today().isoformat()
    rows = db.conn().execute(
        "SELECT p.ts, p.device_user_id, COALESCE(e.name,'Unknown') name, p.status, p.source FROM punches p "
        "LEFT JOIN employees e ON e.device_user_id=p.device_user_id WHERE date(p.ts)=? ORDER BY p.ts DESC LIMIT ?",
        (day, limit),
    )
    return [dict(r) for r in rows]


@app.post("/api/punches", dependencies=admin)
def manual_punch(p: PunchIn):
    rec = db.add_punch(p.device_user_id, p.ts, p.status, "manual")
    if rec:
        hub.publish(rec)
    return {"stored": rec is not None}


@app.get("/api/reports/daily", dependencies=admin)
def report_daily(day: str | None = None):
    return reports.daily(day or date.today().isoformat())


@app.get("/api/reports/monthly", dependencies=admin)
def report_monthly(month: str | None = None):
    return reports.monthly(month or date.today().strftime("%Y-%m"))


@app.get("/api/reports/daily.csv", dependencies=admin)
def report_daily_csv(day: str | None = None):
    rows = reports.daily(day or date.today().isoformat())
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=attendance.csv"})


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


app.mount("/static", StaticFiles(directory="app/static"), name="static")
