"""Pytest session fixtures for isolating user runtime environment."""

import os
import shutil
import tempfile
import pytest


@pytest.fixture(autouse=True, scope="session")
def isolate_audapack_runtime():
    temp_dir = tempfile.mkdtemp(prefix="audapack_test_runtime_")
    old_val = os.environ.get("AUDAPACK_RUNTIME_DIR")
    os.environ["AUDAPACK_RUNTIME_DIR"] = temp_dir
    yield temp_dir
    if old_val is not None:
        os.environ["AUDAPACK_RUNTIME_DIR"] = old_val
    else:
        os.environ.pop("AUDAPACK_RUNTIME_DIR", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def qapp():
    """Provides a headless QCoreApplication / QApplication for Qt tests."""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            app = QApplication(["--platform", "offscreen"])
        yield app
    except ImportError:
        pytest.skip("PySide6 is not installed")

