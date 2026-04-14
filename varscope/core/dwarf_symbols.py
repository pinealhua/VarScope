from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.dwarf.enums import ENUM_DW_ATE
from elftools.dwarf.locationlists import LocationExpr, LocationParser
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from varscope.core.typesystem import StructField, TypeInfo, make_void_type


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(slots=True)
class VariableRecord:
    name: str
    address: int
    type_info: TypeInfo


@dataclass(slots=True)
class SymbolDatabase:
    elf_path: Path
    elf_sha256: str
    _records: dict[str, list[VariableRecord]]

    def lookup_variable(self, name: str) -> VariableRecord | None:
        records = self._records.get(name)
        if not records:
            return None
        if len(records) > 1:
            return None
        return records[0]

    def is_ambiguous(self, name: str) -> bool:
        records = self._records.get(name)
        return bool(records and len(records) > 1)

    def all_variables(self) -> list[VariableRecord]:
        result: list[VariableRecord] = []
        for records in self._records.values():
            if not records:
                continue
            result.append(records[0])
        return result


class ElfSymbolLoader:
    def __init__(self, elf_path: Path):
        self.elf_path = Path(elf_path)

    def load(self) -> SymbolDatabase:
        if not self.elf_path.exists():
            raise FileNotFoundError(self.elf_path)

        sha256 = hashlib.sha256(self.elf_path.read_bytes()).hexdigest()
        records: dict[str, list[VariableRecord]] = {}

        with self.elf_path.open("rb") as f:
            elf = ELFFile(f)
            records.update(self._load_dwarf_variables(elf))
            self._add_symbol_table_fallbacks(elf, records)

        return SymbolDatabase(elf_path=self.elf_path, elf_sha256=sha256, _records=records)

    def _load_dwarf_variables(self, elf: ELFFile) -> dict[str, list[VariableRecord]]:
        if not elf.has_dwarf_info():
            return {}

        dwarfinfo = elf.get_dwarf_info()
        type_parser = _DwarfTypeParser()
        location_parser = LocationParser(dwarfinfo.location_lists())
        records: dict[str, list[VariableRecord]] = {}

        for cu in dwarfinfo.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_variable":
                    continue
                name = _variable_name(die)
                if not name or not _is_identifier(name):
                    continue
                address = _variable_address(die, location_parser, dwarfinfo)
                if address is None or address <= 0:
                    continue
                type_die = _variable_type_die(die)
                type_info = type_parser.parse(type_die) if type_die is not None else _fallback_type_from_size(4)
                records.setdefault(name, []).append(
                    VariableRecord(name=name, address=address, type_info=type_info)
                )

        return records

    def _add_symbol_table_fallbacks(self, elf: ELFFile, records: dict[str, list[VariableRecord]]) -> None:
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for symbol in section.iter_symbols():
                name = symbol.name
                if not name or name in records or not _is_identifier(name):
                    continue

                sym_type = symbol["st_info"]["type"]
                if sym_type not in {"STT_OBJECT", "STT_NOTYPE"}:
                    continue

                address = int(symbol["st_value"])
                if address <= 0:
                    continue

                byte_size = int(symbol["st_size"]) or 4
                records.setdefault(name, []).append(
                    VariableRecord(
                        name=name,
                        address=address,
                        type_info=_fallback_type_from_size(byte_size),
                    )
                )


