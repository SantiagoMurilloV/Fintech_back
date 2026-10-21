# Mandioca: how the business reads

**Operaciones OTC** (tabla `orders`, `extra->>'source' = 'otc'`): cada fila es una operación
ejecutada del libro OTC 2024–2025.
- `SELL` = **Off-ramp**: el cliente envía USDT/USDC y Mandioca gira USD. Es ~95 % del volumen.
- `BUY` = **On-ramp**: el cliente gira USD y recibe USDT/USDC.
- **Volumen** = suma de `amount` (el lado en USD).
- **Spread bruto** = `extra->>'spread_bruto'`: lo que se cobra al cliente.
- **Costo de proveedor** = `extra->>'costo_proveedor'`: lo que cobró Conduit, Reap, Mesta…
- **Utilidad operativa** = `extra->>'utilidad'` = spread bruto − costo de proveedor.
- El proveedor está en `gateway`; el cliente en `customer` (en mayúsculas).

**Precios** (tabla `rate_snapshots`):
- «según TradingView» → `source = 'tradingview'`; si no dice fuente, prefiera
  'tradingview', luego 'coinbase', luego 'kraken'.
- «hoy» o «ahora» = la lectura más reciente (`ORDER BY taken_at DESC LIMIT 1`). Diga la hora
  de la lectura (UTC).
- Un precio en una fecha pasada: la lectura más cercana a esa fecha; si no hay lecturas de
  ese día, dígalo en vez de usar otra fecha sin avisar.
- El dólar en Colombia es la TRM: `pair = 'USD/COP'`, `source = 'trm'`.

**Estado de resultados**: las cifras mensuales están en `account_values` (por `account_key` y
`period`), con el significado de cada cuenta en `accounts`. Los totales son recetas
(`accounts.formula`): para un total, prefiera la herramienta del estado de resultados.

**Gastos** (tabla `expenses`): `amount` está en `currency`; no sume monedas distintas sin
decirlo.
