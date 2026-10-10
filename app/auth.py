"""Employee PIN hashing, signed session tokens and login throttling (stdlib only)."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from . import config

_secret: bytes | None = None
_fails: dict[str, tuple[int, float]] = {}
TOKEN_TTL = 30 * 24 * 3600
MAX_FAILS, LOCK_SECONDS = 5, 300


def _key() -> bytes:
    global _secret
    if _secret is None:
        _secret = (config.SECRET_KEY or secrets.token_hex(32)).encode()
    return _secret


def valid_pin(pin: str) -> bool:
    return bool(re.fullmatch(r"\d{4,8}", pin or ""))


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return salt.hex() + "$" + h.hex()


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    cand = hashlib.pbkdf2_hmac("sha256", (pin or "").encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(cand.hex(), h)


def make_token(sub: str, kind: str = "emp", ttl: int = TOKEN_TTL) -> str:
    """Signed token. `kind` keeps employee-app tokens and admin sessions from being interchangeable."""
    body = base64.urlsafe_b64encode(json.dumps({"s": sub, "k": kind, "x": int(time.time()) + ttl}).encode()).decode()
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str, kind: str = "emp") -> str | None:
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
        return data["s"] if data["k"] == kind and data["x"] > time.time() else None
    except Exception:
        return None


def hash_password(pw: str) -> str:
    return hash_pin(pw)


def check_password(pw: str, stored: str | None) -> bool:
    return check_pin(pw, stored)


def locked(emp_id: str) -> bool:
    n, until = _fails.get(emp_id, (0, 0))
    return n >= MAX_FAILS and time.time() < until


def record_fail(emp_id: str):
    n, until = _fails.get(emp_id, (0, 0))
    n = n + 1 if time.time() >= until or n < MAX_FAILS else 1
    _fails[emp_id] = (n, time.time() + LOCK_SECONDS)


def record_ok(emp_id: str):
    _fails.pop(emp_id, None)
