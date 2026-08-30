"""Pytest session fixtures for isolating user runtime environment."""

import os
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

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


@pytest.fixture
def bridge_server():
    """In-memory Bridge HTTP server for integration tests.

    Yields ``(config, base_url)`` where ``config`` carries a test token and a
    scratch audit root under a temp dir. The server binds port 0 (OS-assigned)
    so concurrent test runs never collide.
    """
    from audapack.bridge.server import AudapackBridgeHandler
    from audapack.config import AppConfig, save_config

    temp_dir = tempfile.mkdtemp(prefix="audapack_bridge_test_")
    audit_root = Path(temp_dir) / "AUDITING_IMPLEMENTATION"
    audit_root.mkdir(parents=True)

    config = AppConfig()
    config.audits.root = str(audit_root)
    config.bridge.host = "127.0.0.1"
    config.bridge.port = 0
    config.bridge.token = "test_secret_token_123456789"

    class TestHandler(AudapackBridgeHandler):
        pass

    TestHandler.config = config
    TestHandler.test_base_dir = temp_dir
    save_config(config, base_dir=temp_dir)
    server = ThreadingHTTPServer((config.bridge.host, config.bridge.port), TestHandler)
    config.bridge.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.bridge.host}:{config.bridge.port}"
    try:
        yield config, base_url
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)

