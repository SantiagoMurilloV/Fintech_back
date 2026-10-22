# Mandioca: contexto del cliente
> Qué hace Mandioca, qué significa cada término de su negocio y dónde está cada dato.

**El negocio.** Rampas entre dólares y stablecoins para empresas: el cliente envía USDT/USDC y
Mandioca gira USD a su proveedor (**off-ramp**, ~95 % del volumen), o el cliente gira USD y
recibe USDT/USDC (**on-ramp**). Hay además una mesa de reales (BRL).

**Dónde está cada dato** (para los playbooks generales):
| En los playbooks | En Mandioca |
|---|---|
| Operaciones | `orders` con `extra->>'source' = 'otc'` (libro OTC 2024–2025) |
| Volumen | `amount` (el lado en USD) |
| Utilidad | `(extra->>'utilidad')::float` — utilidad operativa = spread bruto − costo de proveedor |
| Costo | `(extra->>'costo_proveedor')::float` — lo que cobró el proveedor |
| Spread / ingreso bruto | `(extra->>'spread_bruto')::float` |
| Tipo de operación | `extra->>'tipo'`: 'Off-ramp' o 'On-ramp' (`extra->>'direccion'`: SELL / BUY) |
| Proveedor | `gateway`: Conduit, Reap, Mesta, One Trade, Stillman |
| Otras divisiones | `extra->>'activo'` (USDT, USDC…) |
| Margen | en puntos básicos: utilidad / volumen × 10.000 (márgenes de ~30 pb) |
| Control cruzado (finanzas) | utilidad OTC del período vs. utilidad operacional del estado de resultados |

**Mercado que importa** (tabla `rate_snapshots`):
- USDT/USD del día: lectura del día o cierre diario `source = 'coinbase_1d'`. «Según
  TradingView» → `source = 'tradingview'`. Actual: la lectura más reciente de 'tradingview',
  'coinbase' o 'kraken'.
- Otras monedas cripto: actual en 'binance' / 'kraken'; en una fecha pasada, `source =
  'binance_1d'` con el par contra USDT ('SOL/USDT'), `rate_date = 'AAAA-MM-DD'`.
- TRM (USD/COP): `source = 'trm'`, `rate_date` el día. Otras fiat: `source = 'ecb'`.
- Diga siempre de qué fuente y de qué fecha u hora es cada precio. Si no hay lectura de ese día,
  dígalo en vez de usar otra fecha sin avisar.

**Estado de resultados.** El plan de cuentas de Mandioca (34 cuentas) está en `accounts`; las
cifras mensuales en `account_values`. Faltan comisiones y gastos de ene–abr 2024 y feb–dic 2025:
el resultado neto de esos meses está sobreestimado.

**Gastos** (`expenses`): `amount` está en `currency`; no sume monedas distintas sin decirlo.
