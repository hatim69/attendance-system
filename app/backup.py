"""Nightly backup: the whole database plus employee documents, zipped (AES-encrypted when BACKUP_PASSWORD is set),
kept on the server for BACKUP_KEEP_DAYS days and emailed to BACKUP_EMAIL_TO.

Selfie photos are not included (they grow large); they stay in the data folder on the server."""
import asyncio
import json
import logging
import os
import re
import smtplib
import sqlite3
import tempfile
import zipfile
from datetime import timedelta
from email.message import EmailMessage

import pyzipper

from . import config, db
from .timeutil import now_local

log = logging.getLogger("backup")
NAME_RE = re.compile(r"attendance-backup-\d{4}-\d{2}-\d{2}(-\d{6})?\.zip")
MAX_ATTACH = 20 * 1024 * 1024   # most mail servers refuse attachments above ~25 MB
README = """Attendance system backup
========================
attendance.db   the whole database (employees, punches, leave, payroll, users, audit log ...)
documents/      employee documents uploaded in the dashboard

To restore on the server:
  cd /opt/attendance-system && docker compose -f docker-compose.nginx.yml stop app
  copy attendance.db to data/attendance.db and the documents folder to data/documents
  docker compose -f docker-compose.nginx.yml start app
"""


def status() -> dict:
    row = db.conn().execute("SELECT value FROM settings WHERE key='backup_last'").fetchone()
    return json.loads(row["value"]) if row else {}


def _save_status(st: dict):
    c = db.conn()
    c.execute("INSERT INTO settings(key,value) VALUES ('backup_last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (json.dumps(st),))
    c.commit()


def email_configured() -> bool:
    return bool(config.BACKUP_EMAIL_TO and config.SMTP_HOST)


def _zip(path: str, files: list[tuple[str, str]], texts: dict[str, str]):
    if config.BACKUP_PASSWORD:
        z = pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES)
        z.setpassword(config.BACKUP_PASSWORD.encode())
    else:
        z = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED)
    with z:
        for arc, src in files:
            z.write(src, arc)
        for arc, text in texts.items():
            z.writestr(arc, text)


def make(now=None) -> dict:
    """Create today's backup file. Returns {"file", "path", "size", "db_only_path"}."""
    now = now or now_local()
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "attendance.db")
        src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(snap)
        with dst:
            src.backup(dst)          # consistent copy even while the app is writing
        src.close()
        dst.close()
        docs = []
        if os.path.isdir(config.DOCS_DIR):
            for root, _, names in os.walk(config.DOCS_DIR):
                for n in names:
                    full = os.path.join(root, n)
                    docs.append(("documents/" + os.path.relpath(full, config.DOCS_DIR).replace(os.sep, "/"), full))
        name = f"attendance-backup-{now:%Y-%m-%d}.zip"
        path = os.path.join(config.BACKUP_DIR, name)
        _zip(path, [("attendance.db", snap)] + docs, {"README.txt": README})
        out = {"file": name, "path": path, "size": os.path.getsize(path), "db_only_path": None}
        if out["size"] > MAX_ATTACH:   # too big to email: send the database alone
            small = os.path.join(config.BACKUP_DIR, f".email-{now:%Y-%m-%d}.zip")
            _zip(small, [("attendance.db", snap)], {"README.txt": README + "\n(Documents were too large to email; "
                                                     "they are in the full backup on the server.)\n"})
            out["db_only_path"] = small
    return out


def prune(now=None):
    now = now or now_local()
    cutoff = (now - timedelta(days=config.BACKUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    for n in os.listdir(config.BACKUP_DIR) if os.path.isdir(config.BACKUP_DIR) else []:
        m = re.match(r"attendance-backup-(\d{4}-\d{2}-\d{2})", n)
        if (m and m.group(1) < cutoff) or n.startswith(".email-"):
            try:
                os.remove(os.path.join(config.BACKUP_DIR, n))
            except OSError:
                pass


def send_email(attachment: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = config.BACKUP_EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    with open(attachment, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="zip", filename=os.path.basename(attachment).lstrip(".").replace("email-", "attendance-backup-"))
    if config.SMTP_PORT == 465:
        s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
    else:
        s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60)
        s.starttls()
    with s:
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def run(now=None, email: bool = True) -> dict:
    """Make the backup, email it, clean up old ones, and remember how it went."""
    now = now or now_local()
    st = {"date": now.strftime("%Y-%m-%d"), "at": now.strftime("%Y-%m-%d %H:%M:%S"), "file": "", "size": 0,
          "encrypted": bool(config.BACKUP_PASSWORD), "emailed_to": "", "error": ""}
    try:
        b = make(now)
        st.update(file=b["file"], size=b["size"])
        if email and email_configured():
            c = db.conn()
            counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("employees", "punches", "leaves")}
            body = (f"Nightly backup of the attendance system ({config.COMPANY_NAME or 'attendance'}) for {now:%d %b %Y}.\n\n"
                    f"Employees: {counts['employees']} · punches: {counts['punches']} · leave records: {counts['leaves']}\n"
                    f"File: {b['file']} ({b['size'] / 1024 / 1024:.1f} MB)\n\n"
                    + ("Open it with 7-Zip or WinRAR using the backup password.\n" if config.BACKUP_PASSWORD else
                       "WARNING: this backup is not password protected. Set BACKUP_PASSWORD on the server.\n")
                    + ("The documents were too large to attach; this email has the database only.\n" if b["db_only_path"] else "")
                    + "\nThe last " + str(config.BACKUP_KEEP_DAYS) + " days of backups are also kept on the server.")
            send_email(b["db_only_path"] or b["path"], f"Attendance backup {now:%d %b %Y}", body)
            st["emailed_to"] = config.BACKUP_EMAIL_TO
        prune(now)
    except Exception as e:
        log.exception("backup failed")
        st["error"] = f"{type(e).__name__}: {e}"[:300]
    _save_status(st)
    return st


def list_files() -> list[dict]:
    if not os.path.isdir(config.BACKUP_DIR):
        return []
    out = []
    for n in sorted(os.listdir(config.BACKUP_DIR), reverse=True):
        if NAME_RE.fullmatch(n):
            p = os.path.join(config.BACKUP_DIR, n)
            out.append({"file": n, "size": os.path.getsize(p)})
    return out


async def loop():
    """Once a night, at BACKUP_HOUR local time or as soon after as the server is running."""
    while True:
        try:
            now = now_local()
            if now.hour >= config.BACKUP_HOUR and status().get("date") != now.strftime("%Y-%m-%d"):
                await asyncio.to_thread(run, now)
        except Exception:
            log.exception("backup check failed")
        await asyncio.sleep(300)
