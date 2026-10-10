"""FkWeb push mode (Realtime / FK-series devices): the device acts as an HTTP client and POSTs to the
configured 'Webserver URL'. Requests carry headers like request_code / dev_id / trans_id / blk_no.

Every request is logged raw to fkweb_raw.log so the exact format can be verified against your device;
punch parsing below is best-effort (JSON or key=value bodies) until confirmed with real traffic."""
import json
import logging
import re
from datetime import datetime

from fastapi import Request, Response

from . import db, hub

log = logging.getLogger("fkweb")
RAW_LOG = "fkweb_raw.log"
_TS = re.compile(r"(\d{4})[-/.](\d{2})[-/.](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_ID_KEYS = ("userid", "user_id", "enrollno", "enroll_number", "enrollnumber", "pin", "id")
_TIME_KEYS = ("time", "datetime", "timestamp", "verifytime", "log_time", "date_time")


def _dump(request: Request, body: bytes):
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "method": request.method,
           "path": request.url.path, "query": str(request.url.query),
           "headers": dict(request.headers), "body_len": len(body),
           "body_text": body[:2000].decode("utf-8", "replace"), "body_hex": body[:256].hex()}
    with open(RAW_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    log.info("FkWeb %s %s request_code=%s dev=%s len=%d", request.method, request.url.path,
             request.headers.get("request_code"), request.headers.get("dev_id"), len(body))


def _find_punch(body: bytes) -> tuple[str, str] | None:
    """Best-effort: pull (user_id, 'YYYY-MM-DD HH:MM:SS') out of a JSON or text body."""
    text = body.decode("utf-8", "ignore").strip("\x00 \r\n")
    flat: dict[str, str] = {}
    try:
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v) if isinstance(v, (dict, list)) else flat.setdefault(str(k).lower(), str(v))
            elif isinstance(o, list):
                for i in o:
                    walk(i)
        walk(json.loads(text))
    except ValueError:
        for k, v in re.findall(r"([A-Za-z_]+)\s*[=:]\s*\"?([^\",;\s]+(?: \d\d:\d\d:\d\d)?)", text):
            flat.setdefault(k.lower(), v)
    uid = next((flat[k] for k in _ID_KEYS if k in flat), None)
    m = next((_TS.search(flat[k]) for k in _TIME_KEYS if k in flat and _TS.search(flat[k])), None) or _TS.search(text)
    if uid and m:
        return uid, "%s-%s-%s %s:%s:%s" % m.groups()
    return None


async def handle(request: Request) -> Response:
    body = await request.body()
    _dump(request, body)
    code = request.headers.get("request_code", "")
    if code.startswith("realtime_glog") or code.startswith("receive_glog"):
        p = _find_punch(body)
        if p:
            rec = db.add_punch(p[0], p[1], 0, request.headers.get("dev_id", "fkweb"))
            if rec:
                hub.publish(rec)
    # Acknowledge so the device marks the record as delivered; echo ids it sent.
    headers = {"response_code": "OK", "trans_id": request.headers.get("trans_id", "0"),
               "cmd_id": "0", "Content-Length": "0"}
    return Response(status_code=200, headers=headers)
