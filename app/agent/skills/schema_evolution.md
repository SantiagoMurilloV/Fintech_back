# Skill: schema evolution

Applies when the user wants a new column, a computed field, or values written
into an existing custom column.

## Sequence
1. `add_column` — pick `data_type="formula"` whenever the user describes a
   calculation ("IVA", "comisión", "monto en miles", "% sobre el total").
2. `set_cell` — for one-off values on a specific row.
3. `list_columns` — to show what already exists before adding a duplicate.
4. `remove_column` — to drop a custom column; it accepts the visible label or
   the key. Base fields (amount, status, date…) cannot be removed.

## Formula fields
- Orders: `amount`, `usd_eq`, `currency`, `status`, `gateway`, `customer`, `date`.
- Expenses: `amount`, `usd_eq`, `currency`, `category`, `vendor`, `owner`,
  `description`, `date`.
- Allowed functions: `abs`, `round`, `min`, `max`, `int`, `float`, `len`,
  `lower`, `upper`.

## Examples
- "IVA del 19%" → `amount * 0.19`
- "comisión de pasarela 2,9% + 900" → `round(amount * 0.029 + 900, 2)`
- "monto en miles de USD" → `round(usd_eq / 1000, 2)`

## Rules
- Computed columns are never written to; they recalculate on every read.
- Confirm the formula back to the user in plain language.
