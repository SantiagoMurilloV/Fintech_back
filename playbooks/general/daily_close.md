# Cierre diario
> Cómo preparar el reporte de cierre de un día: qué contiene, cómo calcular cada cifra y cómo presentarlo.

**Para qué sirve.** Que operaciones y finanzas vean en un minuto cómo cerró un día: cuánto se
movió, cuánto se ganó, por qué canales o proveedores, con qué clientes y si algo se salió de lo
normal.

**Qué contiene (en este orden).**
1. Resumen del día: número de operaciones, volumen, utilidad y margen (utilidad / volumen; en
   puntos básicos si el negocio trabaja con márgenes finos).
2. Comparación contra el **día anterior con operaciones** (no necesariamente ayer: fines de
   semana y festivos pueden no operar). Variación de operaciones, volumen y utilidad.
3. La división del volumen por tipo de operación, si el negocio tiene más de uno.
4. Por proveedor, pasarela o canal: operaciones, volumen, costo y costo por unidad de volumen.
5. Los clientes con más volumen del día (top 5) y su utilidad.
6. Acumulado del mes hasta ese día: operaciones, volumen y utilidad.
7. Mercado del día, si el contexto del cliente indica precios o tasas que importan, y los gastos
   registrados ese día.

**Cómo calcularlo.**
- Las operaciones del día están en `orders` (`date = 'AAAA-MM-DD'`). **Qué campo es la utilidad,
  el costo, el tipo de operación o el proveedor lo dice el contexto del cliente.** Si el
  contexto no lo dice, use `amount` como volumen y diga qué no pudo calcular.
- Día anterior con operaciones: `SELECT MAX(date) FROM orders WHERE date < :dia`.
- Pida las cifras con pocas consultas: un SELECT con varios agregados (y CASE para separar
  tipos o períodos) rinde más que muchos. Las variaciones y porcentajes, calculados en el SQL.

**Cómo presentarlo.** Con `present`: KPIs del día con su variación como nota, una tabla por
proveedor o canal, una de clientes y, si aplica, una línea de mercado. Si no hubo operaciones ese
día, dígalo y nombre el último día que sí tuvo.
