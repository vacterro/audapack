"""Pytest session fixtures for isolating user runtime environment."""

import os
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

# -- no test may open a real browser window ------------------------------- #
#
# The suite once did, and silently: an armed START AUDIT debounce outliving its
# window fired on a later test's event loop, ran the real dispatch, and left a
# Chromium worker running on the operator's desktop forever -- 45 of 71 stored
# runs had one. It never failed a test, because the launch happens on a
# background thread. So the ban is enforced here rather than trusted: a browser
# spawn is refused at the process boundary and named at session end.

_REAL_POPEN = subprocess.Popen
_BROWSER_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe")
_BROWSER_LAUNCHES: list[tuple[str, str]] = []
_CURRENT_TEST = {"id": "<session>"}


class _NoBrowserPopen(_REAL_POPEN):
    """``subprocess.Popen`` that refuses to start a browser during tests."""

    def __init__(self, args, *rest, **kwargs):
        first = ""
        try:
            first = str(args[0]) if isinstance(args, (list, tuple)) and args else str(args)
        except Exception:
            first = "<unreadable>"
        if first.lower().endswith(_BROWSER_EXES):
            _BROWSER_LAUNCHES.append((_CURRENT_TEST["id"], first))
            raise RuntimeError(
                f"test tried to launch a real browser: {first} "
                f"(in {_CURRENT_TEST['id']}) -- stub the launcher instead"
            )
        super().__init__(args, *rest, **kwargs)


subprocess.Popen = _NoBrowserPopen


def pytest_runtest_setup(item):
    _CURRENT_TEST["id"] = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    if not _BROWSER_LAUNCHES:
        return
    session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line("REAL BROWSER LAUNCH ATTEMPTED DURING TESTS:", red=True)
        for nodeid, exe in _BROWSER_LAUNCHES:
            reporter.write_line(f"  {nodeid} -> {exe}", red=True)


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

