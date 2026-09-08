"""Deterministic PDF generation with ReportLab.

Four documents share one visual identity — the brand's, taken to print:

  - analysis_report:  the insights screen, block by block, as a document
  - financial_report: KPIs, charts and tables for a period
  - invoice:          a billing document for an order
  - receipt:          an expense voucher, optionally linking its stored file

Design language, applied everywhere: a deep-green masthead with the company
name, serif display type over a quiet sans body, justified prose, tables ruled
horizontally (never caged in grids), KPI cards with an accent keyline, and
REAL vector charts — pies, bars and lines drawn with the same palette the web
uses, not typographic approximations.

Every value arrives already computed; this module only lays out pages.
"""
from __future__ import annotations

import io
from datetime import date
from html import escape

from reportlab.graphics.shapes import (
    Circle, Drawing, Line, PolyLine, Rect, String, Wedge,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from ..config import COMPANY_NAME, COMPANY_TAX_ID

# --- palette: the web's, in print ------------------------------------------
ACCENT = colors.HexColor("#0f9d63")
INK = colors.HexColor("#122019")
MUTED = colors.HexColor("#5a6b62")
FAINT = colors.HexColor("#8fa098")
LINE = colors.HexColor("#d6e3dc")
SOFT = colors.HexColor("#f2f9f5")
BAND = colors.HexColor("#0c1f16")           # masthead background
SERIES = [colors.HexColor(c) for c in
          ("#12b877", "#4d8dff", "#dfb15e", "#e88a80", "#9b8cff", "#4bc6c6")]

CONTENT_W = A4[0] - 36 * mm                  # printable width with 18mm margins

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("title", parent=_styles["Title"], fontName="Times-Bold",
                       fontSize=21, leading=25, textColor=colors.white,
                       alignment=0, spaceAfter=0)
SUBTITLE = ParagraphStyle("subtitle", parent=_styles["Normal"], fontName="Helvetica",
                          fontSize=9, textColor=colors.HexColor("#bcd3c7"))
BRAND = ParagraphStyle("brand", parent=_styles["Normal"], fontName="Helvetica-Bold",
                       fontSize=8, textColor=colors.HexColor("#7fbfa3"),
                       spaceAfter=4)
HEADING = ParagraphStyle("heading", parent=_styles["Heading2"], fontName="Times-Bold",
                         fontSize=14, textColor=INK, spaceBefore=0, spaceAfter=0)
HEADING_DETAIL = ParagraphStyle("hdetail", parent=_styles["Normal"], fontName="Helvetica",
                                fontSize=8.5, textColor=MUTED, leading=12)
BODY = ParagraphStyle("body", parent=_styles["Normal"], fontName="Helvetica",
                      fontSize=9.5, textColor=INK, leading=15.5,
                      alignment=TA_JUSTIFY, spaceAfter=2)
LEAD = ParagraphStyle("lead", parent=BODY, fontName="Times-Roman", fontSize=11,
                      leading=17, textColor=INK)
SMALL = ParagraphStyle("small", parent=_styles["Normal"], fontName="Helvetica",
                       fontSize=7.8, textColor=MUTED, leading=11)
KPI_LABEL = ParagraphStyle("kpilabel", parent=_styles["Normal"], fontName="Helvetica-Bold",
                           fontSize=6.8, textColor=MUTED, spaceAfter=3)
KPI_VALUE = ParagraphStyle("kpivalue", parent=_styles["Normal"], fontName="Times-Bold",
                           fontSize=15, leading=17, textColor=INK)
KPI_DELTA = ParagraphStyle("kpidelta", parent=_styles["Normal"], fontName="Helvetica",
                           fontSize=7.5, textColor=ACCENT, leading=10)
TOTAL_STYLE = ParagraphStyle("total", parent=_styles["Normal"], fontName="Times-Bold",
                             fontSize=15, textColor=colors.white, alignment=TA_RIGHT)


# ------------------------------------------------------------------ chrome

def _document(buffer: io.BytesIO, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buffer, pagesize=A4, title=title, author=COMPANY_NAME,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm, bottomMargin=18 * mm,
    )


def _masthead(title: str, subtitle: str) -> list:
    """The document's opening: a deep-green band with the brand and title."""
    tax = f" · NIT {COMPANY_TAX_ID}" if COMPANY_TAX_ID else ""
    inner = Table(
        [[Paragraph(f"{COMPANY_NAME.upper()}{tax}", BRAND)],
         [Paragraph(escape(title), TITLE)],
         [Paragraph(escape(subtitle), SUBTITLE)]],
        colWidths=[CONTENT_W - 20 * mm],
    )
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    band = Table([[inner]], colWidths=[CONTENT_W])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 10 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 8 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7 * mm),
    ]))
    return [band,
            HRFlowable(width="100%", thickness=2, color=ACCENT, spaceBefore=0,
                       spaceAfter=14)]