class _DwarfTypeParser:
    def __init__(self) -> None:
        self._cache: dict[int, TypeInfo] = {}

    def parse(self, die: Any | None) -> TypeInfo:
        if die is None:
            return make_void_type()
        cached = self._cache.get(die.offset)
        if cached is not None:
            return cached

        tag = die.tag
        if tag == "DW_TAG_base_type":
            info = self._parse_base_type(die)
        elif tag == "DW_TAG_enumeration_type":
            info = self._parse_enum_type(die)
        elif tag == "DW_TAG_pointer_type":
            info = self._parse_pointer_type(die)
        elif tag == "DW_TAG_array_type":
            info = self._parse_array_type(die)
        elif tag in {"DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"}:
            info = self._parse_struct_type(die)
        elif tag in {"DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_restrict_type"}:
            info = self._parse_wrapper_type(die)
        else:
            info = self._parse_unknown_type(die)

        self._cache[die.offset] = info
        return info

    def _parse_base_type(self, die: Any) -> TypeInfo:
        name = _die_name(die) or "base"
        byte_size = _attr_int(die, "DW_AT_byte_size", 1)
        encoding = _encoding_name(_attr_int(die, "DW_AT_encoding", 0))
        return TypeInfo(
            kind="base",
            name=name,
            byte_size=max(1, byte_size),
            signed=encoding in {"DW_ATE_signed", "DW_ATE_signed_char"},
            floating=encoding in {"DW_ATE_float", "DW_ATE_complex_float"},
            boolean=encoding == "DW_ATE_boolean" or name == "bool" or name == "_Bool",
        )

    def _parse_enum_type(self, die: Any) -> TypeInfo:
        name = _die_name(die) or "enum"
        byte_size = _attr_int(die, "DW_AT_byte_size", 4)
        enum_values: dict[int, str] = {}
        for child in die.iter_children():
            if child.tag != "DW_TAG_enumerator":
                continue
            child_name = _die_name(child)
            value = _attr_int(child, "DW_AT_const_value")
            if child_name is not None and value is not None:
                enum_values[value] = child_name
        return TypeInfo(
            kind="enum",
            name=name,
            byte_size=max(1, byte_size),
            signed=True,
            enum_values=enum_values,
        )

    def _parse_pointer_type(self, die: Any) -> TypeInfo:
        target_die = _type_die(die)
        target_type = self.parse(target_die) if target_die is not None else make_void_type()
        byte_size = _attr_int(die, "DW_AT_byte_size", _cu_address_size(die))
        return TypeInfo(
            kind="pointer",
            name=f"{target_type.describe()}*",
            byte_size=max(1, byte_size),
            target_type=target_type,
        )

    def _parse_array_type(self, die: Any) -> TypeInfo:
        element_type = self.parse(_type_die(die))
        length = _array_length(die)
        byte_size = _attr_int(die, "DW_AT_byte_size")
        if byte_size is None and length is not None:
            byte_size = element_type.unwrap().byte_size * length
        if byte_size is None:
            byte_size = element_type.unwrap().byte_size
        suffix = "?" if length is None else str(length)
        return TypeInfo(
            kind="array",
            name=f"{element_type.describe()}[{suffix}]",
            byte_size=max(1, byte_size),
            element_type=element_type,
            array_length=length,
        )

    def _parse_struct_type(self, die: Any) -> TypeInfo:
        kind = "union" if die.tag == "DW_TAG_union_type" else "struct"
        name = _die_name(die) or ("union" if kind == "union" else "struct")
        byte_size = _attr_int(die, "DW_AT_byte_size", 1)
        info = TypeInfo(kind=kind, name=name, byte_size=max(1, byte_size))
        self._cache[die.offset] = info

        fields: list[StructField] = []
        for child in die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            field_name = _die_name(child)
            if not field_name or not _is_identifier(field_name):
                continue
            field_type_die = _type_die(child)
            field_type = self.parse(field_type_die) if field_type_die is not None else _fallback_type_from_size(1)
            fields.append(
                StructField(
                    name=field_name,
                    type_info=field_type,
                    bit_offset=_member_bit_offset(child),
                )
            )
        info.fields = fields
        return info

    def _parse_wrapper_type(self, die: Any) -> TypeInfo:
        target_type = self.parse(_type_die(die))
        wrapper_kind = {
            "DW_TAG_typedef": "typedef",
            "DW_TAG_const_type": "const",
            "DW_TAG_volatile_type": "volatile",
            "DW_TAG_restrict_type": "restrict",
        }[die.tag]
        name = _die_name(die) or target_type.describe()
        return TypeInfo(
            kind=wrapper_kind,
            name=name,
            byte_size=target_type.unwrap().byte_size,
            target_type=target_type,
        )

    def _parse_unknown_type(self, die: Any) -> TypeInfo:
        target_die = _type_die(die)
        if target_die is not None:
            return self.parse(target_die)
        byte_size = _attr_int(die, "DW_AT_byte_size", 1)
        return TypeInfo(kind="base", name=_die_name(die) or die.tag, byte_size=max(1, byte_size))


def _is_identifier(name: str) -> bool:
    return bool(_IDENTIFIER_RE.match(name))


def _fallback_type_from_size(byte_size: int) -> TypeInfo:
    byte_size = max(1, int(byte_size))
    return TypeInfo(kind="base", name=f"uint{max(8, byte_size * 8)}_t", byte_size=byte_size, signed=False)


