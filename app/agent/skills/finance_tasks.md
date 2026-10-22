# Tareas recurrentes de finanzas: qué herramienta usar

| Pedido | Herramienta | Argumentos |
|---|---|---|
| «Cierre diario», «cómo cerró el día», «reporte del día» | `daily_close_report` | `date` AAAA-MM-DD |
| «Estado de resultados» de un mes o un rango («de enero a marzo») | `get_income_statement` | `period_from`, `period_to`, `view: "full"` |
| «Análisis vertical» | `get_income_statement` | `view: "vertical"` |
| «Análisis horizontal», «comparar el estado contra…» | `get_income_statement` | `view: "horizontal"`, `compare` («prev», «year» o un rango «AAAA-MM:AAAA-MM») |
| «Tablero / dashboard de operaciones» | `department_dashboard` | `kind: "operations"` |
| «Tablero / dashboard de finanzas» | `department_dashboard` | `kind: "finance"` |
| «Tablero / dashboard de gerencia / directivo / ejecutivo» | `department_dashboard` | `kind: "management"` |

- Una sola llamada a la herramienta basta: ya trae las tablas y los gráficos. No repita sus
  cifras con query_database.
- «Trimestre 1 de 2025» = 2025-01 a 2025-03; «el año 2025» = 2025-01 a 2025-12. Si no dice
  período, deje los meses vacíos: la herramienta usa el último con datos.
- Si piden «en PDF», «para enviar» o «guárdalo», pase `save_pdf: true`.
- Después del resultado, escriba como máximo TRES frases: el dato principal y la variación más
  notable. El usuario ya ve las tablas: no las repita. Porcentajes con uno o dos decimales.
- Si el resultado trae `meses_sin_gastos_cargados`, dígalo: el resultado neto de esos meses está
  sobreestimado porque faltan comisiones y gastos.
