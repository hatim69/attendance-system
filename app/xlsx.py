"""Excel helpers shared by the reports and the weekly e-mail: one coloured table per worksheet."""
from datetime import date

from . import reports

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TIMECARD_COLUMNS = [("name", "Employee Name"), ("device_user_id", "Employee ID"), ("department", "Department"),
                    ("date", "Date"), ("weekday", "Day"), ("day_type", "Day Type"),
                    ("scheduled_in", "Scheduled Login"), ("in", "Actual Login"),
                    ("scheduled_out", "Scheduled Logout"), ("out", "Actual Logout"),
                    ("late_min", "Late Arrival (min)"), ("early_in_min", "Early Login (min)"),
                    ("early_min", "Early Logout (min)"), ("hours", "Total Working Hours"), ("status", "Attendance Status"),
                    ("leave_type", "Leave Type"), ("week_off_duty", "Week-off duty"), ("note", "Note")]


def timecard_human(r: dict) -> dict:
    """A time card row with Excel headers, a real date, and 'Yes (rostered)' for approved week-off work."""
    return {label: (date.fromisoformat(r[k]) if k == "date" else ("Yes (rostered)" if r[k] else "") if k == "week_off_duty"
                    else r[k]) for k, label in TIMECARD_COLUMNS}


def safe(v):
    """Stop spreadsheet formula injection: a text cell starting with = + - @ would be executed by Excel."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def xlsx_sheet(ws, title: str, rows: list[dict], header_rows: list[list] | None = None):
    """One table on a worksheet, with the leave colours: a leave day's row (leave_type) and IL / AL codes are filled
    yellow (Informed) or red (Adhoc), as are non-zero Informed / Adhoc leave counts; a legend goes under the table."""
    from openpyxl.styles import Font, PatternFill
    fills = {k: PatternFill("solid", fgColor=c) for k, c in reports.XLSX_FILLS.items()}
    ws.title = title[:31]
    for h in header_rows or []:      # e.g. the employee's name and ID above their own table
        ws.append(h)
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    if header_rows:
        ws.append([])
    top = ws.max_row + 1 if header_rows else 1
    if not rows:
        return
    keys = list(rows[0].keys())
    ws.append(keys)
    for cell in ws[top]:
        cell.font = Font(bold=True)
    kind_cols = {i + 1: k for i, h in enumerate(keys) for k, names in reports.LEAVE_COLUMNS.items() if h in names}
    lt = next((h for h in keys if h in ("leave_type", "Leave Type")), None)
    for r in rows:
        ws.append([safe(v) for v in r.values()])
        row = ws.max_row
        for cell in ws[row]:
            if isinstance(cell.value, date):
                cell.number_format = "DD-MM-YYYY"
        kind = r.get(lt) if lt else None
        if kind in fills:
            for cell in ws[row]:
                cell.fill = fills[kind]
        elif r.get("Week-off duty") or r.get("week_off_duty"):
            for cell in ws[row]:
                cell.fill = fills["WeekOff"]
        for cell in ws[row]:
            v = cell.value
            if v in ("IL", "AL"):
                cell.fill = fills["Informed" if v == "IL" else "Adhoc"]
            elif cell.column in kind_cols and isinstance(v, (int, float)) and v > 0:
                cell.fill = fills[kind_cols[cell.column]]
    for col in ws.columns:  # readable column widths
        ws.column_dimensions[col[0].column_letter].width = min(40, max(len(str(c.value or "")) for c in col[top - 1:]) + 2)
    ws.freeze_panes = ws.cell(top + 1, 1)
    ws.append([])
    for key, label in reports.XLSX_LEGEND:
        ws.append([label])
        ws.cell(ws.max_row, 1).fill = fills[key]


def inout_sheet(ws, title: str, rows: list[dict], start: str, end: str):
    """The time card as a grid: one row per employee, two columns (In, Out) per date. Leave days show IL / AL in
    yellow / red, weekly offs WO, holidays H and absent days A across both columns; a late check-in is in red, an
    approved (rostered) week-off duty is blue, and a missing punch stays empty (nothing is invented)."""
    from datetime import timedelta
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    fills = {k: PatternFill("solid", fgColor=c) for k, c in reports.XLSX_FILLS.items()}
    grey, sky = PatternFill("solid", fgColor="F1F5F9"), PatternFill("solid", fgColor="E0F2FE")
    thin = Side(style="thin", color="CBD5E1")
    box, mid = Border(left=thin, right=thin, top=thin, bottom=thin), Alignment(horizontal="center", vertical="center")
    ws.title = title[:31]
    days, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        days.append(d)
        d += timedelta(days=1)
    ws.cell(1, 1, "Date →").font = Font(bold=True)
    ws.cell(2, 1, "Employee").font = Font(bold=True)
    ws.cell(2, 2, "ID").font = Font(bold=True)
    for i, day in enumerate(days):
        c = 3 + 2 * i
        ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + 1)
        h = ws.cell(1, c, f"{day:%a %d/%m}")
        h.font, h.alignment = Font(bold=True), mid
        for j, label in enumerate(("In", "Out")):
            x = ws.cell(2, c + j, label)
            x.font, x.alignment, x.border = Font(bold=True), mid, box
        ws.column_dimensions[get_column_letter(c)].width = ws.column_dimensions[get_column_letter(c + 1)].width = 7
    by = {(r["device_user_id"], r["date"]): r for r in rows}
    people = sorted({(r["name"], r["device_user_id"]) for r in rows}, key=lambda p: (p[0].lower(), p[1]))
    codes = {"Weekly Off": ("WO", grey), "Holiday": ("H", sky), "Absent": ("A", None)}
    for n, (name, uid) in enumerate(people):
        row = 3 + n
        ws.cell(row, 1, safe(name)).font = Font(bold=True)
        ws.cell(row, 2, safe(uid))
        for i, day in enumerate(days):
            c, r = 3 + 2 * i, by.get((uid, day.isoformat()))
            cin, cout = ws.cell(row, c), ws.cell(row, c + 1)
            cin.border = cout.border = box
            cin.alignment = cout.alignment = mid
            if not r or not r["status"]:
                continue
            kind = r.get("leave_type")
            if r["status"] == "Leave" or r["status"] in codes:
                ws.merge_cells(start_row=row, start_column=c, end_row=row, end_column=c + 1)
                if r["status"] == "Leave":
                    cin.value, fill = ("IL" if kind == "Informed" else "AL" if kind == "Adhoc" else "LV"), fills.get(kind)
                else:
                    cin.value, fill = codes[r["status"]]
                if r.get("absent_adhoc"):          # absent without approved leave: counted as Adhoc
                    fill = fills["Adhoc"]
                if fill:
                    cin.fill = fill
                if r["status"] == "Absent":
                    cin.font = Font(bold=True, color="B91C1C")
                continue
            cin.value, cout.value = r["in"] or None, r["out"] or None
            if r["late_min"] > 0:
                cin.font = Font(bold=True, color="B91C1C")
            fill = fills.get(kind) if kind else fills["WeekOff"] if r.get("week_off_duty") else None
            if fill:
                cin.fill = cout.fill = fill
    ws.column_dimensions["A"].width = max([len("Employee")] + [len(p[0]) for p in people]) + 2
    ws.column_dimensions["B"].width = max([4] + [len(p[1]) for p in people]) + 2
    ws.freeze_panes = "C3"
    r0 = ws.max_row + 2
    legend = [("IL", fills["Informed"], "Informed leave"), ("AL", fills["Adhoc"], "Adhoc leave"), ("WO", grey, "Weekly off"),
              ("H", sky, "Holiday"), ("A", None, "Absent"), ("A ", fills["Adhoc"], "Absent without approved leave: counted as Adhoc"), ("10:25", None, "Late check-in (red)"),
              ("", fills["WeekOff"], "Rostered work on a weekly off")]
    for k, (code, fill, label) in enumerate(legend):
        x = ws.cell(r0 + k, 1, code or " ")
        if fill:
            x.fill = fill
        if code.strip() in ("A", "10:25"):
            x.font = Font(bold=True, color="B91C1C")
        ws.cell(r0 + k, 2, label)
