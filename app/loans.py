"""Loans and salary advances, recovered from salary in fixed monthly instalments (EMI).

The instalment for a month is worked out on the fly from what is still outstanding, and becomes a permanent repayment
record when that month's payroll is published (removed again if it is unpublished). HR can also record a manual
repayment (cash, bank transfer), which lowers what later months deduct."""
from . import db
from .timeutil import now_local


def _stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def repaid(loan_id: int, before_month: str | None = None, include_manual_in: str | None = None) -> float:
    """Total repaid. With before_month: only repayments of earlier months, plus manual ones in `include_manual_in`."""
    q, args = "SELECT COALESCE(SUM(amount),0) FROM loan_repayments WHERE loan_id=?", [loan_id]
    if before_month:
        q += " AND (month<? OR (source='manual' AND month=?))"
        args += [before_month, include_manual_in or before_month]
    return round(db.conn().execute(q, args).fetchone()[0], 2)


def instalment(loan: dict, month: str) -> float:
    """What this loan deducts from `month`'s salary."""
    done = db.conn().execute("SELECT amount FROM loan_repayments WHERE loan_id=? AND month=? AND source='payroll'",
                             (loan["id"], month)).fetchone()
    if done:
        return round(done["amount"], 2)
    if loan["closed"] or month < loan["start_month"]:
        return 0.0
    left = loan["amount"] - repaid(loan["id"], month)
    return round(max(0.0, min(loan["emi"], left)), 2)


def deductions(month: str) -> dict[str, list[dict]]:
    """{employee id: [{loan_id, label, amount}]} for the month's payroll."""
    out: dict[str, list[dict]] = {}
    for r in db.conn().execute("SELECT * FROM loans WHERE start_month<=? ORDER BY id", (month,)):
        loan = dict(r)
        amt = instalment(loan, month)
        if amt > 0:
            label = f"{'Loan EMI' if loan['kind'] == 'Loan' else 'Advance recovery'} (#{loan['id']})"
            out.setdefault(loan["device_user_id"], []).append({"loan_id": loan["id"], "label": label, "amount": amt})
    return out


def record_month(month: str, user: str):
    """Called when payroll is published: lock this month's instalments in as repayments."""
    c = db.conn()
    for uid, items in deductions(month).items():
        for it in items:
            c.execute("DELETE FROM loan_repayments WHERE loan_id=? AND month=? AND source='payroll'", (it["loan_id"], month))
            c.execute("INSERT INTO loan_repayments(loan_id,month,amount,source,note,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                      (it["loan_id"], month, it["amount"], "payroll", "salary deduction", user, _stamp()))
    c.commit()


def unrecord_month(month: str):
    c = db.conn()
    c.execute("DELETE FROM loan_repayments WHERE month=? AND source='payroll'", (month,))
    c.commit()


def summary(loan: dict) -> dict:
    paid = repaid(loan["id"])
    left = round(max(loan["amount"] - paid, 0), 2)
    return {**loan, "repaid": paid, "outstanding": left,
            "status": "Closed" if loan["closed"] else "Repaid" if left <= 0 else "Active",
            "months_left": 0 if left <= 0 or loan["closed"] else -(-left // loan["emi"])}


def ledger(uid: str) -> list[dict]:
    """Every loan given and every repayment for one employee, oldest first, with the running balance owed."""
    c = db.conn()
    events = []
    for r in c.execute("SELECT * FROM loans WHERE device_user_id=?", (uid,)):
        events.append({"date": r["created_at"][:10], "sort": (r["created_at"], 0, r["id"]), "loan_id": r["id"],
                       "entry": f"{r['kind']} given" + (f": {r['note']}" if r["note"] else ""), "debit": r["amount"], "credit": 0})
    for r in c.execute("SELECT p.*, l.kind FROM loan_repayments p JOIN loans l ON l.id=p.loan_id WHERE l.device_user_id=?", (uid,)):
        label = "Salary deduction" if r["source"] == "payroll" else "Repayment" + (f": {r['note']}" if r["note"] else "")
        events.append({"date": r["created_at"][:10], "sort": (r["created_at"], 1, r["id"]),
                       "loan_id": r["loan_id"], "entry": f"{label} ({r['month']})", "debit": 0, "credit": r["amount"]})
    events.sort(key=lambda e: e["sort"])
    bal = 0.0
    for e in events:
        bal = round(bal + e["debit"] - e["credit"], 2)
        e["balance"] = bal
        del e["sort"]
    return events
