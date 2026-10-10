"""Create or reset an admin login. Use if the admin password is lost.
Usage (from the project folder; on the Docker server run it inside the container):
    python scripts/reset_admin.py <username> <new-password>
    docker compose exec app python scripts/reset_admin.py admin 'new long password'"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import auth, db  # noqa: E402

if len(sys.argv) != 3 or len(sys.argv[2]) < 8:
    sys.exit("usage: reset_admin.py <username> <password of at least 8 characters>")
user, pw = sys.argv[1].strip().lower(), sys.argv[2]
db.init()
c = db.conn()
c.execute("INSERT INTO users(username, display_name, password_hash, role, active) VALUES (?,?,?, 'admin', 1) "
          "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, role='admin', active=1",
          (user, user, auth.hash_password(pw)))
c.commit()
print(f"Admin '{user}' is ready.")
