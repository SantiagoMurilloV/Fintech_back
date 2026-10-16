"""Deterministic SVG chart rendering.

Charts are drawn from raw series with plain string building: the same input
always produces byte-identical output, there is no plotting dependency, and
the result embeds directly in the web chat and in saved reports.

Colours are written as plain attributes (the brand palette) so the SVG reads
anywhere — a PDF, an e-mail. Every grid line and label also carries a class
(`c-grid`, `c-text`, `c-value`) and the root says `data-themed`, so the web
app can repaint them with its theme: on the dark theme a chart sits on the
card itself instead of on a white plate.
"""
from __future__ import annotations

from html import escape

# Brand palette (mirrors the frontend accent scale).
SERIES_COLORS = ["#12b877", "#4d8dff", "#dfb15e", "#e88a80", "#9b8cff", "#4bc6c6"]
GRID = "#d6e3dc"
AXIS_TEXT = "#5a6b62"
TITLE_TEXT = "#122019"
MUTED_FILL = "#c3d3ca"

CHART_TYPES = ["bar", "line", "pie", "hbar", "box", "stacked"]

# Colours that mean something: an order's state reads the same in every chart.
STATUS_COLORS = {
    "approved": "#12b877",
    "pending": "#dfb15e",
    "rejected": "#e88a80",
    "refunded": "#9b8cff",
}
OTHER_COLOR = "#8a94a3"


def _fmt(value: float) -> str:
    """Compact number label: 12500 -> '12,5 k'."""
    if abs(value) >= 1000:
        return f"{value / 1000:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".") + " k"
    return f"{value:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".")


def _text(x, y, content, size=11, fill=AXIS_TEXT, anchor="middle", weight="400",
          role="text"):
    """`role` names the class the app themes: «text» for axes and legends,
    «value» for the figures written on the marks."""
    return (f'<text class="c-{role}" x="{x:.1f}" y="{y:.1f}" '
            f'font-family="IBM Plex Sans, sans-serif" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(str(content))}</text>')


def _label(point: dict) -> str:
    """The figure written on a mark: the caller's own wording when it gave one
    («733», «USD 12,4 k»), the compact number otherwise."""
    return point.get("display") or _fmt(point["value"])


def _color(point: dict, i: int) -> str:
    return point.get("color") or SERIES_COLORS[i % len(SERIES_COLORS)]


def render(chart_type: str, series: list[dict], title: str = "", width: int = 720,
           height: int = 320) -> str:
    """Render a chart to an SVG string.

    `series` is a list of {label, value} dicts; optional `muted: True` dims a
    point (used for partial periods).
    """
    if chart_type not in CHART_TYPES:
        raise ValueError(f"chart_type debe ser uno de: {', '.join(CHART_TYPES)}")
    if not series:
        raise ValueError("La serie del gráfico está vacía.")
    if chart_type == "hbar" and height == 320:
        # One row per category: ten clients need more room than three states.
        height = max(len(series) * 38 + 36, 120)

    if chart_type == "stacked":
        body = _stacked(series, width, height)
    elif chart_type == "bar":
        body = _bars(series, width, height)
    elif chart_type == "hbar":
        body = _hbars(series, width, height)
    elif chart_type == "line":
        body = _line(series, width, height)
    elif chart_type == "box":
        body = _box(series, width, height)
    else:
        body = _pie(series, width, height)

    heading = _text(width / 2, 26, title, size=17, fill=TITLE_TEXT, weight="600") if title else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" role="img" data-themed="1" '
        f'aria-label="{escape(title or chart_type)}">'
        f'{heading}{body}</svg>'
    )


# --------------------------------------------------------------------- bars

def _plot_area(width: int, height: int, left=64, right=18, top=44, bottom=46):
    return left, top, width - left - right, height - top - bottom


def _bars(series: list[dict], width: int, height: int) -> str:
    left, top, plot_w, plot_h = _plot_area(width, height)
    top_value = max((s["value"] for s in series), default=0) or 1
    parts = [_grid(left, top, plot_w, plot_h, top_value)]

    slot = plot_w / len(series)
    bar_w = min(slot * 0.55, 56)
    for i, point in enumerate(series):
        value = max(point["value"], 0)
        bar_h = (value / top_value) * plot_h
        x = left + slot * i + (slot - bar_w) / 2
        y = top + plot_h - bar_h
        color = MUTED_FILL if point.get("muted") else (point.get("color") or SERIES_COLORS[0])
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                     f'rx="4" fill="{color}"/>')
        parts.append(_text(x + bar_w / 2, y - 8, _label(point), size=14, weight="600",
                           role="value"))
        parts.append(_text(x + bar_w / 2, top + plot_h + 22, point["label"], size=14))
    return "".join(parts)


