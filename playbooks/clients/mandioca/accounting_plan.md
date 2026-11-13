# Plan de cuentas (Mandioca)
> El plan de cuentas oficial de Mandioca: cada ítem, su definición y en qué cuenta del panel está, o si todavía no está.

Fuente: «Accounting Plan» de Mandioca. Use estos números y nombres. Columna «Panel»: la
`account_key` en `accounts` / `account_values`; «—» = el panel aún no la registra por separado.

| Ítem | Concepto | Definición | Panel |
|---|---|---|---|
| 0 | Número de transacciones | | se cuenta en `orders` (OTC) |
| 0.1 | On ramp | # de operaciones on-ramp | `orders`: `extra->>'direccion' = 'BUY'` |
| 0.2 | Off ramp | # de operaciones off-ramp | `orders`: `extra->>'direccion' = 'SELL'` |
| 1 | Transactional Volume | Volumen On-Ramp (USD→USDT/USDC) y Off-Ramp (USDT/USDC→USD) | `operational_volume` |
| 1.1 | USD Received | Valor en USD recibidos | `usd_received` |
| 1.2 | USDT Sended | Valor en USDT enviados | `usdt_sent` |
| 1.3 | USDT Received | Valor en USDT recibidos | `usdt_received` |
| 1.4 | USD Sended | Valor en USD enviados | `usd_sent` |
| 2 | Revenue | Ingresos | `gross_profit` |
| 2.1 | On-Ramp (Buy) | USD recibidos − USDT enviados (supone 1 USD = 1 USDT) | `gross_profit_on_ramp` |
| 2.2 | Off-Ramp (Sell) | USDT recibidos − USD enviados (supone 1 USDT = 1 USD) | `gross_profit_off_ramp` |
| 3 | Transactional Costs | Costos por el uso de plataformas OnRamp/OffRamp | `operational_costs` |
| 3.1 | Mandioca Cost Buy | Costos por plataformas OnRamp | `mandioca_cost_buy` |
| 3.2 | Mandioca Cost Sell | Costos por plataformas OffRamp | `mandioca_cost_sell` |
| 4 | Transactional Profit | Revenue − Transactional Costs | `operational_profit` |
| 5 | Cost and expenses | | `total_costs_expenses` |
| 5.1 | Variable Costs | Costos de la operación asociados a las transacciones | `variable_costs` |
| 5.1.1 | Sales commissions | Fees pagados por operaciones con clientes vinculados por terceros | `sales_commissions` |
| 5.1.2 | Salarios | Salarios del equipo operativo | — (hoy dentro de `salaries`) |
| 5.1.3 | Merkle Science | Monitoreo continuo | — |
| 5.1.4 | Chainalysis | Screening y monitoreo continuo | — |
| 5.1.5 | Elliptic | Screening | — |
| 5.1.6 | Utila | Custodia de wallet | — |
| 5.1.7 | Compliances | Pago por los servicios de compliance | `compliance` (el panel la tiene en costos fijos) |
| 5.2 | Fixed Costs | | `fixed_costs` |
| 5.2.1 | Salaries | Salarios administrativos | `salaries` |
| 5.2.2 | Rent | Rentas de la red de oficinas | `rent` |
| 5.2.4 | Accounting | Servicios de accounting | `accounting` |
| 5.2.5 | Auxo | Mantenimiento y desarrollo de la plataforma transaccional | `auxo` |
| 5.2.6 | Asesoría tecnológica | Asesoría en desarrollo y mantenimiento del back office | `one_to_many` (One to Many) |
| 5.3 | Marketing | Eventos y merchandising (dotaciones) | `marketing` |
| 5.3.1 | Events | Participación en eventos, viajes y viáticos | `events` |
| 5.3.2 | Merchandizing | Adquisición de elementos de mercadeo | `merchandizing` |
| 5.4 | Other Costs & Expenses | Otros costos y gastos no clasificados arriba | `other_costs_expenses` |
| 5.4.1–5.4.4 | Viajes y viáticos · Gastos de representación · Bonos y regalos a colaboradores · Otros | | — (todo en `other_costs_expenses`) |
| 5 | Total Costs & Expenses | | `total_costs_expenses` |
| 6 | EBITDA | Earnings Before Interest, Taxes, Depreciation and Amortization | — (hoy = `net_result`, porque no hay 7–10) |
| 7–10 | Interest · Taxes · Depreciation · Amortization | | — |
| 11 | Net Profit | | — (el panel llama «Net Revenue» a `net_result`) |
| 11.1–11.3 | Partners distribution · Reserve · Capital | | — |

**Comisiones, gastos y costos de proveedor en Mandioca.**
- **Costos de proveedor** = ítem 3 (Transactional Costs). Por operación están en
  `extra->>'costo_proveedor'` de cada orden OTC, por `gateway` (Mesta, T-0, CODEX, Mansa…).
  En la hoja de cierre son las columnas por proveedor (Codex, Mesta Fee, T0, Mansa).
- **Comisiones** = solo 5.1.1 Sales commissions (`sales_commissions`). Columna «Comission».
- **Gastos** = el resto del ítem 5: Total Costs & Expenses − Sales commissions. Columna
  «Expenses».
- En `account_values` solo hay cuentas de entrada; los totales salen de las fórmulas
  (`get_income_statement`).

**Lo que falta en el panel.** Los ítems con «—» no tienen cifra propia: si preguntan por uno
(p. ej. «¿cuánto se pagó a Chainalysis?»), diga que el panel aún no lo registra por separado y en
qué cuenta agregada está, si se sabe. No hay EBITDA ni utilidad neta después de impuestos:
el resultado del panel es antes de intereses, impuestos, depreciación y amortización.
