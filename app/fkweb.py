"""FkWeb receiver for Realtime / FK-series terminals (verified against an M61BH 'T304F mini' capture).

The device acts as an HTTP client but sends a non-standard request line (`POST http://<ip> HTTP/1.0`) that
normal web servers reject, so this is a small raw TCP server run inside the app. Each request has headers
(request_code, dev_id, trans_id, Content-Length) and a body = 4-byte little-endian length + JSON.

  realtime_glog : a punch -> {"user_id": "00217228", "io_mode": 1, "io_time": "20261010140444", ...}
  receive_cmd   : the device polling for commands every ~3s (we have none)

Every request is also appended to fkweb_raw.log for debugging."""
import asyncio
import json
import logging
import re
from datetime import datetime

from . import config, db, hub
from .timeutil import now_local

log = logging.getLogger("fkweb")
MAX_BODY = 1 << 20
bound_port: int | None = None


def _raw(rec: dict):
    rec["at"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(config.RAW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def parse_body(body: bytes) -> dict:
    """Strip the 4-byte little-endian length prefix (if present) and decode the JSON."""
    if len(body) >= 4 and int.from_bytes(body[:4], "little") == len(body) - 4:
        body = body[4:]
    try:
        data = json.loads(body.decode("utf-8", "ignore").strip("\x00 \r\n") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def record_punch(dev_id: str, data: dict) -> dict | None:
    uid, t = str(data.get("user_id", "")).strip(), str(data.get("io_time", ""))
    if not uid or not re.fullmatch(r"\d{14}", t):
        return None
    ts = datetime.strptime(t, "%Y%m%d%H%M%S")
    c = db.conn()
    # The device only reports ids, not names; create a placeholder so reports include the person.
    c.execute("INSERT OR IGNORE INTO employees(device_user_id, name) VALUES (?,?)", (uid, uid))
    c.commit()
    rec = db.add_punch(uid, ts, int(data.get("io_mode", 0) or 0), dev_id)
    if rec:
        hub.publish(rec)
    return rec


def respond(headers: dict, status: str = "OK") -> bytes:
    return (f"HTTP/1.1 200 OK\r\nresponse_code: {status}\r\ntrans_id: {headers.get('trans_id', '0')}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
        n = int(headers.get("content-length", "0") or 0)
        body = await asyncio.wait_for(reader.readexactly(n), timeout=10) if 0 < n <= MAX_BODY else b""
        code = headers.get("request_code", "")
        data = parse_body(body)
        dev = headers.get("dev_id", "")
        if dev and (not config.FKWEB_DEVICES or dev in config.FKWEB_DEVICES):
            info = data.get("fk_info") if isinstance(data.get("fk_info"), dict) else {}
            db.touch_device(dev, now_local().strftime("%Y-%m-%d %H:%M:%S"), str(data.get("fk_name", "")),
                            str(info.get("firmware", "")))
        if code != "receive_cmd":  # keep-alive polling every ~3s would flood the log
            _raw({"peer": (writer.get_extra_info("peername") or ("?",))[0], "request_line": lines[0],
                  "request_code": code, "dev_id": headers.get("dev_id"), "data": data})
        if code == "realtime_glog":
            if config.FKWEB_DEVICES and headers.get("dev_id") not in config.FKWEB_DEVICES:
                log.warning("punch from unknown device %r ignored", headers.get("dev_id"))
                writer.write(respond(headers))
                await writer.drain()
                return
            rec = record_punch(headers.get("dev_id", "fkweb"), data)
            log.info("punch %s %s", data.get("user_id"), "stored" if rec else "duplicate/ignored")
            writer.write(respond(headers))
        elif not code and config.PUBLIC_DOMAIN:
            # A person opened http://<domain>/ in a browser: send them to the HTTPS site
            path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
            if not path.startswith("/"):
                path = "/" + path.split("/", 3)[-1] if path.count("/") >= 3 else "/"
            writer.write(f"HTTP/1.1 301 Moved Permanently\r\nLocation: https://{config.PUBLIC_DOMAIN}{path}\r\n"
                         "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        elif code == "receive_cmd":
            writer.write(respond(headers, "ERROR_NO_CMD"))  # nothing to send; also the device's keep-alive
        else:
            log.info("FkWeb request_code=%s ignored", code)
            writer.write(respond(headers))
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def start() -> asyncio.AbstractServer | None:
    global bound_port
    if config.FKWEB_PORT < 0:
        return None
    try:
        server = await asyncio.start_server(_handle, "0.0.0.0", config.FKWEB_PORT)
    except OSError as e:  # e.g. port 80 taken -> keep the rest of the app alive
        log.error("FkWeb listener could not bind port %s: %s", config.FKWEB_PORT, e)
        return None
    bound_port = server.sockets[0].getsockname()[1]
    log.info("FkWeb listener on port %s", bound_port)
    return server
