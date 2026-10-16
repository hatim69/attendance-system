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
