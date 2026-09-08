"""Executive close summary — HTML always; PDF when weasyprint is available."""

from pathlib import Path

from close.models import CloseReport
from config.settings import COMPANY_NAME, OUTPUT_DIR

_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Cierre {period} — {account}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, sans-serif; margin: 40px; color: #1a1a2e; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
  th, td {{ border: 1px solid #d0d0e0; padding: 6px 10px; font-size: 13px; text-align: left; }}
  th {{ background: #f0f0f8; }}
  .ok {{ color: #0a7a3d; font-weight: 600; }} .fail {{ color: #b3261e; font-weight: 600; }}
  .big {{ font-size: 18px; font-weight: 700; }}
</style></head><body>
<h1>{company} — Cierre de mes {period}</h1>
<p>Cuenta: <b>{account}</b></p>

<h2>Totales</h2>
<table>
<tr><th>Total banco</th><th>Total libros</th><th>Diferencia</th><th>% conciliación</th></tr>
<tr><td>{bank_total:,.2f}</td><td>{books_total:,.2f}</td>
<td class="big {diff_class}">{difference:,.2f}</td><td>{match_rate:.0%}</td></tr>
</table>

<h2>Checklist de cierre</h2>
<table><tr><th>Validación</th><th>Estado</th><th>Detalle</th></tr>{check_rows}</table>

<h2>Partidas pendientes</h2>
<p>{n_bank} en banco sin contrapartida en libros · {n_books} en libros sin contrapartida en banco.
Ver detalle en el Excel de conciliación.</p>
</body></html>
"""


def write_summary(report: CloseReport) -> Path:
    check_rows = "".join(
        f"<tr><td>{c.name}</td>"
        f"<td class=\"{'ok' if c.passed else 'fail'}\">{'OK' if c.passed else 'FALLA'}</td>"
        f"<td>{c.detail}</td></tr>"
        for c in report.checks
    )
    html = _TEMPLATE.format(
        company=COMPANY_NAME,
        period=report.period,
        account=report.account,
        bank_total=report.bank_total,
        books_total=report.books_total,
        difference=report.difference,
        diff_class="ok" if abs(report.difference) < 1.0 else "fail",
        match_rate=report.match.match_rate,
        check_rows=check_rows,
        n_bank=len(report.match.unmatched_bank),
        n_books=len(report.match.unmatched_books),
    )

    html_path = OUTPUT_DIR / f"cierre_{report.account}_{report.period}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"📝 Resumen HTML: {html_path}")

    try:
        from weasyprint import HTML  # optional dependency

        pdf_path = html_path.with_suffix(".pdf")
        HTML(string=html).write_pdf(pdf_path)
        print(f"📄 Resumen PDF: {pdf_path}")
        return pdf_path
    except Exception:
        return html_path
