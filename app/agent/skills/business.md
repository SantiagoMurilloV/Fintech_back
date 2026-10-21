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
- Precio **actual** de una moneda: la lectura más reciente de 'binance', 'kraken' o
  'coinbase' (tres veces al día). «Según TradingView» → `source = 'tradingview'`.
- Precio en una **fecha pasada**: `source = 'binance_1d'` y `rate_date = 'YYYY-MM-DD'`
  (cierre diario en UTC, desde que la moneda se listó). Use el par contra USDT
  (`'SOL/USDT'`); USDT ≈ USD. Si no hay ese día, dígalo en vez de usar otro sin avisar.
- Comparaciones («SOL hoy vs. hace un año», «cuánto subió BTC en 2025») se calculan en un
  solo SQL con las dos lecturas.
- Diga siempre de qué fuente y de qué fecha u hora es cada precio.
- El dólar en Colombia es la TRM: `pair = 'USD/COP'`, `source = 'trm'`. Otras monedas
  fiat: `source = 'ecb'`, `pair = 'USD/BRL'`, 'USD/MXN', 'USD/EUR'…

**Estado de resultados**: las cifras mensuales están en `account_values` (por `account_key` y
`period`), con el significado de cada cuenta en `accounts`. Los totales son recetas
(`accounts.formula`): para un total, prefiera la herramienta del estado de resultados.

**Gastos** (tabla `expenses`): `amount` está en `currency`; no sume monedas distintas sin
decirlo.
