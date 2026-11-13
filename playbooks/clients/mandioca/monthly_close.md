# Cierre mensual (Mandioca)
> La fila de cierre de Mandioca: USD Sent, USDT Received, USD Received, USDT Sent, Gross Revenue, Expenses, Comission, fees por proveedor y Net Revenue.

**Cuándo.** «¿Cómo cerró el mes?», «cierre de octubre», «los valores del mes», «month end».
Mandioca lleva esta fila en su hoja de cierre; la respuesta debe traer **esas columnas, con esos
nombres y en ese orden**.

**Las columnas** (operaciones OTC del mes: `orders` con `extra->>'source' = 'otc'` y
`date LIKE 'AAAA-MM%'`):

| Columna | Cálculo |
|---|---|
| USD Sent | `SUM(amount)` de `extra->>'direccion' = 'SELL'` (off-ramp: Mandioca gira USD) |
| USDT Received | `SUM((extra->>'usdt')::numeric)` de SELL (el cliente envía USDT) |
| USD Received | `SUM(amount)` de BUY (on-ramp: el cliente gira USD) |
| USDT Sent | `SUM((extra->>'usdt')::numeric)` de BUY |
| Gross Revenue | `SUM((extra->>'spread_bruto')::numeric)` (todas) |
| Expenses | `SUM(amount)` de `account_values` del mes (`period = 'AAAA-MM'`) con `account_key IN ('salaries','rent','compliance','accounting','auxo','one_to_many','events','merchandizing','other_costs_expenses')` — en `account_values` solo hay cuentas de entrada; los totales (`fixed_costs`, `marketing`) no se guardan |
| Comission | `account_key = 'sales_commissions'` del mismo mes |
| Codex | `costo_proveedor` de `gateway = 'CODEX'` |
| Mesta Fee | `costo_proveedor` de `gateway IN ('Mesta', 'N-Mesta')` |
| T0 | `costo_proveedor` de `gateway = 'T-0'` |
| Mansa | `costo_proveedor` de `gateway = 'Mansa'` |
| Net Revenue | Gross Revenue − Expenses − Comission − todos los fees de proveedor |

- Un proveedor que no esté en la lista (p. ej. HiFi, Conduit, Reap) va en **su propia columna**
  después de Mansa, con su nombre: nunca sumado en silencio a otro.
- Los fees por proveedor sumados deben dar `SUM(costo_proveedor)` del mes; si no, dígalo.
- Saque la fila con una sola consulta (SUM con CASE por dirección y gateway, más un subquery a
  `account_values` para Expenses y Comission) y calcule Net Revenue en el mismo SQL.

**Si faltan Expenses o Comission** (no hay `account_values` de ese mes): muéstrelas como «sin
dato», no como 0, y en vez de Net Revenue muestre **«Resultado antes de gastos y comisiones»** =
Gross Revenue − fees de proveedor, con una nota que diga qué falta. Los gastos sueltos de la
tabla `expenses` no son el total del mes; puede mencionarlos aparte, diciendo que son parciales.

**Presentación.** `present` con:
1. Tabla «Cierre de <mes> <año>» con las columnas de arriba (unidades `usd`).
2. KPIs: operaciones, volumen (USD Sent + USD Received), Gross Revenue y Net Revenue (o el
   resultado antes de gastos), con la variación contra el mes anterior como nota.
3. Notas: lo que falta y, si aplica, que los fees no cuadran con el costo total.
