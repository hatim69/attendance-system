"""Admin logins, roles and server-side permission checks.

Roles: admin (everything) > hr (employees, shifts, leave, punches, reports, payroll, import) > manager (read-only,
limited to one department, no salary data). Every route declares the minimum role it needs; hiding menu items in the
UI is cosmetic only."""
from contextvars import ContextVar

from fastapi import HTTPException, Request

from . import auth, config, db

# manager = team lead (one department); management has the same power as admin
LEVEL = {"manager": 1, "hr": 2, "admin": 3, "management": 3}
# Abilities that can be switched on or off per person. admin and management always have all of them.
#   hr: starts with HR_DEFAULT (permissions "*"); untick to remove, tick sites/devices to add.
#   manager (team lead): has team-scoped shift changes and punch fixes by role; anything below is a manual extra
#   and still only applies to their own team (MANAGER_GRANTABLE).
HR_DEFAULT = ["employees", "shifts", "leave", "holidays", "punches", "import", "payroll", "payroll_edit"]
ALL_PERMS = HR_DEFAULT + ["sites", "devices"]
MANAGER_GRANTABLE = ["employees", "shifts", "leave", "holidays", "payroll"]
HR_PERMS = ALL_PERMS    # kept for older imports
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


def need(role: str, perm: str | None = None):
    """Dependency factory: require at least `role`; an HR user must also hold `perm` when one is given. Async so the department scope set here is visible to the handler."""
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
        if not allowed(user, role, perm):
            raise HTTPException(403, "Your account does not have access to this" + (f" ({perm})" if perm else ""))
        scope_dept.set((user["department"] or "\u0000") if user["role"] == "manager" else None)
        salary_visible.set(LEVEL[user["role"]] >= LEVEL["hr"])
        return user
    return dep


def scope_sql(column: str) -> tuple[str, list]:
    """SQL fragment limiting rows to the manager's department."""
    d = scope_dept.get()
    return (f" AND {column}=?", [d]) if d is not None else ("", [])


def perm_list(user: dict) -> list[str]:
    raw = (user.get("permissions") or "").strip()
    if user["role"] == "hr" and raw in ("", "*"):
        return list(HR_DEFAULT)
    return [p.strip() for p in raw.split(",") if p.strip() and p.strip() != "*"]


def has_perm(user: dict, perm: str) -> bool:
    if user["role"] in ("admin", "management"):
        return True
    return perm in perm_list(user)


def allowed(user: dict, role: str, perm: str | None) -> bool:
    """May this user use a route that needs `role`, optionally gated by ability `perm`?
    admin/management: always. hr: needs the ability (so it can be removed or added). manager (team lead): open
    routes by role, or any route whose ability was granted to them manually."""
    r = user["role"]
    if r in ("admin", "management"):
        return True
    if r == "hr":
        return has_perm(user, perm) if perm else LEVEL["hr"] >= LEVEL[role]
    if LEVEL["manager"] >= LEVEL[role]:                   # a route open to team leads (scoped to their team)
        return True
    return bool(perm) and has_perm(user, perm)             # manually granted extra


def assert_in_scope(employee_id: str):
    """A team lead may only act on employees in their own department."""
    d = scope_dept.get()
    if d is None:
        return
    row = db.conn().execute("SELECT department FROM employees WHERE device_user_id=?", (employee_id,)).fetchone()
    if not row or row["department"] != d:
        raise HTTPException(403, "That employee is not in your team")
