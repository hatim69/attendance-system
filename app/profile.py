"""Personal and bank details on the employee record: clean-up shared by the employee form (strict: bad values are
refused) and the importers (lenient: values are tidied and kept, so a messy sheet still loads)."""
import re
from datetime import date, datetime

from .db import PROFILE_FIELDS

EMPTY = {"", "-", "--", "na", "n/a", "n.a", "n.a.", "nil", "nill", "null", "none", "no", "nan", "not applicable", "not available"}
BLOOD_GROUPS = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
ACCOUNT_TYPES = ("Savings", "Current", "Salary")
PAN_RE = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():      # Excel turns account / phone numbers into 9876543210.0
        v = int(v)
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    t = re.sub(r"\s+", " ", str(v)).strip().strip('"').strip("'").strip()
    return "" if t.lower() in EMPTY else t


def _blood(t: str) -> str:
    k = t.upper().replace(" ", "").replace("POSITIVE", "+").replace("POSTIVE", "+").replace("NEGATIVE", "-").replace("VE", "")
    k = k.replace("0", "O")
    return k if k in BLOOD_GROUPS else t


def _date(t: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(t.split(" ")[0], fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unreadable date {t!r} (use DD/MM/YYYY)")


def clean(field: str, value, strict: bool) -> str:
    """Tidy one field. strict=True raises ValueError (with a message for the user) when the value is clearly wrong."""
    t = text(value)
    if not t:
        return ""
    if field in ("dob", "passport_expiry"):
        try:
            return _date(t)
        except ValueError:
            if strict:
                raise
            return t
    if field == "gender":
        return {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}.get(t.lower(), t.title())
    if field == "blood_group":
        return _blood(t)
    if field == "nationality":
        return "Indian" if t.lower() in ("indian", "india", "ind") else t.title()
    if field in ("email", "work_email"):
        t = t.replace(",", ".").replace(" ", "").lower()
        if strict and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", t):
            raise ValueError(f"{t!r} is not an e-mail address")
        return t
    if field == "pan":
        t = t.replace(" ", "").upper()
        if strict and not PAN_RE.fullmatch(t):
            raise ValueError("PAN should look like ABCDE1234F")
        return t
    if field == "bank_ifsc":
        t = t.replace(" ", "").upper()
        if strict and not IFSC_RE.fullmatch(t):
            raise ValueError("IFSC should be 11 characters, like SBIN0001234")
        return t
    if field == "bank_account":
        t = re.sub(r"[\s-]", "", t)
        if strict and not re.fullmatch(r"\d{6,20}", t):
            raise ValueError("Bank account number should be 6 to 20 digits")
        return t
    if field == "govt_uid":
        return re.sub(r"\s", "", t) if re.fullmatch(r"[\d\s]+", t) else t
    if field == "bank_type":
        low = t.lower()
        return next((a for a in ACCOUNT_TYPES if low.startswith(a.lower()[:4])), t.title())
    if field in ("bank_holder", "mother_name", "spouse_name") and t.isupper():   # NAMES IN CAPITALS -> Names (bank names keep IDBI, SBI)
        return t.title()
    return t


def clean_all(values: dict, strict: bool) -> dict:
    """Clean every profile field present in values (others are left out)."""
    return {f: clean(f, values[f], strict) for f in PROFILE_FIELDS if f in values}
