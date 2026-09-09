"""Tool modules.

Importing this package registers every tool in the registry; the graph then
resolves them by name.
"""
from . import (  # noqa: F401
    advisor, analytics, documents, external, ranking, records, reporting, schema,
)
