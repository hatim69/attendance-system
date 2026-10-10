"""Admin logins, roles and server-side permission checks.

Roles: admin (everything) > hr (employees, shifts, leave, punches, reports, payroll, import) > manager (read-only,
limited to one department, no salary data). Every route declares the minimum role it needs; hiding menu items in the
UI is cosmetic only."""
from contextvars import ContextVar

from fastapi import HTTPException, Request

from . import auth, config, db

LEVEL = {"manager": 1, "management": 1, "hr": 2, "admin": 3}   # manager = team lead (one department)
COOKIE = "att_session"
SESSION_TTL = 12 * 3600
SAFE = {"GET", "HEAD", "OPTIONS"}
# A manager is scoped to one department; this holds it for the current request (None = no restriction).
scope_dept: ContextVar[str | None] = ContextVar("scope_dept", default=None)
# Salary figures are visible to hr and admin only.
salary_visible: ContextVar[bool] = ContextVar("salary_visible", default=True)


def user_count() -> int:
    return db.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def open_mode() -> bool:
    """No users and no ADMIN_PASSWORD: local development / first run. Everything is open and a warning is logged."""
    return user_count() == 0 and not config.ADMIN_PASSWORD


def bootstrap():
    """First start with ADMIN_PASSWORD set and no users: create the 'admin' account."""
    if user_count() == 0 and config.ADMIN_PASSWORD:
        c = db.conn()
        c.execute("INSERT INTO users(username, display_name, password_hash, role) VALUES ('admin','Administrator',?, 'admin')",
                  (auth.hash_password(config.ADMIN_PASSWORD),))
        c.commit()


def session_user(request_or_ws) -> dict | None:
    name = auth.read_token(request_or_ws.cookies.get(COOKIE, ""), "admin")
    if not name:
        return None
    row = db.conn().execute("SELECT * FROM users WHERE username=? AND active=1", (name,)).fetchone()
    return dict(row) if row else None


def need(role: str):
    """Dependency factory: require at least `role`. Async so the department scope set here is visible to the handler."""
    async def dep(request: Request) -> dict:
        if open_mode():
            request.state.user = "(open)"
            scope_dept.set(None)
            salary_visible.set(True)
            return {"username": "(open)", "role": "admin", "department": "", "display_name": "Open mode"}
        user = session_user(request)
        if not user:
            raise HTTPException(401, "Please log in")
        request.state.user = user["username"]            # recorded in the audit log even when the request is refused
        if request.method not in SAFE and request.headers.get("x-requested-with") != "fetch":
            raise HTTPException(403, "Missing request header")       # CSRF guard: browsers cannot send this cross-site
        if LEVEL[user["role"]] < LEVEL[role]:
            raise HTTPException(403, "Your role does not allow this")
        scope_dept.set((user["department"] or "\u0000") if user["role"] == "manager" else None)
        salary_visible.set(LEVEL[user["role"]] >= LEVEL["hr"])
        return user
    return dep


def scope_sql(column: str) -> tuple[str, list]:
    """SQL fragment limiting rows to the manager's department."""
    d = scope_dept.get()
    return (f" AND {column}=?", [d]) if d is not None else ("", [])