def _footer(canvas, doc):
    """A quiet rule, the confidentiality line and the page number."""
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm,
                      f"{COMPANY_NAME} · documento generado el {date.today().isoformat()} · "
                      "uso interno")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Página {doc.page}")
    canvas.restoreState()


def _section(title: str, detail: str = "") -> list:
    """Serif heading over a short accent rule — the section signature."""
    parts = [Spacer(0, 12), Paragraph(escape(title), HEADING),
             HRFlowable(width=16 * mm, thickness=2, color=ACCENT, hAlign="LEFT",
                        spaceBefore=3, spaceAfter=4)]
    if detail:
        parts.append(Paragraph(escape(detail), HEADING_DETAIL))
    parts.append(Spacer(0, 4))
    return parts


# --------------------------------------------------------------- components

def _kpi_cards(items: list[dict]) -> Table:
    """One card per indicator: keyline on top, serif value, quiet delta."""
    cells = []
    for item in items:
        cells.append([
            Paragraph(escape(str(item.get("label", ""))).upper(), KPI_LABEL),
            Paragraph(escape(str(item.get("value", ""))), KPI_VALUE),
            Paragraph(escape(str(item.get("delta", "") or "")), KPI_DELTA),
        ])
    row = [[Table([[c[0]], [c[1]], [c[2]]],
                  colWidths=[CONTENT_W / len(cells) - 6],
                  style=TableStyle([
                      ("LEFTPADDING", (0, 0), (-1, -1), 0),
                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                      ("TOPPADDING", (0, 0), (-1, -1), 0),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                  ]))
            for c in cells]]
    grid = Table(row, colWidths=[CONTENT_W / len(cells)] * len(cells))
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SOFT),
        ("LINEABOVE", (0, 0), (-1, 0), 2, ACCENT),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return grid


def _data_table(header: list[str], rows: list[list], widths=None,
                align_right: set[int] | None = None) -> Table:
    """Financial-report table: ruled horizontally, zebra, no cage."""
    align_right = align_right or set()
    table = Table([header] + rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.6),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 8.6),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
    ]
    for column in align_right:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    return table


def _findings(items: list[dict]) -> list:
    """Readings list: a colored dot per tone, bold claim, justified detail."""
    tones = {"warning": colors.HexColor("#c98a1b"), "danger": colors.HexColor("#c0463c"),
             "info": ACCENT, "success": ACCENT}
    out = []
    for item in items:
        dot = Drawing(8, 8)
        dot.add(Circle(4, 4, 2.6, fillColor=tones.get(item.get("tone", "info"), ACCENT),
                       strokeColor=None))
        text = Paragraph(
            f'<b>{escape(str(item.get("title", "")))}</b>'
            + (f'<br/><font color="#5a6b62">{escape(str(item["detail"]))}</font>'
               if item.get("detail") else ""), BODY)
        row = Table([[dot, text]], colWidths=[6 * mm, CONTENT_W - 6 * mm])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        out.append(row)
    return out


# ------------------------------------------------------------ vector charts

def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        text = f"{value / 1000:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".")
        return f"{text} k"
    return f"{value:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".")


def _chart_drawing(kind: str, series: list[dict]) -> Drawing | None:
    """A real vector chart from a {label, value, display} series."""
    series = [s for s in series if isinstance(s.get("value"), (int, float))]
    if len(series) < 2:
        return None
    if kind == "pie" and len(series) <= 6:
        return _pie_drawing(series)
    if kind == "line":
        return _line_drawing(series)
    return _hbar_drawing(series)


