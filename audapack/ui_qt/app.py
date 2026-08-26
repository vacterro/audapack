"""Qt application entry (Wave L). Imports PySide6 only here."""

from __future__ import annotations

import sys


def run_qt_gui(service=None) -> int:
    from PySide6.QtWidgets import QApplication

    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    if service is None:
        service = ProjectService()

    app = QApplication(sys.argv)
    window = MainWindow(service)
    window.show()
    return app.exec()
