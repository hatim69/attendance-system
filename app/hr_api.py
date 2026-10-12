"""Admin API: employee documents, department / designation pick lists, loans & advances, and the shift roster."""
import math
import os
import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import access, config, db, loans
from .timeutil import now_local

router = APIRouter(prefix="/api")
MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def _stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def _employee(uid: str) -> str:
    uid = db.canon_id(uid)
    if not db.conn().execute("SELECT 1 FROM employees WHERE device_user_id=?", (uid,)).fetchone():
        raise HTTPException(404, "No such employee")
    access.assert_in_scope(uid)
    return uid


# ---------------------------------------------------------------- documents
DOC_TYPES = ["Aadhaar card", "PAN card", "Photo", "Resume", "Offer letter", "Appointment letter", "Address proof",
             "Bank details", "Education certificate", "Experience letter", "Resignation letter", "Other"]
# file type is decided from the content, never from the name the uploader chose
MAGIC = [(b"%PDF", "pdf", "application/pdf"), (b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"\x89PNG", "png", "image/png"),
         (b"PK\x03\x04", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
         (b"\xd0\xcf\x11\xe0", "doc", "application/msword")]


def _kind(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return next(((ext, mime) for magic, ext, mime in MAGIC if data.startswith(magic)), None)


@router.get("/document-types", dependencies=[Depends(access.need("hr", "employees"))])
def document_types():
    return DOC_TYPES


@router.get("/employees/{uid}/documents", dependencies=[Depends(access.need("hr", "employees"))])
def list_documents(uid: str):
    uid = _employee(uid)
    return [dict(r) for r in db.conn().execute(
        "SELECT id, doc_type, filename, size, uploaded_by, uploaded_at FROM employee_documents WHERE device_user_id=? ORDER BY id DESC",
        (uid,))]


@router.post("/employees/{uid}/documents")
async def upload_document(uid: str, file: UploadFile = File(...), doc_type: str = Form("Other"),
                          u: dict = Depends(access.need("hr", "employees"))):
    uid = _employee(uid)
    data = await file.read(config.MAX_DOC_BYTES + 1)
    if len(data) > config.MAX_DOC_BYTES:
        raise HTTPException(413, f"File too large (max {config.MAX_DOC_BYTES // 1024 // 1024} MB)")
    k = _kind(data)
    if not k:
        raise HTTPException(400, "Upload a PDF, photo (JPG / PNG / WebP) or Word file")
    stored = f"{re.sub(r'[^A-Za-z0-9_-]', '_', uid)}/{uuid.uuid4().hex}.{k[0]}"
    path = os.path.join(config.DOCS_DIR, stored)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    name = re.sub(r"[\r\n\"\\\\/]", "_", os.path.basename(file.filename or f"document.{k[0]}"))[:120]
    c = db.conn()
    cur = c.execute("INSERT INTO employee_documents(device_user_id,doc_type,filename,stored,size,uploaded_by,uploaded_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uid, doc_type if doc_type in DOC_TYPES else "Other", name, stored, len(data), u["username"], _stamp()))
    c.commit()
    return {"ok": True, "id": cur.lastrowid}


def _doc(doc_id: int) -> dict:
    row = db.conn().execute("SELECT * FROM employee_documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such document")
    access.assert_in_scope(row["device_user_id"])
    return dict(row)


@router.get("/documents/{doc_id}", dependencies=[Depends(access.need("hr", "employees"))])
def get_document(doc_id: int):
    d = _doc(doc_id)
    base = os.path.realpath(config.DOCS_DIR)
    path = os.path.realpath(os.path.join(base, d["stored"]))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, "File missing")
    k = _kind(open(path, "rb").read(16))
    inline = k and k[0] in ("pdf", "jpg", "png", "webp")
    return FileResponse(path, media_type=k[1] if k else "application/octet-stream", filename=d["filename"],
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/documents/{doc_id}", dependencies=[Depends(access.need("hr", "employees"))])
def delete_document(doc_id: int):
    d = _doc(doc_id)
    c = db.conn()
    c.execute("DELETE FROM employee_documents WHERE id=?", (doc_id,))
    c.commit()
    try:
        os.remove(os.path.join(config.DOCS_DIR, d["stored"]))
    except OSError:
        pass
    return {"ok": True}


# ---------------------------------------------------------------- departments & designations
KINDS = {"department": "department", "designation": "designation"}


class MasterIn(BaseModel):
    name: str
    new_name: str = ""     # rename


@router.get("/masters", dependencies=[Depends(access.need("manager"))])
def masters():
    """Pick lists with how many active employees use each value (values typed on employees before the lists existed
    are included automatically)."""
    c = db.conn()
    out = {}
    for kind, col in KINDS.items():
        names = {r["name"] for r in c.execute("SELECT name FROM masters WHERE kind=?", (kind,))}
        counts = {r[0]: r[1] for r in c.execute(f"SELECT {col}, COUNT(*) FROM employees WHERE active=1 AND {col}!='' GROUP BY {col}")}
        names |= set(counts)
        out[kind + "s"] = [{"name": n, "employees": counts.get(n, 0)} for n in sorted(names, key=str.lower)]
    return out


def _kind_col(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(404, "Unknown list")
    return KINDS[kind]


@router.post("/masters/{kind}", dependencies=[Depends(access.need("hr", "employees"))])
def add_master(kind: str, body: MasterIn):
    col = _kind_col(kind)
    name, new = body.name.strip()[:60], body.new_name.strip()[:60]
    if not name:
        raise HTTPException(400, "Type a name")
    c = db.conn()
    if new and new != name:   # rename everywhere it is used
        c.execute("DELETE FROM masters WHERE kind=? AND name=?", (kind, name))
        c.execute("INSERT OR IGNORE INTO masters(kind,name) VALUES (?,?)", (kind, new))
        c.execute(f"UPDATE employees SET {col}=? WHERE {col}=?", (new, name))
        if kind == "department":
            c.execute("UPDATE users SET department=? WHERE department=?", (new, name))
    else:
        c.execute("INSERT OR IGNORE INTO masters(kind,name) VALUES (?,?)", (kind, name))
    c.commit()
    return {"ok": True}


@router.delete("/masters/{kind}/{name}", dependencies=[Depends(access.need("hr", "employees"))])
def delete_master(kind: str, name: str):
    col = _kind_col(kind)
    c = db.conn()
    n = c.execute(f"SELECT COUNT(*) FROM employees WHERE active=1 AND {col}=?", (name,)).fetchone()[0]
    if n:
        raise HTTPException(400, f"{n} employee(s) still have this {kind}. Move them first, or rename it instead.")
    c.execute("DELETE FROM masters WHERE kind=? AND name=?", (kind, name))
    c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- loans & advances
class LoanIn(BaseModel):
    device_user_id: str
    kind: str = "Loan"        # Loan | Advance
    amount: float
    months: int = 1           # repaid in this many equal monthly instalments (EMI = amount / months)
    start_month: str          # first month the EMI is deducted, YYYY-MM
    note: str = ""


class RepayIn(BaseModel):
    month: str
    amount: float
    note: str = ""


@router.get("/loans", dependencies=[Depends(access.need("hr", "payroll"))])
def list_loans(employee: str | None = None):
    frag, args = access.scope_sql("e.department")
    q = ("SELECT l.*, COALESCE(e.name, l.device_user_id) name FROM loans l LEFT JOIN employees e ON e.device_user_id=l.device_user_id "
         "WHERE 1=1" + frag)
    if employee:
        q, args = q + " AND l.device_user_id=?", [*args, db.canon_id(employee)]
    return [loans.summary(dict(r)) for r in db.conn().execute(q + " ORDER BY l.id DESC", args)]


@router.post("/loans")
async def add_loan(body: LoanIn, u: dict = Depends(access.need("hr", "payroll_edit"))):
    uid = _employee(body.device_user_id)
    if body.kind not in ("Loan", "Advance"):
        raise HTTPException(400, "Type must be Loan or Advance")
    if not (0 < body.amount <= 10_000_000) or not (1 <= body.months <= 120):
        raise HTTPException(400, "Give an amount and 1 to 120 months")
    if not MONTH_RE.fullmatch(body.start_month):
        raise HTTPException(400, "First deduction month must look like 2026-11")
    emi = math.ceil(body.amount / body.months * 100) / 100     # rounded up to the paisa; the last instalment is smaller
    c = db.conn()
    cur = c.execute("INSERT INTO loans(device_user_id,kind,amount,emi,start_month,note,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, body.kind, round(body.amount, 2), emi, body.start_month, body.note.strip()[:120], u["username"], _stamp()))
    c.commit()
    return {"ok": True, "id": cur.lastrowid, "emi": emi}


def _loan(loan_id: int) -> dict:
    row = db.conn().execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such loan")
    access.assert_in_scope(row["device_user_id"])
    return dict(row)


@router.post("/loans/{loan_id}/repay")
async def repay_loan(loan_id: int, body: RepayIn, u: dict = Depends(access.need("hr", "payroll_edit"))):
    """A repayment outside salary (cash, bank transfer). Later EMIs shrink accordingly."""
    loan = loans.summary(_loan(loan_id))
    if not MONTH_RE.fullmatch(body.month) or not (0 < body.amount <= loan["outstanding"] + 0.001):
        raise HTTPException(400, f"Give a month and an amount up to the outstanding {loan['outstanding']:,.2f}")
    c = db.conn()
    c.execute("INSERT INTO loan_repayments(loan_id,month,amount,source,note,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
              (loan_id, body.month, round(body.amount, 2), "manual", body.note.strip()[:120], u["username"], _stamp()))
    c.commit()
    return {"ok": True}


@router.post("/loans/{loan_id}/close", dependencies=[Depends(access.need("hr", "payroll_edit"))])
def close_loan(loan_id: int):
    """Stop deducting (written off / settled outside the system)."""
    _loan(loan_id)
    c = db.conn()
    c.execute("UPDATE loans SET closed=1 WHERE id=?", (loan_id,))
    c.commit()
    return {"ok": True}


@router.delete("/loans/{loan_id}", dependencies=[Depends(access.need("hr", "payroll_edit"))])
def delete_loan(loan_id: int):
    _loan(loan_id)
    c = db.conn()
    if c.execute("SELECT 1 FROM loan_repayments WHERE loan_id=?", (loan_id,)).fetchone():
        raise HTTPException(400, "Repayments are already recorded for this loan. Close it instead of deleting.")
    c.execute("DELETE FROM loans WHERE id=?", (loan_id,))
    c.commit()
    return {"ok": True}


@router.get("/loans/ledger/{uid}", dependencies=[Depends(access.need("hr", "payroll"))])
def loan_ledger(uid: str):
    return loans.ledger(_employee(uid))


# ---------------------------------------------------------------- shift roster
class RosterIn(BaseModel):
    device_user_id: str
    date: str
    value: str = ""        # "" = back to normal, "off" = day off, otherwise a shift id


class RosterCopyIn(BaseModel):
    from_start: str        # copy the 7 days starting here ...
    to_start: str          # ... onto the 7 days starting here (for everyone you can see)


def _iso(d: str) -> date:
    try:
        return date.fromisoformat(d)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-10-20")


@router.get("/roster", dependencies=[Depends(access.need("manager"))])
def get_roster(start: str, days: int = 7):
    d0 = _iso(start)
    days = max(1, min(days, 31))
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(days)]
    frag, args = access.scope_sql("e.department")
    c = db.conn()
    emps = [dict(r) for r in c.execute(
        "SELECT e.device_user_id, e.name, e.department, e.shift_id, COALESCE(s.name,'') shift FROM employees e "
        "LEFT JOIN shifts s ON s.id=e.shift_id WHERE e.active=1" + frag + " ORDER BY e.department, e.name", args)]
    cells: dict = {}
    for r in c.execute("SELECT * FROM roster WHERE date BETWEEN ? AND ?", (dates[0], dates[-1])):
        cells.setdefault(r["device_user_id"], {})[r["date"]] = "off" if r["off"] else str(r["shift_id"])
    for e in emps:
        e["cells"] = cells.get(e["device_user_id"], {})
    return {"dates": dates, "rows": emps}


def _set_cell(c, uid: str, d: str, value: str, user: str):
    if value == "":
        c.execute("DELETE FROM roster WHERE device_user_id=? AND date=?", (uid, d))
    elif value == "off":
        c.execute("INSERT INTO roster(device_user_id,date,shift_id,off,created_by) VALUES (?,?,NULL,1,?) "
                  "ON CONFLICT(device_user_id,date) DO UPDATE SET shift_id=NULL, off=1, created_by=excluded.created_by", (uid, d, user))
    else:
        if not value.isdigit() or not c.execute("SELECT 1 FROM shifts WHERE id=?", (int(value),)).fetchone():
            raise HTTPException(404, "No such shift")
        c.execute("INSERT INTO roster(device_user_id,date,shift_id,off,created_by) VALUES (?,?,?,0,?) "
                  "ON CONFLICT(device_user_id,date) DO UPDATE SET shift_id=excluded.shift_id, off=0, created_by=excluded.created_by",
                  (uid, d, int(value), user))


@router.post("/roster")
async def set_roster(body: RosterIn, u: dict = Depends(access.need("manager", "shifts"))):
    uid = _employee(body.device_user_id)
    _iso(body.date)
    c = db.conn()
    _set_cell(c, uid, body.date, body.value, u["username"])
    c.commit()
    return {"ok": True}


@router.post("/roster/copy")
async def copy_roster(body: RosterCopyIn, u: dict = Depends(access.need("manager", "shifts"))):
    """Repeat a week's roster on another week (only the people this user may manage)."""
    a, b = _iso(body.from_start), _iso(body.to_start)
    src = get_roster(a.isoformat(), 7)
    c = db.conn()
    n = 0
    for row in src["rows"]:
        for i in range(7):
            v = row["cells"].get((a + timedelta(days=i)).isoformat(), "")
            _set_cell(c, row["device_user_id"], (b + timedelta(days=i)).isoformat(), v, u["username"])
            n += bool(v)
    c.commit()
    return {"ok": True, "copied": n}
