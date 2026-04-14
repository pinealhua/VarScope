from __future__ import annotations

import struct
from typing import Any

from varscope.core.typesystem import TypeInfo


class DecodeError(ValueError):
    pass


def decoder_kind_for_type(type_info: TypeInfo) -> str:
    leaf = type_info.unwrap()
    if leaf.kind == "enum":
        return "enum"
    if leaf.kind == "pointer":
        return "pointer"
    if leaf.kind != "base":
        return leaf.kind
    if leaf.boolean:
        return "bool"
    if leaf.floating:
        return f"float{leaf.byte_size * 8}"
    if leaf.signed:
        return f"int{leaf.byte_size * 8}"
    return f"uint{leaf.byte_size * 8}"


def decode_value(raw: bytes, type_info: TypeInfo) -> Any:
    leaf = type_info.unwrap()
    size = leaf.byte_size
    if len(raw) != size:
        raise DecodeError(f"Raw byte size {len(raw)} does not match expected type size {size}.")

    if leaf.kind == "pointer":
        value = int.from_bytes(raw, "little", signed=False)
        return f"0x{value:0{size * 2}X}"

    if leaf.kind == "enum":
        value = int.from_bytes(raw, "little", signed=leaf.signed)
        if value in leaf.enum_values:
            return f"{leaf.enum_values[value]} ({value})"
        return value

    if leaf.kind != "base":
        raise DecodeError(f"Unsupported decoded type kind: {leaf.describe()}")

    if leaf.boolean:
        return bool(int.from_bytes(raw, "little", signed=False))

    if leaf.floating:
        fmt = {4: "<f", 8: "<d"}.get(size)
        if fmt is None:
            raise DecodeError(f"Unsupported floating-point byte size: {size}.")
        return struct.unpack(fmt, raw)[0]

    return int.from_bytes(raw, "little", signed=leaf.signed)


def format_value(decoded: Any) -> str:
    if isinstance(decoded, float):
        return f"{decoded:.6f}"
    if isinstance(decoded, bool):
        return "true" if decoded else "false"
    return str(decoded)
