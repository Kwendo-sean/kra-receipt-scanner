"""Render report dicts to HTML, PDF (ReportLab), XLSX (openpyxl) and CSV.

All three documents use the web app's look: off-white paper, near-black ink,
signal red accent, DM Sans + IBM Plex Mono labels, hairline rules."""
from __future__ import annotations

import base64
import csv
import io
import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.reports import kes

APP = Path(__file__).parent
TEMPLATES = APP / "templates"
STATIC = APP / "static"
FONTS = APP / "fonts"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

# the site's light theme (app.css :root)
INK, MUTED, DIM = "#0D0E0E", "#5F625E", "#8A8C87"
PAPER, SURFACE_2 = "#FBFAF6", "#E8E5DB"
LINE, LINE_SOFT = "#CFCDC6", "#E6E4DD"
ACCENT, OK, WARN = "#D8322C", "#3E7A43", "#A86B00"


def _fmt(v, money: bool) -> str:
    if v is None:
        return "—"
    if money and isinstance(v, (int, float)):
        return kes(v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _barw(v, rows) -> str:
    """Bar length (0-100) relative to the longest bar in the group."""
    top = max((r[1] for r in rows if isinstance(r[1], (int, float))), default=0)
    if not top or not isinstance(v, (int, float)) or v <= 0:
        return "0"
    return f"{max(1.0, 100.0 * v / top):.1f}"


env.filters["cell"] = _fmt
env.filters["barw"] = _barw


@lru_cache(maxsize=1)
def embedded_css() -> str:
    """app.css with its fonts inlined, so a saved or emailed report looks like the site offline."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    def font(m):
        data = base64.b64encode((STATIC / m.group(1)).read_bytes()).decode("ascii")
        return f'url("data:font/woff2;base64,{data}")'
    return re.sub(r'url\("(fonts/[\w.-]+\.woff2)"\)', font, css)


def to_html(rep: dict, image_url=None, standalone: bool = True, embed: bool = False) -> str:
    """image_url: callable(path) -> URL for on-screen images (auth-protected routes).
    embed: inline the stylesheet and fonts (files that leave the app: downloads, email)."""
    return env.get_template("report.html").render(rep=rep, image_url=image_url, standalone=standalone,
                                                  css=embedded_css() if embed else None)


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


def _sheet_name(name: str, used: set) -> str:
    name = name[:28]
    for ch in "[]:*?/\\":
        name = name.replace(ch, " ")
    base, n = name, 2
    while name in used:
        name = f"{base[:25]} {n}"
        n += 1
    used.add(name)
    return name


def to_xlsx(rep: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import DataBarRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    hex6 = lambda c: c.lstrip("#")  # noqa: E731
    head_fill = PatternFill("solid", fgColor=hex6(INK))
    head_font = Font(bold=True, color=hex6(PAPER), size=9)
    label_font = Font(color=hex6(DIM), size=8, bold=True)
    hair = Side(style="thin", color=hex6(LINE_SOFT))
    rule = Side(style="thin", color=hex6(LINE))
    strong = Side(style="medium", color=hex6(INK))
    wrap = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 30
    for col in "BCD":
        ws.column_dimensions[col].width = 24

    def put(row: list, font=None, fill=None, border=None, align=None):
        ws.append(row)
        for c in ws[ws.max_row]:
            if font:
                c.font = font
            if fill:
                c.fill = fill
            if border:
                c.border = border
            if align:
                c.alignment = align

    put([f"● {rep['kind'].replace('_', ' ').upper()} · COMPUTED IN PYTHON"], Font(color=hex6(ACCENT), size=8, bold=True))
    put([rep["title"]], Font(size=18, color=hex6(INK)))
    put([rep["subtitle"]], Font(size=12, color=hex6(INK)))
    put([f"Generated {rep['generated']} · {rep.get('prose_summary', '')}"], Font(size=8, color=hex6(MUTED)))
    ws.append([])
    for s in rep["sections"]:
        if not (s.get("prose") or s.get("kv") or s.get("stats") or s.get("bullets")) or s.get("prose_status") == "raw":
            continue
        put([s["heading"]], Font(size=12, bold=True, color=hex6(INK)), border=Border(bottom=rule))
        for col in range(2, 5):
            ws.cell(ws.max_row, col).border = Border(bottom=rule)
        if s.get("stats"):
            put([st[0].upper() for st in s["stats"]], label_font)
            put([st[1] for st in s["stats"]], Font(size=14, color=hex6(INK)))
            ws.cell(ws.max_row, 1).font = Font(size=14, color=hex6(ACCENT))
            put([st[2] for st in s["stats"]], Font(size=8, color=hex6(MUTED)), border=Border(bottom=hair))
        for para in (s.get("prose") or "").split("\n\n"):
            if not para:
                continue
            ws.append([para])
            r = ws.max_row
            ws.cell(r, 1).alignment = wrap
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
            ws.row_dimensions[r].height = max(30, 15 * (len(para) // 95 + 1))
        for i, b in enumerate(s.get("bullets") or [], start=1):
            ws.append([f"{i:02d}", b])
            r = ws.max_row
            ws.cell(r, 1).font = Font(color=hex6(ACCENT), bold=True, size=9)
            ws.cell(r, 1).alignment = Alignment(horizontal="right", vertical="top")
            ws.cell(r, 2).alignment = wrap
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
            ws.row_dimensions[r].height = max(16, 15 * (len(b) // 80 + 1))
        for k, v in s.get("kv", []):
            put([str(k).upper(), v], border=Border(bottom=hair))
            ws.cell(ws.max_row, 1).font = label_font
            ws.cell(ws.max_row, 2).alignment = wrap
        ws.append([])
    put([rep["footer"]], Font(size=8, color=hex6(DIM)))
    ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=4)
    ws.cell(ws.max_row, 1).alignment = wrap
    ws.row_dimensions[ws.max_row].height = 45

    used = {"Summary"}
    for s in rep["sections"]:
        for bars in s.get("bars") or []:
            if not bars["rows"]:
                continue
            sh = wb.create_sheet(_sheet_name(f"{s['heading'][:14]} - {bars['title']}" if bars.get("title")
                                             else s["heading"], used))
            sh.sheet_view.showGridLines = False
            sh.append(["Name", "Amount (KES)", "Shown"])
            for c in sh[1]:
                c.font, c.fill = head_font, head_fill
            for label, v, shown in bars["rows"]:
                sh.append([label, v, shown])
                for c in sh[sh.max_row]:
                    c.border = Border(bottom=hair)
            for c in sh["B"][1:]:
                c.number_format = "#,##0.00"
            sh.conditional_formatting.add(f"B2:B{sh.max_row}", DataBarRule(start_type="num", start_value=0,
                                                                          end_type="max", color=hex6(ACCENT)))
            sh.column_dimensions["A"].width, sh.column_dimensions["B"].width, sh.column_dimensions["C"].width = 36, 18, 28
        for t in s["tables"]:
            sh = wb.create_sheet(_sheet_name(s["heading"] if not t.get("title") else t["title"], used))
            sh.sheet_view.showGridLines = False
            sh.append(t["columns"])
            for c in sh[1]:
                c.font, c.fill = head_font, head_fill
                c.alignment = Alignment(vertical="center")
            sh.row_dimensions[1].height = 20
            for r in t["rows"]:
                sh.append(list(r))
                for i, c in enumerate(sh[sh.max_row]):
                    c.border = Border(bottom=hair)
                    if c.value in ("MISSING", "No", "FAIL"):
                        c.font = Font(color=hex6(ACCENT))
                    elif c.value in ("Yes", "PASS"):
                        c.font = Font(color=hex6(OK))
            if t.get("totals"):
                sh.append(list(t["totals"]))
                for c in sh[sh.max_row]:
                    c.font = Font(bold=True)
                    c.border = Border(top=strong)
            for i in t["money_cols"]:
                col = get_column_letter(i + 1)
                for c in sh[col][1:]:
                    c.number_format = "#,##0.00"
            for i, col in enumerate(t["columns"], start=1):
                width = max([len(str(col))] + [len(str(r[i - 1])) for r in t["rows"][:200] if i - 1 < len(r)])
                sh.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
            sh.freeze_panes = "A2"
            if t["rows"]:
                sh.auto_filter.ref = f"A1:{get_column_letter(len(t['columns']))}{1 + len(t['rows'])}"
    for sh in wb.worksheets:
        sh.sheet_properties.tabColor = hex6(ACCENT) if sh.title == "Summary" else hex6(INK)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ------------------------------------------------------------------ PDF


@lru_cache(maxsize=1)
def _pdf_fonts() -> dict:
    """Register the site's fonts with ReportLab (falls back to Helvetica/Courier if missing)."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    try:
        for name in ("DMSans-300", "DMSans-500", "IBMPlexMono-400", "IBMPlexMono-500"):
            pdfmetrics.registerFont(TTFont(name, str(FONTS / f"{name}.ttf")))
    except Exception:  # noqa: BLE001 - a missing font must not stop a report
        return {"sans": "Helvetica", "sans_b": "Helvetica-Bold", "mono": "Courier", "mono_b": "Courier-Bold"}
    for regular, bold in (("DMSans-300", "DMSans-500"), ("IBMPlexMono-400", "IBMPlexMono-500")):
        for italic in (0, 1):  # no italics bundled
            addMapping(regular, 0, italic, regular)
            addMapping(regular, 1, italic, bold)
    return {"sans": "DMSans-300", "sans_b": "DMSans-500", "mono": "IBMPlexMono-400", "mono_b": "IBMPlexMono-500"}


def _col_widths(columns: list, rows: list, avail: float) -> list[float]:
    """Share the width by how much text each column holds (capped, so one long column can't starve the rest)."""
    weights = []
    for i, c in enumerate(columns):
        longest = max([round(len(str(c)) * 1.25)] + [len(_fmt(r[i], False)) for r in rows[:80] if i < len(r)])
        weights.append(min(42, longest) + 4)  # + cell padding
    tot = sum(weights)
    return [avail * w / tot for w in weights]


def to_pdf(rep: dict) -> bytes:
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (CondPageBreak, Image, PageBreak, Paragraph, Preformatted,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)
    from xml.sax.saxutils import escape

    F = _pdf_fonts()
    c = colors.HexColor
    ink, muted, dim, accent = c(INK), c(MUTED), c(DIM), c(ACCENT)
    line, line_soft = c(LINE), c(LINE_SOFT)

    wide = any(len(t["columns"]) > 6 for s in rep["sections"] for t in s["tables"])
    pagesize = landscape(A4) if wide else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=15 * mm, bottomMargin=18 * mm, title=f"{rep['title']} · {rep['subtitle']}",
                            author="Receipt scanner")
    avail_w = pagesize[0] - doc.leftMargin - doc.rightMargin
    avail_h = pagesize[1] - doc.topMargin - doc.bottomMargin - 30

    def st(name, parent=None, **kw):
        if parent is not None:
            return ParagraphStyle(name, parent=parent, **kw)
        base = dict(fontName=F["sans"], fontSize=9.5, leading=14, textColor=ink)
        base.update(kw)
        return ParagraphStyle(name, **base)
    body = st("body", spaceAfter=5)
    cell = st("cell", fontSize=8, leading=10.5)
    cell_r = st("cellr", fontName=F["mono"], fontSize=7.5, leading=10.5, alignment=TA_RIGHT)
    th = st("th", fontName=F["mono_b"], fontSize=6.3, leading=8.5, textColor=dim)
    th_r = st("thr", parent=th, alignment=TA_RIGHT)
    label = st("label", fontName=F["mono_b"], fontSize=6.3, leading=8.5, textColor=dim)
    eyebrow = st("eyebrow", fontName=F["mono_b"], fontSize=7, leading=9, textColor=dim)
    h1 = st("h1", fontSize=23, leading=27, spaceBefore=5, spaceAfter=2)
    sub = st("sub", fontSize=12.5, leading=16)
    meta = st("meta", fontName=F["mono_b"], fontSize=6.3, leading=9, textColor=muted, spaceBefore=6)
    h2 = st("h2", fontName=F["sans_b"], fontSize=11.5, leading=14)
    code = st("code", fontName=F["mono_b"], fontSize=7, leading=14, textColor=dim, alignment=TA_RIGHT)
    h3 = st("h3", fontName=F["mono_b"], fontSize=6.5, leading=9, textColor=dim, spaceBefore=6, spaceAfter=3)
    tile_v = st("tilev", fontSize=14, leading=17)
    tile_v1 = st("tilev1", parent=tile_v, textColor=accent)
    tile_n = st("tilen", fontSize=7.3, leading=9.5, textColor=muted)
    badge_ok = st("bok", fontName=F["mono_b"], fontSize=6.2, leading=9, textColor=c(OK))
    badge_warn = st("bwarn", fontName=F["mono_b"], fontSize=6.2, leading=9, textColor=c(WARN))
    step_n = st("stepn", fontName=F["mono_b"], fontSize=7, leading=13, textColor=accent)

    def P(text, style):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    def U(text, style):  # mono labels are uppercase on the site
        return P(str(text).upper(), style)

    rule_below = lambda color, w=0.5: [("LINEBELOW", (0, 0), (-1, -1), w, color)]  # noqa: E731
    story = [Paragraph(f'<font color="{ACCENT}">•</font> / ' + escape(
                 {"vat_schedule": "VAT3 PURCHASES", "compliance": "COMPLIANCE", "spending": "SPENDING",
                  "receipt": "RECEIPT", "narrative": "MONTHLY BRIEF"}.get(rep["kind"], "REPORT") + " · COMPUTED IN PYTHON"),
                 eyebrow),
             P(rep["title"], h1), P(rep["subtitle"], sub),
             U(f"Generated {rep['generated']} · {rep.get('prose_summary', '')}", meta), Spacer(1, 8)]
    head_rule = Table([[""]], colWidths=[avail_w], rowHeights=[1])
    head_rule.setStyle(TableStyle(rule_below(line, 0.8)))
    story.append(head_rule)

    for n, s in enumerate(rep["sections"], start=1):
        head = Table([[P(s["heading"], h2), P(f"{n:02d}", code)]], colWidths=[avail_w * 0.85, avail_w * 0.15])
        head.setStyle(TableStyle(rule_below(line_soft) + [("LEFTPADDING", (0, 0), (-1, -1), 0),
                                                         ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                                         ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [CondPageBreak(60 * mm), Spacer(1, 12), head, Spacer(1, 7)]

        if s.get("stats"):
            k = len(s["stats"])
            cells = [[[U(a, label), Spacer(1, 3), P(b, tile_v1 if i == 0 else tile_v)] + ([P(n_, tile_n)] if n_ else [])
                      for i, (a, b, n_) in enumerate(s["stats"])]]
            t = Table(cells, colWidths=[avail_w / k] * k)
            t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, line), ("INNERGRID", (0, 0), (-1, -1), 0.4, line_soft),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
            story += [t, Spacer(1, 9)]

        if s.get("prose"):
            if s.get("prose_status") == "raw":
                story.append(Preformatted(s["prose"], ParagraphStyle("pre", fontName=F["mono"], fontSize=6.8,
                                                                     leading=8.6, textColor=ink)))
            else:
                for para in s["prose"].split("\n\n"):
                    story.append(P(para, body))
                if s.get("prose_status") in ("template_rejected", "template_offline"):
                    story.append(U("Template text (" + ("model output failed number check"
                                   if s["prose_status"] == "template_rejected" else "model unavailable") + ")", badge_warn))
                elif s.get("prose_status") in ("llm", "llm_retry"):
                    story.append(U("Written by local model · numbers verified", badge_ok))

        for bars in s.get("bars") or []:
            if not bars["rows"]:
                continue
            if bars.get("title"):
                story.append(U(bars["title"], h3))
            top = max((r[1] for r in bars["rows"]), default=0) or 1
            data, style = [], [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]
            for i, (lbl, v, shown) in enumerate(bars["rows"]):
                d = Drawing(avail_w, 3.2)
                d.add(Rect(0, 0, avail_w, 3.2, fillColor=c(SURFACE_2), strokeColor=None))
                d.add(Rect(0, 0, max(1.5, avail_w * v / top), 3.2, fillColor=accent, strokeColor=None))
                data += [[P(lbl, cell), P(shown, cell_r)], [d, ""]]
                r0 = 2 * i
                style += [("SPAN", (0, r0 + 1), (1, r0 + 1)), ("TOPPADDING", (0, r0), (-1, r0), 5),
                          ("BOTTOMPADDING", (0, r0), (-1, r0), 2), ("BOTTOMPADDING", (0, r0 + 1), (-1, r0 + 1), 5),
                          ("LINEBELOW", (0, r0 + 1), (-1, r0 + 1), 0.4, line_soft)]
            t = Table(data, colWidths=[avail_w * 0.62, avail_w * 0.38])
            t.setStyle(TableStyle(style))
            story += [t, Spacer(1, 6)]

        if s.get("bullets"):
            data = [[P(f"{i:02d}", step_n), P(b, body)] for i, b in enumerate(s["bullets"], start=1)]
            t = Table(data, colWidths=[11 * mm, avail_w - 11 * mm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), 0.4, line_soft),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 5)]))
            story.append(t)

        if s.get("kv"):
            data = [[U(k, label), P(v, st("kvv", fontSize=8.5, leading=11.5,
                                          textColor=accent if v == "MISSING" else ink))] for k, v in s["kv"]]
            t = Table(data, colWidths=[avail_w * 0.32, avail_w * 0.68])
            t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, line_soft),
                                   ("LINEABOVE", (0, 0), (-1, 0), 0.4, line_soft),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story += [Spacer(1, 4), t]

        for tbl in s["tables"]:
            if tbl.get("title"):
                story.append(U(tbl["title"], h3))
            mc = set(tbl["money_cols"])

            def row_cells(r, bold=False):
                out = []
                for i, v in enumerate(r):
                    txt = escape(_fmt(v, i in mc))
                    color = ACCENT if v in ("MISSING", "No", "FAIL") else OK if v in ("Yes", "PASS") else None
                    if color:
                        txt = f'<font color="{color}">{txt}</font>'
                    if bold:
                        txt = f"<b>{txt}</b>"
                    out.append(Paragraph(txt, cell_r if i in mc else cell))
                return out
            data = [[U(col, th_r if i in mc else th) for i, col in enumerate(tbl["columns"])]]
            data += [row_cells(r) for r in tbl["rows"]] or [[P("No rows", st("e", fontSize=8, textColor=muted))]
                                                             + [""] * (len(tbl["columns"]) - 1)]
            if tbl.get("totals"):
                data.append(row_cells(tbl["totals"], bold=True))
            t = Table(data, repeatRows=1, colWidths=_col_widths(tbl["columns"], tbl["rows"], avail_w))
            style = [("LINEBELOW", (0, 0), (-1, 0), 0.6, line), ("LINEBELOW", (0, 1), (-1, -1), 0.35, line_soft),
                     ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                     ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (0, -1), 0)]
            if not tbl["rows"]:
                style.append(("SPAN", (0, 1), (-1, 1)))
            if tbl.get("totals"):
                style.append(("LINEABOVE", (0, -1), (-1, -1), 0.9, ink))
            t.setStyle(TableStyle(style))
            story += [Spacer(1, 3), t]

        for p in s.get("images", []):
            story += _pdf_images(p, avail_w, avail_h, Image, PageBreak)
    story.append(Spacer(1, 10))  # disclaimer is drawn in every page footer

    def on_page(canvas, d):
        canvas.saveState()
        canvas.setFillColor(c(PAPER))
        canvas.rect(0, 0, pagesize[0], pagesize[1], stroke=0, fill=1)
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, 12.5 * mm, pagesize[0] - doc.rightMargin, 12.5 * mm)
        canvas.setFont(F["mono"], 5.6)
        canvas.setFillColor(dim)
        foot = rep["footer"]
        width = avail_w - 22 * mm
        lines, cur = [], ""
        for word in foot.split():
            nxt = (cur + " " + word).strip()
            if canvas.stringWidth(nxt, F["mono"], 5.6) > width and cur:
                lines.append(cur)
                cur = word
            else:
                cur = nxt
        lines.append(cur)
        for i, ln in enumerate(lines[:3]):
            canvas.drawString(doc.leftMargin, 9.5 * mm - i * 2.6 * mm, ln)
        canvas.setFont(F["mono_b"], 6.3)
        canvas.setFillColor(accent)
        canvas.drawRightString(pagesize[0] - doc.rightMargin, 9.5 * mm, f"{d.page:02d}")
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