def _pie_drawing(series: list[dict]) -> Drawing:
    width, height = CONTENT_W, 150
    drawing = Drawing(width, height)
    total = sum(max(s["value"], 0) for s in series) or 1
    cx, cy, radius = width * 0.24, height / 2, 62

    start = 90.0
    for i, point in enumerate(series):
        sweep = max(point["value"], 0) / total * 360
        drawing.add(Wedge(cx, cy, radius, start - sweep, start,
                          fillColor=SERIES[i % len(SERIES)],
                          strokeColor=colors.white, strokeWidth=1.2))
        start -= sweep

    legend_x = width * 0.47
    top = cy + (len(series) * 20) / 2 - 10
    for i, point in enumerate(series):
        y = top - i * 20
        pct = max(point["value"], 0) / total * 100
        drawing.add(Rect(legend_x, y - 4, 9, 9, fillColor=SERIES[i % len(SERIES)],
                         strokeColor=None))
        label = f'{point["label"]} — {point.get("display", _fmt(point["value"]))} ' \
                f'({pct:.1f}%)'.replace(".", ",")
        drawing.add(String(legend_x + 15, y - 3, label, fontName="Helvetica",
                           fontSize=8.5, fillColor=INK))
    return drawing


def _hbar_drawing(series: list[dict]) -> Drawing:
    series = series[:10]
    row_h = 20
    width = CONTENT_W
    height = len(series) * row_h + 8
    drawing = Drawing(width, height)
    label_w, value_w = width * 0.30, 58
    plot_w = width - label_w - value_w - 16
    top_value = max((s["value"] for s in series), default=0) or 1

    for i, point in enumerate(series):
        y = height - (i + 1) * row_h + 4
        bar_w = max(max(point["value"], 0) / top_value * plot_w, 1.5)
        drawing.add(String(label_w - 6, y + 3, str(point["label"])[:34],
                           fontName="Helvetica", fontSize=8.5, fillColor=INK,
                           textAnchor="end"))
        drawing.add(Rect(label_w, y, bar_w, row_h - 8, rx=2, ry=2,
                         fillColor=SERIES[i % len(SERIES)], strokeColor=None))
        drawing.add(String(label_w + bar_w + 5, y + 3,
                           str(point.get("display", _fmt(point["value"]))),
                           fontName="Helvetica-Bold", fontSize=8, fillColor=MUTED))
    return drawing