def _hbars(series: list[dict], width: int, height: int) -> str:
    """Horizontal bars — better for long category labels."""
    left, top, plot_w, plot_h = _plot_area(width, height, left=180, right=130, top=12, bottom=12)
    top_value = max((s["value"] for s in series), default=0) or 1
    parts = []
    slot = plot_h / len(series)
    bar_h = min(slot * 0.6, 26)
    for i, point in enumerate(series):
        value = max(point["value"], 0)
        bar_w = (value / top_value) * plot_w
        y = top + slot * i + (slot - bar_h) / 2
        color = point.get("color") or SERIES_COLORS[0]
        parts.append(f'<rect x="{left}" y="{y:.1f}" width="{max(bar_w, 2):.1f}" height="{bar_h:.1f}" '
                     f'rx="4" fill="{color}"/>')
        parts.append(_text(left - 12, y + bar_h / 2 + 5, point["label"], size=15, anchor="end"))
        # A long figure («733 · USD 456,7 k») moves inside a bar that fills the row.
        label = _label(point)
        inside = bar_w > plot_w - 9 * len(label)
        parts.append(_text(left + bar_w - 10 if inside else left + bar_w + 10,
                           y + bar_h / 2 + 5, label, size=14,
                           anchor="end" if inside else "start", weight="600",
                           fill="#052015" if inside else AXIS_TEXT,
                           role="inside" if inside else "value"))
    return "".join(parts)


