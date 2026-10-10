"""Payslip PDF (reportlab)."""
import calendar
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def money(v: float) -> str:
    return f"{v:,.2f}"


def build_pdf(d: dict) -> bytes:
    y, m = map(int, d["month"].split("-"))
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=16, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=colors.grey, alignment=1)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9, alignment=1, textColor=colors.HexColor("#444444"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Payslip {d['month']} {d['employee']['name']}", pageCompression=0)
    e, a = d["employee"], d["attendance"]
    story = [Paragraph(d["company"] or "Payslip", h1)]
    if d["address"]:
        story.append(Paragraph(d["address"], sub))
    story += [Paragraph(f"<b>Payslip for {calendar.month_name[m]} {y}</b>", ParagraphStyle("t", parent=ss["Normal"], fontSize=12, alignment=1, spaceBefore=6)), Spacer(1, 8)]

    grid = [("Employee", e["name"], "Employee ID", e["code"]),
            ("Department", e["department"], "Designation", e["designation"]),
            ("Branch", e["branch"], "Date of joining", e["joined"])]
    t = Table(grid, colWidths=[28 * mm, 58 * mm, 30 * mm, 58 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                           ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.grey)]))
    story += [t, Spacer(1, 8)]

    att = [("Days in month", f"{a['days_in_month']:g}"), ("Paid days", f"{a['paid_days']:g}"), ("Days present", f"{a['present']:g}"),
           ("Late days", f"{a['late_days']:g}"), ("Half days", f"{a['half_days']:g}"), ("Absent", f"{a['absent']:g}"),
           ("Leave", f"{a['leave']:g}"), ("Holidays + weekly offs", f"{a['holidays'] + a['weekly_offs']:g}"), ("Overtime hours", f"{a['overtime_hours']:g}")]
    rows = [[att[i][0], att[i][1], att[i + 1][0], att[i + 1][1], att[i + 2][0], att[i + 2][1]] for i in range(0, 9, 3)]
    t = Table(rows, colWidths=[34 * mm, 14 * mm, 34 * mm, 14 * mm, 34 * mm, 14 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (0, -1), colors.grey), ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
                           ("TEXTCOLOR", (4, 0), (4, -1), colors.grey), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [Paragraph("<b>Attendance</b>", ss["Normal"]), Spacer(1, 3), t, Spacer(1, 10)]

    n = max(len(d["earnings"]), len(d["deductions"]))
    pay = [["Earnings", "Amount", "Deductions", "Amount"]]
    for i in range(n):
        le = d["earnings"][i] if i < len(d["earnings"]) else ("", None)
        ld = d["deductions"][i] if i < len(d["deductions"]) else ("", None)
        pay.append([le[0], money(le[1]) if le[1] is not None else "", ld[0], money(ld[1]) if ld[1] is not None else ""])
    pay.append(["Total earnings", money(d["gross"]), "Total deductions", money(d["total_deductions"])])
    t = Table(pay, colWidths=[56 * mm, 30 * mm, 56 * mm, 30 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2ff")),
                           ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                           ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                           ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.grey), ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                           ("LINEAFTER", (1, 0), (1, -1), 0.5, colors.grey)]))
    story += [t, Spacer(1, 8)]
    net = Table([["Net pay", money(d["net_pay"])]], colWidths=[112 * mm, 60 * mm])
    net.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 12), ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                             ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#dcfce7")), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [net, Spacer(1, 14),
              Paragraph("This is a computer-generated payslip and does not require a signature. Loss of pay is salary divided by calendar "
                        "days in the month, multiplied by unpaid days.", small),
              Paragraph(f"Generated {datetime.now():%d %b %Y}", small)]
    doc.build(story)
    return buf.getvalue()