def _line_drawing(series: list[dict]) -> Drawing:
    series = series[:30]
    width, height = CONTENT_W, 130
    drawing = Drawing(width, height)
    left, bottom, top_pad = 14, 22, 14
    plot_w, plot_h = width - left - 12, height - bottom - top_pad
    top_value = max((s["value"] for s in series), default=0) or 1

    step = plot_w / max(len(series) - 1, 1)
    points = []
    for i, point in enumerate(series):
        x = left + step * i
        y = bottom + max(point["value"], 0) / top_value * plot_h
        points.append((x, y))

    for i in range(3):
        gy = bottom + plot_h / 2 * i
        drawing.add(Line(left, gy, left + plot_w, gy, strokeColor=LINE, strokeWidth=0.5))

    flat = [c for xy in points for c in xy]
    drawing.add(PolyLine(flat, strokeColor=ACCENT, strokeWidth=1.8))
    for (x, y), point in zip(points, series):
        drawing.add(Circle(x, y, 2, fillColor=ACCENT, strokeColor=None))
    labels = max(len(series) // 8, 1)
    for i, ((x, _), point) in enumerate(zip(points, series)):
        if i % labels == 0:
            drawing.add(String(x, 6, str(point["label"]), fontName="Helvetica",
                               fontSize=7.5, fillColor=MUTED, textAnchor="middle"))
    return drawing


# ---------------------------------------------------------------- documents

def analysis_report(title: str, subtitle: str, blocks: list[dict]) -> bytes:
    """The insights screen, block by block, in the print identity.

    Same source, same order: what the library keeps is what the screen showed,
    with each chart redrawn as true vector art from its own series.
    """
    buffer = io.BytesIO()
    doc = _document(buffer, title)
    story = _masthead(title, subtitle)

    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            story.extend(_section(block["content"], block.get("detail", "")))
        elif kind == "text":
            story.append(Spacer(0, 4))
            story.append(Paragraph(escape(block["content"]), LEAD))
        elif kind == "notice":
            story.append(Paragraph(escape(block["content"]), SMALL))
        elif kind == "kpis" and block.get("items"):
            story.append(Spacer(0, 6))
            story.append(_kpi_cards(block["items"][:4]))
        elif kind == "table" and block.get("rows"):
            columns = block["columns"]
            rows = [[str(row.get(col["key"], "")) for col in columns]
                    for row in block["rows"]]
            align = {i for i, col in enumerate(columns) if col.get("align") == "right"}
            story.append(Spacer(0, 6))
            story.append(_data_table([c["label"] for c in columns], rows,
                                     align_right=align))
            if block.get("caption"):
                story.append(Spacer(0, 3))
                story.append(Paragraph(escape(block["caption"]), SMALL))
        elif kind == "list" and block.get("items"):
            story.extend(_findings(block["items"]))
        elif kind == "chart" and block.get("series"):
            drawing = _chart_drawing(block.get("chart_type", "bar"), block["series"])
            if drawing is not None:
                story.append(KeepTogether([
                    Spacer(0, 10),
                    Paragraph(escape(block.get("title") or "Distribución"),
                              ParagraphStyle("charttitle", parent=HEADING, fontSize=11)),
                    Spacer(0, 6),
                    drawing,
                ]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def financial_report(data: dict) -> bytes:
    """Build the period report PDF from already-computed figures."""
    buffer = io.BytesIO()
    doc = _document(buffer, data["title"])
    story = _masthead(data["title"], data["subtitle"])

    story.append(_kpi_cards(data["kpis"]))

    if data.get("summary"):
        story.extend(_section("Resumen ejecutivo"))
        story.append(Paragraph(escape(data["summary"]), LEAD))

    for section in data.get("charts", []):
        series = [{"label": p["label"], "value": p["value"],
                   "display": p.get("display", "")} for p in section["series"]]
        drawing = _chart_drawing("hbar", series)
        if drawing is not None:
            story.append(KeepTogether(_section(section["title"]) + [drawing]))

    for section in data.get("tables", []):
        story.extend(_section(section["title"]))
        story.append(_data_table(section["columns"], section["rows"],
                                 align_right=set(section.get("align_right", []))))

    if data.get("notes"):
        story.extend(_section("Observaciones"))
        story.extend(_findings([{"title": note, "tone": "warning"}
                                for note in data["notes"]]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def invoice(data: dict) -> bytes:
    """Billing document for one order."""
    buffer = io.BytesIO()
    doc = _document(buffer, data["title"])
    story = _masthead(data["title"], data["subtitle"])

    meta = [["Cliente", data["customer"]],
            ["Documento", data["number"]],
            ["Fecha", data["date"]],
            ["Estado", data["status"]]]
    if data.get("gateway"):
        meta.append(["Pasarela", data["gateway"]])
    story.extend(_section("Datos del documento"))
    story.append(_data_table(["Campo", "Detalle"], meta,
                             widths=[45 * mm, CONTENT_W - 45 * mm]))

    story.extend(_section("Detalle"))
    story.append(_data_table(
        ["Concepto", "Moneda", "Monto", "USD eq."], data["lines"],
        widths=[CONTENT_W - 94 * mm, 24 * mm, 35 * mm, 35 * mm], align_right={2, 3}))

    story.append(Spacer(0, 10))
    total = Table(
        [[Paragraph("TOTAL", ParagraphStyle("tl", parent=KPI_LABEL,
                                            textColor=colors.HexColor("#bcd3c7"))),
          Paragraph(escape(str(data["total"])), TOTAL_STYLE)]],
        colWidths=[CONTENT_W - 70 * mm, 70 * mm])
    total.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("LINEABOVE", (0, 0), (-1, 0), 2, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(total)

    if data.get("notes"):
        story.extend(_section("Notas"))
        for note in data["notes"]:
            story.append(Paragraph(escape(note), BODY))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def receipt(data: dict) -> bytes:
    """Expense voucher, including a link to the stored receipt file if present."""
    buffer = io.BytesIO()
    doc = _document(buffer, data["title"])
    story = _masthead(data["title"], data["subtitle"])

    story.extend(_section("Detalle del gasto"))
    story.append(KeepTogether(_data_table(
        ["Campo", "Detalle"], data["fields"], widths=[45 * mm, CONTENT_W - 45 * mm])))

    story.extend(_section("Comprobante adjunto"))
    if data.get("file_url"):
        story.append(Paragraph(
            f'<link href="{data["file_url"]}" color="#0f9d63">{data["file_url"]}</link>', BODY))
    else:
        story.append(Paragraph("Sin archivo cargado.", BODY))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
