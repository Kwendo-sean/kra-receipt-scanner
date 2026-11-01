"""Render report dicts to HTML, PDF (ReportLab), XLSX (openpyxl) and CSV."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.reports import kes

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def _fmt(v, money: bool) -> str:
    if v is None:
        return "—"
    if money and isinstance(v, (int, float)):
        return kes(v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


env.filters["cell"] = _fmt


def to_html(rep: dict, image_url=None, standalone: bool = True) -> str:
    """image_url: callable(path) -> URL for on-screen images (auth-protected routes)."""
    return env.get_template("report.html").render(rep=rep, image_url=image_url, standalone=standalone)


# ------------------------------------------------------------------ CSV


def to_csv(rep: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([rep["title"], rep["subtitle"]])
    for s in rep["sections"]:
        for t in s["tables"]:
            w.writerow([])
            w.writerow([s["heading"] + (f" — {t['title']}" if t.get("title") else "")])
            w.writerow(t["columns"])
            for r in t["rows"]:
                w.writerow(["" if v is None else (f"{v:.2f}" if i in t["money_cols"] and isinstance(v, (int, float)) else v)
                            for i, v in enumerate(r)])
            if t.get("totals"):
                w.writerow(["" if v is None else (f"{v:.2f}" if i in t["money_cols"] and isinstance(v, (int, float)) else v)
                            for i, v in enumerate(t["totals"])])
    w.writerow([])
    w.writerow([rep["footer"]])
    return buf.getvalue().encode("utf-8-sig")


# ------------------------------------------------------------------ XLSX


def to_xlsx(rep: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="1F6F5C")
    head_font = Font(bold=True, color="FFFFFF")
    ws.append([rep["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([rep["subtitle"]])
    ws.append([f"Generated {rep['generated']}"])
    ws.append([])
    for s in rep["sections"]:
        if not (s.get("prose") or s.get("kv")) or s.get("prose_status") == "raw":
            continue
        ws.append([s["heading"]])
        ws.cell(ws.max_row, 1).font = bold
        if s.get("prose"):
            ws.append([s["prose"]])
            ws.cell(ws.max_row, 1).alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=6)
            ws.row_dimensions[ws.max_row].height = max(30, 15 * (len(s["prose"]) // 90 + 1))
        for k, v in s.get("kv", []):
            ws.append([k, v])
        ws.append([])
    ws.append([rep["footer"]])
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 50

    used = set()
    for s in rep["sections"]:
        for t in s["tables"]:
            name = (s["heading"] if not t.get("title") else t["title"])[:28]
            for ch in "[]:*?/\\":
                name = name.replace(ch, " ")
            base, n = name, 2
            while name in used:
                name = f"{base[:25]} {n}"
                n += 1
            used.add(name)
            sh = wb.create_sheet(name)
            sh.append(t["columns"])
            for c in sh[1]:
                c.font, c.fill = head_font, head_fill
            for r in t["rows"]:
                sh.append(list(r))
            if t.get("totals"):
                sh.append(list(t["totals"]))
                for c in sh[sh.max_row]:
                    c.font = bold
            for i in t["money_cols"]:
                col = get_column_letter(i + 1)
                for c in sh[col][1:]:
                    c.number_format = "#,##0.00"
            for i, col in enumerate(t["columns"], start=1):
                width = max([len(str(col))] + [len(str(r[i - 1])) for r in t["rows"][:200] if i - 1 < len(r)])
                sh.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
            sh.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ------------------------------------------------------------------ PDF


def to_pdf(rep: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape

    wide = any(len(t["columns"]) > 6 for s in rep["sections"] for t in s["tables"])
    pagesize = landscape(A4) if wide else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm, title=rep["title"])
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, leading=9.5)
    small_r = ParagraphStyle("sr", parent=small, alignment=TA_RIGHT)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#1F6F5C"))
    green = colors.HexColor("#1F6F5C")
    story = [Paragraph(escape(rep["title"]), h1), Paragraph(escape(rep["subtitle"]), body),
             Paragraph(escape(f"Generated {rep['generated']} · {rep.get('prose_summary', '')}"), small),
             Spacer(1, 6)]
    avail_w = pagesize[0] - doc.leftMargin - doc.rightMargin
    avail_h = pagesize[1] - doc.topMargin - doc.bottomMargin - 30
    for s in rep["sections"]:
        story.append(Paragraph(escape(s["heading"]), h2))
        if s.get("prose"):
            if s.get("prose_status") == "raw":
                story.append(Preformatted(s["prose"], ParagraphStyle("pre", fontName="Courier", fontSize=7, leading=8.5)))
            else:
                for para in s["prose"].split("\n\n"):
                    story.append(Paragraph(escape(para).replace("\n", "<br/>"), body))
        if s.get("kv"):
            data = [[Paragraph(escape(str(k)), small), Paragraph(escape(str(v)), small)] for k, v in s["kv"]]
            t = Table(data, colWidths=[avail_w * 0.32, avail_w * 0.68])
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                                   ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F6F5")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [Spacer(1, 4), t]
        for tbl in s["tables"]:
            if tbl.get("title"):
                story.append(Paragraph(escape(tbl["title"]), small))
            mc = set(tbl["money_cols"])

            def row_cells(r, bold=False):
                out = []
                for i, v in enumerate(r):
                    txt = escape(_fmt(v, i in mc))
                    if bold:
                        txt = f"<b>{txt}</b>"
                    out.append(Paragraph(txt, small_r if i in mc else small))
                return out
            data = [[Paragraph(f"<b>{escape(c)}</b>", small) for c in tbl["columns"]]]
            data += [row_cells(r) for r in tbl["rows"]] or [[Paragraph("No rows", small)] + [""] * (len(tbl["columns"]) - 1)]
            if tbl.get("totals"):
                data.append(row_cells(tbl["totals"], bold=True))
            n = len(tbl["columns"])
            t = Table(data, repeatRows=1, colWidths=[avail_w / n] * n)
            style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                     ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3EFEC")),
                     ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("LINEBELOW", (0, 0), (-1, 0), 0.8, green)]
            if tbl.get("totals"):
                style.append(("LINEABOVE", (0, -1), (-1, -1), 0.8, green))
            t.setStyle(TableStyle(style))
            story += [Spacer(1, 4), t]
        for p in s.get("images", []):
            story += _pdf_images(p, avail_w, avail_h, Image, PageBreak)
    story.append(Spacer(1, 10))  # disclaimer is drawn in every page footer

    def on_page(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(doc.leftMargin, 8 * mm, rep["footer"][:150])
        canvas.drawRightString(pagesize[0] - doc.rightMargin, 8 * mm, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def _pdf_images(path: str, avail_w: float, avail_h: float, Image, PageBreak) -> list:
    """Long receipts are sliced into page-height chunks (one chunk in memory at a time)."""
    import cv2

    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return []
    h, w = img.shape[:2]
    target_w = min(avail_w * 0.6, 110 * 2.835 * 1.0)  # ~110 mm wide receipt column
    scale = target_w / w
    slice_px = int(avail_h / scale)
    out = []
    for y in range(0, h, slice_px):
        part = img[y:y + slice_px]
        ok, enc = cv2.imencode(".jpg", part, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            continue
        out.append(Image(io.BytesIO(enc.tobytes()), width=target_w, height=part.shape[0] * scale))
        if y + slice_px < h:
            out.append(PageBreak())
    return out
