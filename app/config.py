import os

DB_PATH = os.getenv("DB_PATH", "attendance.db")
SELFIE_DIR = os.getenv("SELFIE_DIR", "selfies")
DOCS_DIR = os.getenv("DOCS_DIR", "documents")          # employee documents (ID proofs, letters...)
MAX_DOC_BYTES = int(os.getenv("MAX_DOC_BYTES", str(10 * 1024 * 1024)))
RAW_LOG = os.getenv("RAW_LOG", "fkweb_raw.log")
# Local timezone of the workplace. Devices report local time; app punches are stamped in this zone.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
# Comma separated "host[:port]" list of ZKTeco devices to poll, e.g. "192.168.1.201:4370"
ZK_DEVICES = [d.strip() for d in os.getenv("ZK_DEVICES", "").split(",") if d.strip()]
ZK_PASSWORD = int(os.getenv("ZK_PASSWORD", "0"))
ZK_POLL_SECONDS = int(os.getenv("ZK_POLL_SECONDS", "10"))
# Raw TCP port for FkWeb devices (Realtime/FK series). They post to the 'Webserver URL' on port 80.
# -1 disables the listener, 0 picks a free port (tests).
FKWEB_PORT = int(os.getenv("FKWEB_PORT", "80"))
# Optional allow-list of device ids (the dev_id header, e.g. RSS202506120926). The FkWeb port is plain HTTP
# and public on a hosted server, so set this to stop strangers injecting punches.
FKWEB_DEVICES = [d.strip() for d in os.getenv("FKWEB_DEVICES", "").split(",") if d.strip()]
# Weekly off for employees who have no shift assigned (shifts carry their own). Comma list, e.g. "SUN" or "SAT,SUN".
WEEKLY_OFF = os.getenv("WEEKLY_OFF", "SUN")
# Ignore repeated punches by the same person within this many seconds
DEBOUNCE_SECONDS = int(os.getenv("DEBOUNCE_SECONDS", "60"))
# Admin dashboard login (HTTP Basic, user: admin). REQUIRED when hosted on the internet.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Signs employee app tokens. Set a long random value in production; if empty one is generated per process.
SECRET_KEY = os.getenv("SECRET_KEY", "")
# App punches outside every site: "block" rejects them, "flag" stores them marked 'outside_geofence'.
GEOFENCE_MODE = os.getenv("GEOFENCE_MODE", "block")
MAX_SELFIE_BYTES = int(os.getenv("MAX_SELFIE_BYTES", str(3 * 1024 * 1024)))
# Public hostname of the hosted site (e.g. attendance.example.com). Plain-HTTP browser visits to port 80 are redirected
# to https://<this>. Leave empty to disable the redirect.
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "")
# When true the app refuses to start without any login (set in docker-compose so a missing ADMIN_PASSWORD can never
# leave a hosted dashboard open to the internet).
REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "") not in ("", "0", "false")
# Shown on payslips
COMPANY_NAME = os.getenv("COMPANY_NAME", "")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
# Optional safety net: append every scan received from a fingerprint device to this CSV file (the columns match the
# Import tab, so the file can be loaded later). Leave empty to disable. Example: PUNCH_LOG_FILE=punches_log.csv
PUNCH_LOG_FILE = os.getenv("PUNCH_LOG_FILE", "")
