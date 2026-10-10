"""Pull mode: periodically read attendance logs from ZKTeco devices over TCP (port 4370) using pyzk."""
import logging
import threading
import time
from datetime import datetime, timedelta

from . import config, db, hub

log = logging.getLogger("zk")


def parse_addr(s: str) -> tuple[str, int]:
    host, _, port = s.partition(":")
    return host, int(port or 4370)


def sync_device(addr: str) -> int:
    """Connect once, import users + new attendance. Returns number of new punches."""
    from zk import ZK  # imported lazily so the app runs without pyzk when unused

    host, port = parse_addr(addr)
    zk = ZK(host, port=port, timeout=10, password=config.ZK_PASSWORD, force_udp=False, ommit_ping=True)
    conn = zk.connect()
    new = 0
    try:
        conn.disable_device()
        try:
            c = db.conn()
            for u in conn.get_users():
                c.execute(
                    "INSERT INTO employees(device_user_id, name) VALUES (?,?) "
                    "ON CONFLICT(device_user_id) DO NOTHING",
                    (db.canon_id(u.user_id), u.name or f"User {u.user_id}"),
                )
            c.commit()
            recent = datetime.now() - timedelta(minutes=5)
            for a in conn.get_attendance():
                rec = db.add_punch(str(a.user_id), a.timestamp, a.punch, addr)
                if rec:
                    new += 1
                    if a.timestamp >= recent:  # don't flood the live feed with history
                        hub.publish(rec)
        finally:
            conn.enable_device()
    finally:
        conn.disconnect()
    return new


def _loop(addr: str):
    while True:
        try:
            n = sync_device(addr)
            if n:
                log.info("%s: %d new punches", addr, n)
        except Exception as e:  # device offline, network blip, etc. -> retry next cycle
            log.warning("%s: sync failed: %s", addr, e)
        time.sleep(config.ZK_POLL_SECONDS)


def start():
    for addr in config.ZK_DEVICES:
        threading.Thread(target=_loop, args=(addr,), daemon=True, name=f"zk-{addr}").start()
        log.info("polling ZK device %s every %ss", addr, config.ZK_POLL_SECONDS)
