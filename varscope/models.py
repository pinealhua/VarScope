from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varscope.core.typesystem import TypeInfo


@dataclass(slots=True)
class ProbePreset:
    name: str
    cfg_path: Path
    elf_path: Path | None = None
    description: str = ""


@dataclass(slots=True)
class WatchDefinition:
    expression: str
    alias: str | None = None
    color: str = "#3BA7FF"
    plot_enabled: bool = True

    @property
    def display_name(self) -> str:
        return self.alias or self.expression


@dataclass(slots=True)
class ResolvedWatch:
    definition: WatchDefinition
    address: int
    byte_size: int
    type_name: str
    decoder_kind: str
    numeric: bool
    description: str
    type_info: TypeInfo


@dataclass(slots=True)
class SampleFrame:
    timestamp_ns: int
    raw_values: list[bytes]
    decoded_values: list[Any]


@dataclass(slots=True)
class SessionMetadata:
    elf_path: Path | None
    elf_sha256: str | None
    cfg_path: Path | None
    started_ns: int
    stopped_ns: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
