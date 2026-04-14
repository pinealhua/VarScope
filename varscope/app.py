from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from varscope.core.workspace_store import ensure_workspace_dirs
from varscope.paths import set_app_root
from varscope.ui.main_window import MainWindow


def _configure_application(app: QApplication) -> None:
    QLocale.setDefault(QLocale(QLocale.Chinese, QLocale.China))
    app.setLayoutDirection(Qt.LeftToRight)

    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
        if family in QFontDatabase.families():
            app.setFont(QFont(family, 9))
            break


def run_app(*, app_root: Path, initial_elf: Path | None = None, initial_cfg: Path | None = None) -> int:
    app_root = Path(app_root).resolve()
    set_app_root(app_root)
    ensure_workspace_dirs(app_root)

    QLocale.setDefault(QLocale(QLocale.Chinese, QLocale.China))
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    _configure_application(app)

    window = MainWindow(app_root=app_root, initial_elf=initial_elf, initial_cfg=initial_cfg)
    window.show()

    if owns_app:
        return int(app.exec())
    return 0