"""Mini evaluator for the arithmetic subset of Tableau calculations used by the workbook.

Supported: field references ``[name]``, numbers, ``SUM``, ``ZN``, unary minus, ``+ - * /`` and
parentheses. Anything else (other functions, string literals, IF, LOD expressions) raises
``EvalError`` so an unsupported calculation is reported instead of being silently skipped.

Evaluation happens over a group of rows, the way a view aggregates them. ``SUM`` adds a
field over the group; a bare field reference is allowed only on a single-row group. Division by
zero and arithmetic on null give null, and ``ZN`` turns null into zero, as in Tableau.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

Node = tuple
Row = Mapping[str, float | None]
Lookup = Callable[[str], "Node | None"]

_TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<field>\[[^\]]+\])|(?P<name>[A-Za-z_]\w*)|(?P<op>[-+*/(),]))"
)
FUNCTIONS = {"SUM", "ZN"}


class EvalError(ValueError):
    """The formula is outside the supported subset, malformed, or refers to nothing."""


def _tokenize(formula: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    text = formula.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise EvalError(f"unsupported syntax at {text[pos:].strip()[:20]!r} in {formula!r}")
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, formula: str):
        self.formula = formula
        self.tokens = _tokenize(formula)
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise EvalError(f"unexpected end of formula {self.formula!r}")
        self.pos += 1
        return token

    def expect(self, symbol: str) -> None:
        if self.take() != ("op", symbol):
            raise EvalError(f"expected {symbol!r} in {self.formula!r}")

    def parse(self) -> Node:
        node = self.expression()
        if self.peek() is not None:
            raise EvalError(f"unexpected {self.peek()[1]!r} in {self.formula!r}")
        return node

    def expression(self) -> Node:
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = ("bin", self.take()[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.factor()
        while self.peek() in (("op", "*"), ("op", "/")):
            node = ("bin", self.take()[1], node, self.factor())
        return node

    def factor(self) -> Node:
        kind, value = self.take()
        if kind == "number":
            return ("num", float(value))
        if kind == "field":
            return ("field", value[1:-1])
        if kind == "op" and value == "-":
            return ("neg", self.factor())
        if kind == "op" and value == "(":
            node = self.expression()
            self.expect(")")
            return node
        if kind == "name":
            if value.upper() not in FUNCTIONS:
                raise EvalError(f"unsupported function {value} in {self.formula!r}")
            self.expect("(")
            argument = self.expression()
            self.expect(")")
            return ("call", value.upper(), argument)
        raise EvalError(f"unexpected {value!r} in {self.formula!r}")


def parse(formula: str) -> Node:
    return _Parser(formula).parse()


def field_refs(node: Node) -> list[str]:
    """Every field name referenced in the tree, in order of appearance."""
    kind = node[0]
    if kind == "field":
        return [node[1]]
    if kind == "bin":
        return field_refs(node[2]) + field_refs(node[3])
    if kind in ("neg", "call"):
        return field_refs(node[-1])
    return []


def evaluate(node: Node, rows: Sequence[Row], lookup: Lookup, _row: Row | None = None):
    """Evaluate ``node`` over ``rows``. ``lookup(name)`` returns the tree of a calculated field
    or ``None`` when ``name`` is a plain column."""
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "field":
        name = node[1]
        calc = lookup(name)
        if calc is not None:
            return evaluate(calc, rows, lookup, _row)
        if _row is not None:
            return _row.get(name)
        if len(rows) != 1:
            raise EvalError(f"[{name}] used outside an aggregate on a group of {len(rows)} rows")
        return rows[0].get(name)
    if kind == "neg":
        value = evaluate(node[1], rows, lookup, _row)
        return None if value is None else -value
    if kind == "bin":
        left = evaluate(node[2], rows, lookup, _row)
        right = evaluate(node[3], rows, lookup, _row)
        if left is None or right is None:
            return None
        if node[1] == "+":
            return left + right
        if node[1] == "-":
            return left - right
        if node[1] == "*":
            return left * right
        return None if right == 0 else left / right
    if kind == "call":
        if node[1] == "SUM":
            if _row is not None:
                raise EvalError("SUM nested inside SUM")
            values = [evaluate(node[2], rows, lookup, row) for row in rows]
            values = [v for v in values if v is not None]
            return sum(values) if values else None
        value = evaluate(node[2], rows, lookup, _row)
        return 0.0 if value is None else value
    raise EvalError(f"unknown node {kind}")
