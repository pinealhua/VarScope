from __future__ import annotations

import struct
from typing import Any, Callable

from varscope.core.typesystem import TypeInfo


class DecodeError(ValueError):
    pass


PayloadDecoder = Callable[[bytes | memoryview, int], Any]


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


def compile_payload_decoder(type_info: TypeInfo) -> PayloadDecoder:
    leaf = type_info.unwrap()
    size = leaf.byte_size

    if leaf.kind == "pointer":
        width = size * 2

        def decode_pointer(payload: bytes | memoryview, offset: int) -> Any:
            value = int.from_bytes(payload[offset : offset + size], "little", signed=False)
            return f"0x{value:0{width}X}"

        return decode_pointer

    if leaf.kind == "enum":
        enum_values = dict(leaf.enum_values)
        signed = leaf.signed

        def decode_enum(payload: bytes | memoryview, offset: int) -> Any:
            value = int.from_bytes(payload[offset : offset + size], "little", signed=signed)
            if value in enum_values:
                return f"{enum_values[value]} ({value})"
            return value

        return decode_enum

    if leaf.kind != "base":
        raise DecodeError(f"Unsupported decoded type kind: {leaf.describe()}")

    if leaf.boolean:
        def decode_bool(payload: bytes | memoryview, offset: int) -> Any:
            return bool(int.from_bytes(payload[offset : offset + size], "little", signed=False))

        return decode_bool

    if leaf.floating:
        fmt = {4: "<f", 8: "<d"}.get(size)
        if fmt is None:
            raise DecodeError(f"Unsupported floating-point byte size: {size}.")
        unpacker = struct.Struct(fmt)
        return lambda payload, offset: unpacker.unpack_from(payload, offset)[0]

    fmt = _integer_struct_format(size=size, signed=leaf.signed)
    if fmt is not None:
        unpacker = struct.Struct(fmt)
        return lambda payload, offset: unpacker.unpack_from(payload, offset)[0]

    def decode_int(payload: bytes | memoryview, offset: int) -> Any:
        return int.from_bytes(payload[offset : offset + size], "little", signed=leaf.signed)

    return decode_int


def format_value(decoded: Any) -> str:
    if isinstance(decoded, float):
        return f"{decoded:.6f}"
    if isinstance(decoded, bool):
        return "true" if decoded else "false"
    return str(decoded)


def _integer_struct_format(*, size: int, signed: bool) -> str | None:
    if signed:
        return {
            1: "<b",
            2: "<h",
            4: "<i",
            8: "<q",
        }.get(size)
    return {
        1: "<B",
        2: "<H",
        4: "<I",
        8: "<Q",
    }.get(size)
