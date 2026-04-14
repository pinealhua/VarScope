from __future__ import annotations

import sys
from pathlib import Path

_APP_ROOT_OVERRIDE: Path | None = None


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return source_root()


def set_app_root(path: Path) -> None:
    global _APP_ROOT_OVERRIDE
    _APP_ROOT_OVERRIDE = Path(path).resolve()


def app_root() -> Path:
    return _APP_ROOT_OVERRIDE or default_app_root()


def runtime_dir() -> Path:
    return app_root() / "runtime"


def resource_path(*parts: str) -> Path:
    root_candidate = app_root().joinpath(*parts)
    if root_candidate.exists():
        return root_candidate

    bundle_root = Path(getattr(sys, "_MEIPASS", app_root()))
    return bundle_root.joinpath(*parts)