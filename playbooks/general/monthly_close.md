# Cierre mensual
> Cómo responder «¿cómo cerró el mes?»: la fila de cierre con volúmenes, ingreso bruto, costos y resultado neto.

**Para qué sirve.** Finanzas y gerencia quieren la misma fila que llevan en su hoja de cierre:
cuánto entró y salió, cuánto se ganó en bruto, qué se fue en costos (proveedores, comisiones,
gastos) y con cuánto quedó el mes. No un resumen narrado: los números en columnas.

**Qué contiene (en este orden).**
1. **La fila de cierre**, una tabla de una fila con las columnas que el contexto del cliente
   indique (si el cliente tiene su propio playbook de cierre, sígalo). Por defecto:
   volumen por dirección o tipo, ingreso bruto, costo de proveedores (desglosado por proveedor),
   comisiones, gastos y resultado neto.
2. KPIs: volumen total, ingreso bruto, resultado neto y margen (resultado / volumen; en puntos
   básicos si el negocio trabaja con márgenes finos).
3. Comparación contra el mes anterior: variación del volumen, del ingreso bruto y del resultado.
4. Qué falta: si comisiones, gastos u otra columna no tienen datos para ese mes, dígalo en una
   nota y **no calcule el resultado neto como si fueran cero**: muestre el resultado hasta donde
   hay datos (p. ej. ingreso bruto − proveedores) y nómbrelo así.

**Cómo calcularlo.**
- Las operaciones del mes están en `orders` (`date LIKE 'AAAA-MM%'`). Qué campo es el ingreso
  bruto, el costo, la dirección o el proveedor lo dice el contexto del cliente.
- Comisiones y gastos: primero el estado de resultados del mes (`get_income_statement` o
  `account_values` con `period = 'AAAA-MM'`); si no hay, la tabla `expenses` del mes, diciendo
  de dónde salió.
- Saque la fila entera con **una** consulta: SUM con CASE por dirección y por proveedor, y el
  resultado neto calculado en el mismo SQL (con COALESCE solo si la columna existe ese mes).

**Cómo presentarlo.** Con `present`: la tabla de cierre primero (una columna por concepto,
unidades `usd`), luego los KPIs con la variación contra el mes anterior como nota y las notas de
lo que falta. Si el usuario no dice el mes, use el último mes con operaciones y dígalo.
