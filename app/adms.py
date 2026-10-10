"""Push mode: ZKTeco 'ADMS' / iClock protocol. Point the device's Cloud Server setting at this app
(Server address = this machine's IP, port = app port). The device then POSTs logs here in real time."""
from fastapi import APIRouter, Request, Response

from . import db, hub

router = APIRouter(prefix="/iclock")


def _text(s: str) -> Response:
    return Response(content=s, media_type="text/plain")


@router.get("/cdata")
async def handshake(SN: str = ""):
    # Tells the device how to behave; Realtime=1 makes it push each punch immediately.
    return _text(
        f"GET OPTION FROM: {SN}\nATTLOGStamp=None\nOPERLOGStamp=9999\nATTPHOTOStamp=None\n"
        "ErrorDelay=30\nDelay=10\nTransTimes=00:00;14:00\nTransInterval=1\n"
        "TransFlag=TransData AttLog\nRealtime=1\nEncrypt=0\n"
    )


@router.post("/cdata")
async def receive(request: Request, SN: str = "", table: str = ""):
    body = (await request.body()).decode("utf-8", "ignore")
    count = 0
    if table.upper() == "ATTLOG":
        # Each line: PIN \t YYYY-MM-DD HH:MM:SS \t status \t verify \t ...
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            rec = db.add_punch(parts[0].strip(), parts[1].strip(), int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0, SN)
            if rec:
                count += 1
                hub.publish(rec)
    elif table.upper() == "OPERLOG":
        # User records look like: USER PIN=1\tName=Alice\t...
        c = db.conn()
        for line in body.splitlines():
            if line.startswith("USER "):
                f = dict(p.split("=", 1) for p in line[5:].split("\t") if "=" in p)
                if f.get("PIN"):
                    c.execute(
                        "INSERT INTO employees(device_user_id, name) VALUES (?,?) ON CONFLICT(device_user_id) DO NOTHING",
                        (db.canon_id(f["PIN"]), f.get("Name") or f"User {f['PIN']}"),
                    )
        c.commit()
    return _text(f"OK:{count}")


@router.get("/getrequest")
async def get_request(SN: str = ""):
    return _text("OK")  # no commands queued for the device


@router.post("/devicecmd")
async def device_cmd():
    return _text("OK")
