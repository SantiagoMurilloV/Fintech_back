# Tableros por área: operaciones, finanzas y gerencia
> Qué necesita ver cada área en su tablero, cómo calcular cada indicador y cómo armarlo.

Un tablero responde las preguntas de **una audiencia** en una pantalla. Compare siempre contra
el **período anterior de igual duración** (un mes contra el anterior; un trimestre contra el
anterior) y diga contra qué compara. Si no dan período, use el último mes con operaciones
(`SELECT MAX(date) FROM orders`). Los campos concretos de cada indicador (utilidad, costo,
proveedor, tipo de operación) los dice el contexto del cliente.

## Operaciones — ¿cómo está funcionando la operación?
- KPIs: operaciones, volumen, ticket promedio (volumen / operaciones), clientes activos
  (`COUNT(DISTINCT customer)`) y la división por tipo de operación si existe.
- Proveedores o canales (`gateway`): volumen, costo y costo por unidad de volumen — es lo que
  operaciones puede negociar o mover.
- Volumen por día (un mes) o por mes (un rango): gráfico de barras.
- Otras divisiones que el contexto del cliente marque como importantes (activo, país, red…).
- Clientes con más operaciones.

## Finanzas — ¿cuánto ganamos y dónde se va el dinero?
- Del estado de resultados (`get_income_statement`, rango del tablero, `compare: "prev"`): las
  líneas principales del plan (utilidad bruta, operacional, costos y gastos, resultado neto) y
  los márgenes (operacional / bruta, neto / bruta).
- Composición de los costos según los grupos del plan de cuentas.
- Resultado neto por mes si el rango tiene varios meses.
- Un control cruzado, si el contexto del cliente lo define (p. ej. la utilidad de las
  operaciones contra la utilidad del estado de resultados).
- Avise si faltan gastos cargados (`meses_sin_gastos_cargados`).

## Gerencia — ¿vamos bien y qué riesgo hay?
- Pocos números, los más importantes: volumen, utilidad, resultado neto y margen, cada uno con
  su variación.
- Acumulado del año hasta el último mes.
- Tendencia de los últimos 12 meses de la utilidad (gráfico).
- Concentración: % del volumen de los 5 principales clientes (alerta si pasa de 60 %).
- Alertas: costo por unidad subiendo, utilidad cayendo más de 10 %, gastos sin cargar. Solo las
  que apliquen.

## Cómo armarlo
1. Saque las cifras con pocas consultas (agregados en un SELECT, CASE para comparar períodos).
2. Si el tablero incluye el estado de resultados, use `get_income_statement`.
3. Arme el tablero con `present`: KPIs arriba, luego gráficos, luego tablas, y notas o alertas.
   Las cifras que pase deben venir tal cual de los resultados de las consultas.
