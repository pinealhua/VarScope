from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


TypeKind = Literal[
    "base",
    "enum",
    "pointer",
    "array",
    "struct",
    "union",
    "typedef",
    "const",
    "volatile",
    "restrict",
]


@dataclass(slots=True)
class StructField:
    name: str
    type_info: TypeInfo
    bit_offset: int = 0


@dataclass(slots=True)
class TypeInfo:
    kind: TypeKind
    name: str
    byte_size: int
    signed: bool = False
    floating: bool = False
    boolean: bool = False
    fields: list[StructField] = field(default_factory=list)
    element_type: TypeInfo | None = None
    array_length: int | None = None
    target_type: TypeInfo | None = None
    enum_values: dict[int, str] = field(default_factory=dict)

    def unwrap(self) -> TypeInfo:
        current = self
        while current.kind in {"typedef", "const", "volatile", "restrict"} and current.target_type is not None:
            current = current.target_type
        return current

    def describe(self) -> str:
        leaf = self.unwrap()
        if leaf.kind == "array" and leaf.element_type is not None:
            count = "?" if leaf.array_length is None else str(leaf.array_length)
            return f"{leaf.element_type.describe()}[{count}]"
        if leaf.kind == "pointer" and leaf.target_type is not None:
            return f"{leaf.target_type.describe()}*"
        return leaf.name

    def is_numeric(self) -> bool:
        leaf = self.unwrap()
        if leaf.kind == "enum":
            return True
        if leaf.kind != "base":
            return False
        return leaf.boolean or leaf.floating or leaf.byte_size > 0

    def is_scalar_like(self) -> bool:
        leaf = self.unwrap()
        return leaf.kind in {"base", "enum", "pointer"}

    def leaf_paths(
        self,
        root: str,
        *,
        max_array_items: int = 32,
        max_results: int = 4096,
    ) -> list[str]:
        out: list[str] = []

        def emit(path: str) -> None:
            if len(out) < max_results:
                out.append(path)

        def walk(node: TypeInfo, path: str) -> None:
            if len(out) >= max_results:
                return
            leaf = node.unwrap()

            if leaf.kind in {"struct", "union"}:
                if not leaf.fields:
                    emit(path)
                    return
                for field in leaf.fields:
                    walk(field.type_info, f"{path}.{field.name}")
                return

            if leaf.kind == "array" and leaf.element_type is not None:
                if leaf.array_length is None:
                    limit = min(max_array_items, 1)
                else:
                    limit = min(leaf.array_length, max_array_items)
                if limit <= 0:
                    emit(path)
                    return
                for i in range(limit):
                    walk(leaf.element_type, f"{path}[{i}]")
                return

            emit(path)

        walk(self, root)
        return out


def make_void_type() -> TypeInfo:
    return TypeInfo(kind="base", name="void", byte_size=1)
