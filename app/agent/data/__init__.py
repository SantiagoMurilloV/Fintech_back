"""The Data layer: where the agent's facts come from.

The model never sees the database directly. It reads `catalog.describe()`
(which tables exist and what each column means) and asks `query.run(sql)` for
what it needs; the server decides what may be read and how much comes back.
"""
