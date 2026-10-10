import os

DB_PATH = os.getenv("DB_PATH", "attendance.db")
# Comma separated "host[:port]" list of ZKTeco devices to poll, e.g. "192.168.1.201:4370"
ZK_DEVICES = [d.strip() for d in os.getenv("ZK_DEVICES", "").split(",") if d.strip()]
ZK_PASSWORD = int(os.getenv("ZK_PASSWORD", "0"))
ZK_POLL_SECONDS = int(os.getenv("ZK_POLL_SECONDS", "10"))
# Shift rules used by reports
SHIFT_START = os.getenv("SHIFT_START", "09:00")
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "10"))
# Ignore repeated punches by the same person within this many seconds
DEBOUNCE_SECONDS = int(os.getenv("DEBOUNCE_SECONDS", "60"))
# If set, the dashboard/API requires HTTP Basic auth (user: admin). Device endpoints stay open.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
