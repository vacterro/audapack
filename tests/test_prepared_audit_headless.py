"""Milestone S: the Bridge prepared-audit runtime builds without any Qt.

Qt is a UI client, not runtime infrastructure. The scheduler/audit service
path must remain usable in a headless Bridge process, so this proves the
canonical construction path imports and builds the AuditRunCoordinator without
pulling PySide6, MainWindow or a QApplication into the process.
"""

from __future__ import annotations

import subprocess
import sys


def test_headless_audit_runtime_builds_without_qt(tmp_path, monkeypatch):
    from audapack.config import AppConfig
    from audapack.prepared_audit import PreparedAuditRuntime, build_headless_audit_runtime

    config = AppConfig()
    runtime = build_headless_audit_runtime(config)
    assert isinstance(runtime, PreparedAuditRuntime)
    assert runtime.coordinator is not None
    # The coordinator carries the exact service set AuditRunCoordinator needs.
    assert runtime.coordinator.projects is not None
    assert runtime.coordinator.packing is not None
    assert runtime.coordinator.bridge is not None
    assert runtime.coordinator.audits is not None


def test_building_the_runtime_imports_no_pyside6():
    """A subprocess proves PySide6 is never imported by the construction path.

    Running in-process would be contaminated by any earlier Qt test in the
    session, so this uses a clean interpreter.
    """
    code = (
        "import sys;"
        "from audapack.prepared_audit import build_headless_audit_runtime;"
        "build_headless_audit_runtime();"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "{'PySide6','shiboken6','PyQt5','PyQt6'}];"
        "print('QT:'+','.join(sorted(bad)) if bad else 'CLEAN')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout, result.stdout