def _encoding_name(value: int | None) -> str:
    if value is None:
        return ""
    inverse = {enum_value: enum_name for enum_name, enum_value in ENUM_DW_ATE.items()}
    return inverse.get(value, "")


def _attr_int(die: Any, name: str, default: int | None = None) -> int | None:
    attr = die.attributes.get(name)
    if attr is None:
        return default
    value = attr.value
    if isinstance(value, int):
        return value
    return default


def _die_name(die: Any | None) -> str | None:
    if die is None:
        return None
    attr = die.attributes.get("DW_AT_name")
    if attr is None:
        return None
    value = attr.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _referenced_die(die: Any, attr_name: str) -> Any | None:
    if attr_name not in die.attributes:
        return None
    try:
        return die.get_DIE_from_attribute(attr_name)
    except Exception:
        return None


def _variable_name(die: Any) -> str | None:
    name = _die_name(die)
    if name:
        return name
    for attr_name in ("DW_AT_specification", "DW_AT_abstract_origin"):
        referenced = _referenced_die(die, attr_name)
        name = _die_name(referenced)
        if name:
            return name
    return None


def _type_die(die: Any) -> Any | None:
    return _referenced_die(die, "DW_AT_type")


def _variable_type_die(die: Any) -> Any | None:
    type_die = _type_die(die)
    if type_die is not None:
        return type_die
    for attr_name in ("DW_AT_specification", "DW_AT_abstract_origin"):
        referenced = _referenced_die(die, attr_name)
        type_die = _type_die(referenced) if referenced is not None else None
        if type_die is not None:
            return type_die
    return None


def _cu_address_size(die: Any) -> int:
    try:
        return int(die.cu.header["address_size"])
    except Exception:
        return 4


def _array_length(die: Any) -> int | None:
    length = 1
    found = False
    for child in die.iter_children():
        if child.tag != "DW_TAG_subrange_type":
            continue
        found = True
        count = _attr_int(child, "DW_AT_count")
        if count is None:
            lower_bound = _attr_int(child, "DW_AT_lower_bound", 0) or 0
            upper_bound = _attr_int(child, "DW_AT_upper_bound")
            if upper_bound is None:
                return None
            count = upper_bound - lower_bound + 1
        if count < 0:
            return None
        length *= count
    return length if found else None


def _member_bit_offset(die: Any) -> int:
    data_bit_offset = _attr_int(die, "DW_AT_data_bit_offset")
    if data_bit_offset is not None:
        return data_bit_offset

    attr = die.attributes.get("DW_AT_data_member_location")
    if attr is None:
        return 0
    if isinstance(attr.value, int):
        return attr.value * 8

    try:
        ops = DWARFExprParser(die.cu.structs).parse_expr(attr.value)
    except Exception:
        return 0
    for op in ops:
        if op.op_name in {"DW_OP_plus_uconst", "DW_OP_constu", "DW_OP_consts"} and op.args:
            return int(op.args[0]) * 8
    return 0


def _variable_address(die: Any, location_parser: LocationParser, dwarfinfo: Any) -> int | None:
    attr = die.attributes.get("DW_AT_location")
    if attr is None:
        return None

    try:
        location = location_parser.parse_from_attribute(attr, die.cu["version"], die=die)
    except Exception:
        location = LocationExpr(attr.value) if isinstance(attr.value, list) else None

    if isinstance(location, LocationExpr):
        return _address_from_location_expr(location.loc_expr, die, dwarfinfo)

    if isinstance(location, list):
        for entry in location:
            loc_expr = getattr(entry, "loc_expr", None)
            if loc_expr is None:
                continue
            address = _address_from_location_expr(loc_expr, die, dwarfinfo)
            if address is not None:
                return address
    return None


def _address_from_location_expr(expr: list[int], die: Any, dwarfinfo: Any) -> int | None:
    try:
        ops = DWARFExprParser(die.cu.structs).parse_expr(expr)
    except Exception:
        return None
    if not ops:
        return None

    first = ops[0]
    if first.op_name == "DW_OP_addr" and first.args:
        return int(first.args[0])
    if first.op_name in {"DW_OP_addrx", "DW_OP_GNU_addr_index"} and first.args:
        try:
            return int(dwarfinfo.get_addr(die.cu, int(first.args[0])))
        except Exception:
            return None
    return None