def _line(series: list[dict], width: int, height: int) -> str:
    left, top, plot_w, plot_h = _plot_area(width, height)
    top_value = max((s["value"] for s in series), default=0) or 1
    parts = [_grid(left, top, plot_w, plot_h, top_value)]

    step = plot_w / max(len(series) - 1, 1)
    points = []
    for i, point in enumerate(series):
        x = left + step * i
        y = top + plot_h - (max(point["value"], 0) / top_value) * plot_h
        points.append((x, y))

    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
    area = (f'M{points[0][0]:.1f},{top + plot_h:.1f} ' +
            " ".join(f"L{x:.1f},{y:.1f}" for x, y in points) +
            f' L{points[-1][0]:.1f},{top + plot_h:.1f} Z')
    parts.append(f'<path d="{area}" fill="{SERIES_COLORS[0]}" opacity="0.12"/>')
    parts.append(f'<path d="{path}" fill="none" stroke="{SERIES_COLORS[0]}" stroke-width="2.5" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
    for (x, y), point in zip(points, series):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{SERIES_COLORS[0]}"/>')
        parts.append(_text(x, y - 12, _label(point), size=13, weight="600", role="value"))
        parts.append(_text(x, top + plot_h + 22, point["label"], size=14))
    return "".join(parts)


def _pie(series: list[dict], width: int, height: int) -> str:
    import math

    total = sum(max(s["value"], 0) for s in series) or 1
    cx, cy = width * 0.30, height * 0.52
    radius = min(width * 0.25, height * 0.38)
    parts = []
    angle = -math.pi / 2  # start at 12 o'clock

    for i, point in enumerate(series):
        fraction = max(point["value"], 0) / total
        sweep = fraction * 2 * math.pi
        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        angle += sweep
        x2, y2 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        large = 1 if sweep > math.pi else 0
        color = _color(point, i)
        parts.append(f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                     f'A{radius:.1f},{radius:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>')

    # Legend on the right side.
    legend_x = width * 0.58
    start_y = max(height * 0.5 - len(series) * 14, 26)
    for i, point in enumerate(series):
        y = start_y + i * 28
        pct = max(point["value"], 0) / total * 100
        parts.append(f'<rect x="{legend_x}" y="{y - 11}" width="14" height="14" rx="4" '
                     f'fill="{_color(point, i)}"/>')
        parts.append(_text(legend_x + 22, y, f'{point["label"]} — {pct:.1f}%'.replace(".", ","),
                           size=15, anchor="start"))
    return "".join(parts)


def _box(series: list[dict], width: int, height: int) -> str:
    """Horizontal box-and-whisker plot.

    Each point is {label, min, q1, med, q3, max}: the drawing of the same
    five numbers the descriptive table reports, so the skew that a CV of 180%
    describes can simply be SEEN — a thin box near zero with a long whisker.
    """
    left, top, plot_w, plot_h = _plot_area(width, height, left=140, right=40,
                                           top=24, bottom=48)
    top_value = max((point["max"] for point in series), default=0) or 1

    def x_of(value: float) -> float:
        return left + (max(value, 0) / top_value) * plot_w

    parts = []
    # Vertical reference grid with value labels along the bottom.
    for i in range(5):
        gx = left + plot_w / 4 * i
        parts.append(f'<line class="c-grid" x1="{gx:.1f}" y1="{top}" x2="{gx:.1f}" '
                     f'y2="{top + plot_h:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(gx, top + plot_h + 24, _fmt(top_value / 4 * i), size=13))

    slot = plot_h / len(series)
    box_h = min(slot * 0.5, 44)
    for i, point in enumerate(series):
        cy = top + slot * i + slot / 2
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        x_min, x_q1 = x_of(point["min"]), x_of(point["q1"])
        x_med, x_q3, x_max = x_of(point["med"]), x_of(point["q3"]), x_of(point["max"])

        parts.append(_text(left - 12, cy + 5, point["label"], size=15, anchor="end"))
        # Whiskers with end caps.
        for x1, x2 in ((x_min, x_q1), (x_q3, x_max)):
            parts.append(f'<line x1="{x1:.1f}" y1="{cy:.1f}" x2="{x2:.1f}" y2="{cy:.1f}" '
                         f'stroke="{color}" stroke-width="2"/>')
        for x in (x_min, x_max):
            parts.append(f'<line x1="{x:.1f}" y1="{cy - box_h * 0.3:.1f}" x2="{x:.1f}" '
                         f'y2="{cy + box_h * 0.3:.1f}" stroke="{color}" stroke-width="2"/>')
        # The box (Q1..Q3) and the median line.
        parts.append(f'<rect x="{x_q1:.1f}" y="{cy - box_h / 2:.1f}" '
                     f'width="{max(x_q3 - x_q1, 2):.1f}" height="{box_h:.1f}" rx="4" '
                     f'fill="{color}" opacity="0.25" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<line x1="{x_med:.1f}" y1="{cy - box_h / 2:.1f}" x2="{x_med:.1f}" '
                     f'y2="{cy + box_h / 2:.1f}" stroke="{color}" stroke-width="3"/>')
        parts.append(_text(x_med, cy - box_h / 2 - 8, f"med {_fmt(point['med'])}",
                           size=13, weight="600", role="value"))
        parts.append(_text(x_max, cy - box_h * 0.3 - 8, _fmt(point["max"]), size=12))
    return "".join(parts)


def _stacked(series: list[dict], width: int, height: int) -> str:
    """Columns split into coloured parts, with a legend on top.

    Each point is {label, parts: [{name, value, color}], display}: one column
    per period, each part one state of the orders, so a month's volume and how
    much of it was rejected read in the same glance.
    """
    left, top, plot_w, plot_h = _plot_area(width, height, top=58, bottom=40)
    totals = [sum(max(part["value"], 0) for part in point["parts"]) for point in series]
    top_value = max(totals, default=0) or 1
    parts = [_grid(left, top, plot_w, plot_h, top_value)]

    # Legend: every part name once, in the order of the first column.
    names: list[tuple[str, str]] = []
    for point in series:
        for part in point["parts"]:
            if part["name"] not in [n for n, _ in names]:
                names.append((part["name"], part.get("color") or OTHER_COLOR))
    x = left
    for name, color in names:
        parts.append(f'<rect x="{x:.1f}" y="14" width="12" height="12" rx="3" fill="{color}"/>')
        parts.append(_text(x + 18, 25, name, size=13, anchor="start"))
        x += 18 + len(name) * 7.4 + 22

    slot = plot_w / len(series)
    bar_w = min(slot * 0.62, 48)
    # Thin the axis labels so they never overlap: at most ~12 on the axis.
    every = max(len(series) // 12 + (1 if len(series) % 12 else 0), 1) if len(series) > 12 else 1
    for i, (point, total) in enumerate(zip(series, totals)):
        bx = left + slot * i + (slot - bar_w) / 2
        y = top + plot_h
        for part in point["parts"]:
            value = max(part["value"], 0)
            if not value:
                continue
            h_ = value / top_value * plot_h
            y -= h_
            parts.append(f'<rect x="{bx:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                         f'height="{max(h_, 0.5):.1f}" fill="{part.get("color") or OTHER_COLOR}"/>')
        if total and len(series) <= 14:
            parts.append(_text(bx + bar_w / 2, y - 7, point.get("display") or _fmt(total),
                               size=12, weight="600", role="value"))
        if i % every == 0:
            parts.append(_text(bx + bar_w / 2, top + plot_h + 22, point["label"], size=13))
    return "".join(parts)


def _grid(left, top, plot_w, plot_h, top_value, lines: int = 4) -> str:
    """Horizontal grid lines with value labels on the left axis."""
    parts = []
    for i in range(lines + 1):
        y = top + plot_h - (plot_h / lines) * i
        parts.append(f'<line class="c-grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w:.1f}" '
                     f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(left - 12, y + 5, _fmt(top_value / lines * i), size=13, anchor="end"))
    return "".join(parts)
