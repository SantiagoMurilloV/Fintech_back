"""Deterministic SVG chart rendering.

Charts are drawn from raw series with plain string building: the same input
always produces byte-identical output, there is no plotting dependency, and
the result embeds directly in the web chat and in saved reports.

Colours are theme-neutral (they use the brand palette explicitly) because the
SVG is also embedded in PDFs, which have no CSS variables.
"""
from __future__ import annotations

from html import escape

# Brand palette (mirrors the frontend accent scale).
SERIES_COLORS = ["#12b877", "#4d8dff", "#dfb15e", "#e88a80", "#9b8cff", "#4bc6c6"]
GRID = "#d6e3dc"
AXIS_TEXT = "#5a6b62"
TITLE_TEXT = "#122019"
MUTED_FILL = "#c3d3ca"

CHART_TYPES = ["bar", "line", "pie", "hbar", "box"]


def _fmt(value: float) -> str:
    """Compact number label: 12500 -> '12,5 k'."""
    if abs(value) >= 1000:
        return f"{value / 1000:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".") + " k"
    return f"{value:,.1f}".replace(",", "·").replace(".", ",").replace("·", ".")


def _text(x, y, content, size=11, fill=AXIS_TEXT, anchor="middle", weight="400"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="IBM Plex Sans, sans-serif" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(str(content))}</text>')


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

    if chart_type == "bar":
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
        f'width="100%" role="img" aria-label="{escape(title or chart_type)}">'
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
        color = MUTED_FILL if point.get("muted") else SERIES_COLORS[0]
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                     f'rx="4" fill="{color}"/>')
        parts.append(_text(x + bar_w / 2, y - 8, _fmt(value), size=14, weight="600"))
        parts.append(_text(x + bar_w / 2, top + plot_h + 22, point["label"], size=14))
    return "".join(parts)


def _hbars(series: list[dict], width: int, height: int) -> str:
    """Horizontal bars — better for long category labels."""
    left, top, plot_w, plot_h = _plot_area(width, height, left=195, right=88, top=20, bottom=16)
    top_value = max((s["value"] for s in series), default=0) or 1
    parts = []
    slot = plot_h / len(series)
    bar_h = min(slot * 0.6, 26)
    for i, point in enumerate(series):
        value = max(point["value"], 0)
        bar_w = (value / top_value) * plot_w
        y = top + slot * i + (slot - bar_h) / 2
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        parts.append(f'<rect x="{left}" y="{y:.1f}" width="{max(bar_w, 2):.1f}" height="{bar_h:.1f}" '
                     f'rx="4" fill="{color}"/>')
        parts.append(_text(left - 12, y + bar_h / 2 + 5, point["label"], size=15, anchor="end"))
        parts.append(_text(left + bar_w + 10, y + bar_h / 2 + 5, _fmt(value), size=14, anchor="start", weight="600"))
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
        parts.append(_text(x, y - 12, _fmt(point["value"]), size=13, weight="600"))
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
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        parts.append(f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                     f'A{radius:.1f},{radius:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>')

    # Legend on the right side.
    legend_x = width * 0.58
    start_y = max(height * 0.5 - len(series) * 14, 26)
    for i, point in enumerate(series):
        y = start_y + i * 28
        pct = max(point["value"], 0) / total * 100
        parts.append(f'<rect x="{legend_x}" y="{y - 11}" width="14" height="14" rx="4" '
                     f'fill="{SERIES_COLORS[i % len(SERIES_COLORS)]}"/>')
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
        parts.append(f'<line x1="{gx:.1f}" y1="{top}" x2="{gx:.1f}" '
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
                           size=13, weight="600"))
        parts.append(_text(x_max, cy - box_h * 0.3 - 8, _fmt(point["max"]), size=12))
    return "".join(parts)


def _grid(left, top, plot_w, plot_h, top_value, lines: int = 4) -> str:
    """Horizontal grid lines with value labels on the left axis."""
    parts = []
    for i in range(lines + 1):
        y = top + plot_h - (plot_h / lines) * i
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w:.1f}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(_text(left - 12, y + 5, _fmt(top_value / lines * i), size=13, anchor="end"))
    return "".join(parts)
