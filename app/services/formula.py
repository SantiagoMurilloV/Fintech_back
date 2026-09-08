"""Safe, deterministic formula evaluator for computed columns.

Formulas are written against row fields, e.g.:

    amount * 0.19                 -> VAT on the row amount
    usd_eq / 1000                 -> amount in thousands of USD
    round(amount * 0.03, 2)       -> gateway fee

Only arithmetic, comparisons and a small whitelist of functions are allowed;
there is no attribute access, no calls to arbitrary names and no imports, so
the same inputs always produce the same output and nothing can escape.
"""
from __future__ import annotations

import ast
import operator

# Binary and unary operators the evaluator accepts.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_COMPARE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}

# Callables available inside formulas.
_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "int": int,
    "float": float,
    "len": len,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
}

MAX_LENGTH = 500


class FormulaError(ValueError):
    pass


def validate(expression: str) -> None:
    """Raise FormulaError if the expression is not safe to evaluate."""
    if not expression or len(expression) > MAX_LENGTH:
        raise FormulaError(f"La fórmula debe tener entre 1 y {MAX_LENGTH} caracteres.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as err:
        raise FormulaError(f"Sintaxis inválida: {err.msg}")
    _check(tree.body)


def _check(node: ast.AST) -> None:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool)):
            raise FormulaError("Solo se permiten números, textos y booleanos.")
    elif isinstance(node, ast.Name):
        return  # resolved against the row at evaluation time
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise FormulaError("Operador no permitido.")
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise FormulaError("Operador unario no permitido.")
        _check(node.operand)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value)
    elif isinstance(node, ast.Compare):
        if any(type(op) not in _COMPARE_OPS for op in node.ops):
            raise FormulaError("Comparación no permitida.")
        _check(node.left)
        for comparator in node.comparators:
            _check(comparator)
    elif isinstance(node, ast.IfExp):
        _check(node.test)
        _check(node.body)
        _check(node.orelse)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            allowed = ", ".join(sorted(_FUNCTIONS))
            raise FormulaError(f"Solo se permiten estas funciones: {allowed}.")
        for arg in node.args:
            _check(arg)
    else:
        raise FormulaError("Expresión no permitida en una fórmula.")


def evaluate(expression: str, row: dict):
    """Evaluate `expression` against `row`; returns None when it cannot resolve."""
    try:
        tree = ast.parse(expression, mode="eval")
        return _eval(tree.body, row)
    except FormulaError:
        raise
    except Exception:
        # A row missing a field or dividing by zero yields an empty cell
        # rather than breaking the whole listing.
        return None


def _eval(node: ast.AST, row: dict):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return row.get(node.id)
    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](_eval(node.left, row), _eval(node.right, row))
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval(node.operand, row))
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, row) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, row)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, row)
            if not _COMPARE_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, row) if _eval(node.test, row) else _eval(node.orelse, row)
    if isinstance(node, ast.Call):
        return _FUNCTIONS[node.func.id](*[_eval(a, row) for a in node.args])
    raise FormulaError("Expresión no permitida.")
