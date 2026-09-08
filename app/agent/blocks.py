"""Structured response blocks.

The agent never answers with markdown: it returns a list of typed blocks that
the web chat renders as real UI (tables, KPI tiles, charts, file cards). Each
builder is a pure function, so the same tool output always yields the same
block.

Block contract (also consumed by fintech_front/src/components/blocks):
  {type: "text",   content}
  {type: "kpis",   items: [{label, value, delta, positive}]}
  {type: "table",  columns: [{key, label, align}], rows: [{...}], caption}
  {type: "chart",  svg, title, chart_type}
  {type: "file",   name, url, kind, description}
  {type: "list",   items: [{title, detail, tone}], title}
  {type: "notice", tone: "info"|"success"|"warning"|"danger", content}
  {type: "heading", content, detail}   — section header inside a long answer
"""
from __future__ import annotations


def text(content: str) -> dict:
    return {"type": "text", "content": content}


def kpis(items: list[dict]) -> dict:
    return {"type": "kpis", "items": items}


def table(columns: list[dict], rows: list[dict], caption: str | None = None,
          wide: bool = False) -> dict:
    """`wide` asks the layout for the full row — for tables with many columns."""
    return {"type": "table", "columns": columns, "rows": rows, "caption": caption,
            "wide": wide}


def chart(svg: str, title: str = "", chart_type: str = "bar",
          series: list | None = None) -> dict:
    """`series` duplicates the numbers behind the SVG so a PDF can redraw them."""
    return {"type": "chart", "svg": svg, "title": title, "chart_type": chart_type,
            "series": series or []}


def file(name: str, url: str, kind: str = "pdf", description: str = "") -> dict:
    return {"type": "file", "name": name, "url": url, "kind": kind, "description": description}


def listing(items: list[dict], title: str = "") -> dict:
    return {"type": "list", "items": items, "title": title}


def notice(content: str, tone: str = "info") -> dict:
    return {"type": "notice", "tone": tone, "content": content}


def heading(content: str, detail: str = "") -> dict:
    """Section title: organises a long analysis into named parts."""
    return {"type": "heading", "content": content, "detail": detail}


def plain_text(blocks: list[dict]) -> str:
    """Flatten blocks to a plain-text transcript.

    Stored alongside the blocks so conversation history stays readable and can
    be fed back to the LLM as context.
    """
    parts: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind in ("text", "notice"):
            parts.append(block["content"])
        elif kind == "heading":
            parts.append(f"== {block['content']} ==")
        elif kind == "kpis":
            parts.append(" · ".join(f"{i['label']}: {i['value']}" for i in block["items"]))
        elif kind == "table":
            labels = [c["label"] for c in block["columns"]]
            parts.append(f"[tabla: {', '.join(labels)} — {len(block['rows'])} filas]")
        elif kind == "chart":
            parts.append(f"[gráfico: {block.get('title') or block['chart_type']}]")
        elif kind == "file":
            parts.append(f"[archivo: {block['name']} — {block['url']}]")
        elif kind == "list":
            parts.append("\n".join(f"• {i['title']}" + (f" — {i['detail']}" if i.get("detail") else "")
                                   for i in block["items"]))
    return "\n\n".join(p for p in parts if p)
