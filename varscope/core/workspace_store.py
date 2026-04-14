from __future__ import annotations

from datetime import datetime
from pathlib import Path


def runtime_dir(app_root: Path) -> Path:
    return Path(app_root) / "runtime"


def sessions_dir(app_root: Path) -> Path:
    path = runtime_dir(app_root) / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_session_path(app_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return sessions_dir(app_root) / f"capture-{stamp}.dhrec.sqlite"


def ensure_workspace_dirs(app_root: Path) -> None:
    runtime_dir(app_root).mkdir(parents=True, exist_ok=True)
    sessions_dir(app_root)
