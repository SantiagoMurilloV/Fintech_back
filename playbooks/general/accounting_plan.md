# Plan de cuentas
> Cómo leer el plan de cuentas de una empresa: qué es cada capa del estado de resultados y cómo no confundir costos de proveedor, comisiones y gastos.

**Cuándo.** Cualquier pregunta sobre una cuenta («¿qué es Auxo?», «¿dónde va este pago?»),
sobre gastos o comisiones, o antes de armar un cierre o un estado de resultados que mezcle
operaciones y contabilidad.

**Dónde está el plan.** En la tabla `accounts` (`key`, `name`, `definition`, `formula`,
`nature` income | cost | result, `kind` input | total). Las cifras mensuales están en
`account_values` (`account_key`, `period` AAAA-MM, `amount`) y **solo para las cuentas de
entrada** (`kind = 'input'`); los totales salen de su `formula`. Si el cliente tiene su propio
playbook `accounting_plan`, ese manda: numeración, nombres y definiciones oficiales.

**Las capas de un estado de resultados** (de arriba abajo; cada empresa las nombra a su modo):
1. **Actividad**: número de operaciones y volumen. No es ingreso: es lo que se movió.
2. **Ingreso** (revenue / utilidad bruta): lo que la empresa gana en cada operación.
3. **Costos transaccionales**: lo que cobran los proveedores o plataformas por ejecutar cada
   operación. Van pegados a la operación.
4. **Utilidad transaccional** = ingreso − costos transaccionales.
5. **Costos y gastos**: variables (crecen con la actividad: comisiones comerciales,
   monitoreo, custodia…), fijos (salarios, arriendo, contabilidad, tecnología), mercadeo y
   otros.
6. **EBITDA**, luego intereses, impuestos, depreciación y amortización → **utilidad neta**, y
   su destino (distribución a socios, reserva, capital).

**Tres cosas que no se mezclan.**
- **Costo de proveedor** (capa 3): el fee de la plataforma que ejecuta la operación.
- **Comisión** (capa 5, variable): lo que se paga a un tercero que trajo al cliente. No es un
  costo de proveedor aunque también sea por operación.
- **Gasto** (capa 5, el resto): salarios, arriendo, servicios, mercadeo, viajes…

Cuando alguien diga «gastos» sin más, use la definición del cliente (su playbook o su hoja de
cierre); si no la hay, diga qué incluyó. Muestre siempre comisiones y gastos por separado.

**Reglas.**
- Use el nombre y la numeración del plan del cliente al nombrar una cuenta.
- Si el plan oficial tiene una cuenta que el panel todavía no registra, dígalo: no la deduzca
  de otra ni la dé como cero.
- Un gasto suelto de la tabla `expenses` se ubica en el plan por su categoría; si la categoría
  no encaja en ninguna cuenta, dígalo en vez de adivinar.
