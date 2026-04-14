from __future__ import annotations

import json
from pathlib import Path

from varscope.models import ProbePreset
from varscope.paths import app_root, runtime_dir


def repo_root() -> Path:
    return app_root()


def bundled_openocd_dir() -> Path:
    return runtime_dir() / "openocd"


def _preset_store_path() -> Path:
    path = runtime_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / "saved_presets.json"


def saved_presets() -> list[ProbePreset]:
    path = _preset_store_path()
    if not path.exists():
        return []

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    presets: list[ProbePreset] = []
    for item in payload:
        cfg_path = Path(item["cfg_path"])
        elf_text = item.get("elf_path")
        elf_path = Path(elf_text) if elf_text else None
        presets.append(
            ProbePreset(
                name=item["name"],
                cfg_path=cfg_path,
                elf_path=elf_path,
                description=item.get("description", "User archive"),
            )
        )
    return presets


def all_presets() -> list[ProbePreset]:
    return saved_presets()


def save_preset_archive(name: str, *, cfg_path: Path, elf_path: Path | None) -> ProbePreset:
    path = _preset_store_path()
    current = saved_presets()
    updated: list[ProbePreset] = []
    replaced = False
    for preset in current:
        if preset.name == name:
            updated.append(
                ProbePreset(
                    name=name,
                    cfg_path=Path(cfg_path),
                    elf_path=Path(elf_path) if elf_path else None,
                    description="User archive",
                )
            )
            replaced = True
        else:
            updated.append(preset)
    if not replaced:
        updated.append(
            ProbePreset(
                name=name,
                cfg_path=Path(cfg_path),
                elf_path=Path(elf_path) if elf_path else None,
                description="User archive",
            )
        )

    payload = [
        {
            "name": preset.name,
            "cfg_path": str(preset.cfg_path),
            "elf_path": str(preset.elf_path) if preset.elf_path else "",
            "description": preset.description,
        }
        for preset in updated
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return next(preset for preset in updated if preset.name == name)


def delete_preset_archive(name: str) -> bool:
    current = saved_presets()
    updated = [preset for preset in current if preset.name != name]
    if len(updated) == len(current):
        return False

    path = _preset_store_path()
    payload = [
        {
            "name": preset.name,
            "cfg_path": str(preset.cfg_path),
            "elf_path": str(preset.elf_path) if preset.elf_path else "",
            "description": preset.description,
        }
        for preset in updated
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True