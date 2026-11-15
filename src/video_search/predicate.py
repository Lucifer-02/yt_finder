"""Compile metadata WHERE expressions without accepting arbitrary SQL statements."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Never, TypeAlias

SQLScalar: TypeAlias = str | int | float | None

_COLUMNS = {
    "video_id": "t.video_id",
    "title": "v.title",
    "description": "v.description",
    "channel": "c.title",
    "channel_id": "v.channel_id",
    "channel_title": "c.title",
    "channel_url": "c.url",
    "language": "t.language",
    "source": "t.source",
    "source_mode": "t.source_mode",
    "updated_at": "t.updated_at",
}

_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<string>'(?:''|[^'])*')"
    r"|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<operator><=|>=|<>|!=|[=<>])"
    r"|(?P<punctuation>[(),?+-])|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
)


@dataclass(frozen=True)
class CompiledPredicate:
    sql: str
    params: tuple[SQLScalar, ...]
    _storage_types: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Python equates 1 and 1.0, but SQLite's TEXT affinity compares their
        # bindings as '1' and '1.0'. Preserve that distinction in cache identity.
        object.__setattr__(
            self,
            "_storage_types",
            tuple(
                "integer"
                if isinstance(value, int)
                else "real"
                if isinstance(value, float)
                else "text"
                if isinstance(value, str)
                else "null"
                for value in self.params
            ),
        )


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


class _Parser:
    def __init__(self, expression: str, params: tuple[SQLScalar, ...]):
        self.tokens: list[_Token] = []
        position = 0
        while position < len(expression):
            match = _TOKEN.match(expression, position)
            if match is None:
                raise ValueError(f"Invalid WHERE expression at position {position}.")
            if match.lastgroup != "space":
                self.tokens.append(
                    _Token(str(match.lastgroup), match.group(), position)
                )
            position = match.end()
        self.tokens.append(_Token("end", "", len(expression)))
        self.index = 0
        self.supplied = params
        self.parameter_index = 0
        self.bound: list[SQLScalar] = []

    def _matches(self, value: str) -> bool:
        token = self.tokens[self.index]
        return token.kind in {"word", "punctuation", "operator"} and (
            token.value.upper() == value
        )

    def _accept(self, value: str) -> bool:
        if self._matches(value):
            self.index += 1
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._accept(value):
            self._error(f"expected {value}")

    def _error(self, reason: str) -> Never:
        raise ValueError(
            f"Invalid WHERE expression at position {self.tokens[self.index].position}: {reason}."
        )

    def parse(self) -> CompiledPredicate:
        sql = self._or()
        if self.tokens[self.index].kind != "end":
            self._error("unexpected token")
        if self.parameter_index != len(self.supplied):
            raise ValueError("Too many WHERE parameters.")
        return CompiledPredicate(sql, tuple(self.bound))

    def _or(self) -> str:
        left = self._and()
        while self._accept("OR"):
            left = f"({left} OR {self._and()})"
        return left

    def _and(self) -> str:
        left = self._not()
        while self._accept("AND"):
            left = f"({left} AND {self._not()})"
        return left

    def _not(self) -> str:
        if self._accept("NOT"):
            return f"(NOT {self._not()})"
        return self._comparison()

    def _comparison(self) -> str:
        left = self._value()
        token = self.tokens[self.index]
        if token.kind == "operator":
            self.index += 1
            return f"({left} {token.value} {self._value()})"
        if self._accept("IS"):
            operator = "IS NOT" if self._accept("NOT") else "IS"
            self._expect("NULL")
            return f"({left} {operator} NULL)"
        negated = self._accept("NOT")
        prefix = "NOT " if negated else ""
        if self._accept("IN"):
            self._expect("(")
            values: list[str] = []
            if not self._matches(")"):
                values.append(self._value())
                while self._accept(","):
                    values.append(self._value())
            self._expect(")")
            return f"({left} {prefix}IN ({','.join(values)}))"
        if self._accept("LIKE"):
            right = self._value()
            escape = f" ESCAPE {self._value()}" if self._accept("ESCAPE") else ""
            return f"({left} {prefix}LIKE {right}{escape})"
        if self._accept("BETWEEN"):
            lower = self._value()
            self._expect("AND")
            upper = self._value()
            return f"({left} {prefix}BETWEEN {lower} AND {upper})"
        if negated:
            self._error("expected IN, LIKE or BETWEEN after NOT")
        return left

    def _value(self) -> str:
        if self._accept("("):
            expression = self._or()
            self._expect(")")
            return f"({expression})"
        token = self.tokens[self.index]
        if token.kind == "string":
            self.index += 1
            return self._bind(token.value[1:-1].replace("''", "'"))
        sign = ""
        if self._matches("+") or self._matches("-"):
            sign = token.value
            self.index += 1
            token = self.tokens[self.index]
        if token.kind == "number":
            self.index += 1
            number = sign + token.value
            if any(character in number.lower() for character in (".", "e")):
                return self._bind(float(number))
            integer = int(number)
            if not -(2**63) <= integer < 2**63:
                raise ValueError(
                    "Integer WHERE literals must fit SQLite's signed int64 range."
                )
            return self._bind(integer)
        if sign:
            self._error("expected a number after its sign")
        if self._accept("?"):
            if self.parameter_index >= len(self.supplied):
                raise ValueError("Not enough WHERE parameters.")
            value = self.supplied[self.parameter_index]
            self.parameter_index += 1
            return self._bind(value)
        if self._accept("NULL"):
            return self._bind(None)
        if self._accept("TRUE"):
            return self._bind(1)
        if self._accept("FALSE"):
            return self._bind(0)
        if token.kind == "word" and (column := _COLUMNS.get(token.value.lower())):
            self.index += 1
            return column
        self._error("expected a metadata field, literal or parameter")

    def _bind(self, value: SQLScalar) -> str:
        self.bound.append(value)
        return "?"


def compile_where(where: str, params: tuple[SQLScalar, ...] = ()) -> CompiledPredicate:
    """Validate a metadata predicate and bind its literals and positional parameters.

    SQL table names, functions, comments and subqueries are outside this grammar.
    Logical fields use SQLite's ordinary comparison, NULL and LIKE semantics.
    """
    if not isinstance(where, str) or not where.strip():
        raise ValueError("WHERE expression must be a non-empty string.")
    if not isinstance(params, tuple):
        raise ValueError("WHERE parameters must be a tuple.")
    if any(
        value is not None and not isinstance(value, (str, int, float))
        for value in params
    ):
        raise ValueError("WHERE parameters must be strings, numbers or None.")
    if any(
        isinstance(value, int) and not -(2**63) <= value < 2**63 for value in params
    ):
        raise ValueError(
            "Integer WHERE parameters must fit SQLite's signed int64 range."
        )
    try:
        return _Parser(where, params).parse()
    except RecursionError as error:
        raise ValueError("WHERE expression nesting is too deep.") from error
