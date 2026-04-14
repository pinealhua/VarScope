from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from varscope.core.typesystem import StructField, TypeInfo


class ExpressionError(ValueError):
    pass


class VariableLike(Protocol):
    name: str
    address: int
    type_info: TypeInfo


@dataclass(slots=True)
class ParsedExpression:
    root: str
    steps: list[str | int]


@dataclass(slots=True)
class ResolvedExpression:
    address: int
    byte_size: int
    type_info: TypeInfo


_ROOT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*")
_STEP_RE = re.compile(r"(?:\.([A-Za-z_][A-Za-z0-9_]*))|(?:\[(\d+)\])")


def parse_expression(expression: str) -> ParsedExpression:
    text = expression.strip()
    if not text:
        raise ExpressionError("Expression is empty.")

    root_match = _ROOT_RE.match(text)
    if root_match is None:
        raise ExpressionError(f"Invalid expression root: {expression!r}")

    root = root_match.group(0)
    pos = root_match.end()
    steps: list[str | int] = []

    while pos < len(text):
        step_match = _STEP_RE.match(text, pos)
        if step_match is None:
            raise ExpressionError(f"Invalid token near: {text[pos:]!r}")
        field_name = step_match.group(1)
        index_text = step_match.group(2)
        if field_name is not None:
            steps.append(field_name)
        else:
            steps.append(int(index_text))
        pos = step_match.end()

    return ParsedExpression(root=root, steps=steps)


def resolve_expression(expression: str, symbol_db) -> ResolvedExpression:
    parsed = parse_expression(expression)
    variable = _lookup_variable(symbol_db, parsed.root)
    if variable is None:
        raise ExpressionError(f"Variable not found: {parsed.root}")

    address = int(variable.address)
    current_type = variable.type_info

    for step in parsed.steps:
        if isinstance(step, str):
            current_type, field = _resolve_field(current_type, step)
            if field.bit_offset % 8 != 0:
                raise ExpressionError("Bitfield-only members are not supported.")
            address += field.bit_offset // 8
        else:
            current_type = _resolve_index(current_type, step)
            element = current_type.unwrap()
            address += step * element.byte_size

    leaf = current_type.unwrap()
    return ResolvedExpression(address=address, byte_size=leaf.byte_size, type_info=current_type)


def _lookup_variable(symbol_db, name: str):
    if hasattr(symbol_db, "lookup_variable"):
        return symbol_db.lookup_variable(name)
    if hasattr(symbol_db, "resolve_variable"):
        return symbol_db.resolve_variable(name)

    variables = None
    if isinstance(symbol_db, dict):
        variables = symbol_db
    elif hasattr(symbol_db, "variables"):
        variables = getattr(symbol_db, "variables")

    if isinstance(variables, dict):
        return variables.get(name)

    return None


def _resolve_field(type_info: TypeInfo, field_name: str) -> tuple[TypeInfo, StructField]:
    leaf = type_info.unwrap()
    if leaf.kind not in {"struct", "union"}:
        raise ExpressionError(f"Type {leaf.describe()} does not support member access.")

    for field in leaf.fields:
        if field.name == field_name:
            return field.type_info, field

    raise ExpressionError(f"Member '{field_name}' not found in {leaf.describe()}.")


def _resolve_index(type_info: TypeInfo, index: int) -> TypeInfo:
    leaf = type_info.unwrap()
    if leaf.kind != "array" or leaf.element_type is None:
        raise ExpressionError(f"Type {leaf.describe()} does not support index access.")
    if index < 0:
        raise ExpressionError("Array index must be non-negative.")
    if leaf.array_length is not None and index >= leaf.array_length:
        raise ExpressionError(f"Array index {index} out of range (len={leaf.array_length}).")
    return leaf.element_type
