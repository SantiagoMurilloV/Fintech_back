# Estado de resultados y análisis vertical y horizontal
> Cómo preparar el estado de resultados de un mes o un rango y sus análisis vertical y horizontal.

**La herramienta.** `get_income_statement` aplica el plan de cuentas de la empresa (sus
fórmulas) sobre cualquier rango de meses: suma los insumos del rango y recalcula totales y
razones — no los promedia. Úsela siempre para el estado de resultados; no lo reconstruya con
SQL.

**Estado de resultados (período desde–hasta).** `period_from` y `period_to` en AAAA-MM
(«primer trimestre 2025» = 2025-01 a 2025-03). Presente las líneas principales (ingresos /
utilidad bruta, costos operacionales, utilidad operacional, costos y gastos, resultado neto) y
lo más notable.

**Análisis vertical.** `view: "vertical"`. Cada cuenta como % de su cuenta base (la columna
«Sobre»): p. ej. la utilidad operacional como % de la utilidad bruta, cada costo como % del
total de costos. Sirve para ver la **estructura**: qué pesa más. Comente las dos o tres
proporciones que más dicen del negocio (margen operacional, peso de costos de proveedor).

**Análisis horizontal.** `view: "horizontal"` con `compare`: «prev» (período anterior de igual
duración), «year» (mismos meses del año anterior) o un rango «AAAA-MM:AAAA-MM». Muestra la
variación absoluta y en % de cada cuenta. Sirve para ver la **tendencia**: qué creció o cayó y
por qué. Señale las dos o tres variaciones más grandes y si van en la dirección correcta
(un costo que sube es malo; un ingreso que sube es bueno).

**Ojo con los datos.** Si el resultado trae `meses_sin_gastos_cargados`, dígalo siempre: el
resultado neto de esos meses está sobreestimado porque faltan comisiones y gastos.
