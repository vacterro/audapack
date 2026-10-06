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


def _interactive_console(args) -> bool:
    """An agent console a test must never open on the operator's desktop.

    ``-NoExit`` keeps a PowerShell window open after its command, and an
    OpenCode/agent launcher starts a real agent session. Both outlive the test
    and were found running by the dozen after suite runs (launch tests whose
    admission fell back to a real degraded launch).
    """
    try:
        parts = [str(a) for a in args] if isinstance(args, (list, tuple)) else [str(args)]
    except Exception:
        return False
    lowered = [p.lower() for p in parts]
    if any(p == "-noexit" for p in lowered):
        return True
    return any("ai_agent_launcher" in p or p.endswith(("opencode.cmd", "opencode.exe")) for p in lowered)


class _NoBrowserPopen(_REAL_POPEN):
    """``subprocess.Popen`` that refuses to start a browser or agent console during tests."""

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
        if _interactive_console(args):
            _BROWSER_LAUNCHES.append((_CURRENT_TEST["id"], f"agent console: {first}"))
            raise RuntimeError(
                f"test tried to open a real agent console: {first} "
                f"(in {_CURRENT_TEST['id']}) -- stub the launcher instead"
            )
        super().__init__(args, *rest, **kwargs)


subprocess.Popen = _NoBrowserPopen

# -- live operator scripts are not unit tests ----------------------------- #
#
# ``test_target_g_live.py`` drives a real Chrome over CDP at import time to
# exercise the T-198 Target-G ZIP acceptance by hand. It has no ``test_``
# functions; importing it runs a browser, so collection must skip it. It stays
# runnable directly (``python tests/test_target_g_live.py``) for an operator.
collect_ignore = ["test_target_g_live.py"]


def pytest_runtest_setup(item):
    _CURRENT_TEST["id"] = item.nodeid


# -- no test may block on a modal message box ------------------------------ #
#
# A static ``QMessageBox.warning(...)`` runs its own event loop and waits for a
# click nobody will ever make, so one unexpected modal hung the whole suite
# with no failure at all (seen in the launcher and double-click tests when
# OpenCode admission refused a placeholder path). Unpatched, the static
# dialogs answer at once and are recorded; a test that expects a dialog still
# patches it itself and sees its own mock.
_MODAL_CALLS: list[tuple[str, str, str]] = []


@pytest.fixture(autouse=True)
def _non_blocking_message_boxes(monkeypatch):
    try:
        from PySide6.QtWidgets import QMessageBox
    except ImportError:
        yield _MODAL_CALLS
        return

    def fake(kind, answer):
        def show(_parent, title="", text="", *args, **kwargs):
            _MODAL_CALLS.append((_CURRENT_TEST["id"], kind, str(text)))
            return answer
        return staticmethod(show)

    ok = QMessageBox.StandardButton.Ok
    for kind in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, kind, fake(kind, ok))
    # "No" is the non-destructive answer to any unexpected confirmation.
    monkeypatch.setattr(QMessageBox, "question", fake("question", QMessageBox.StandardButton.No))
    yield _MODAL_CALLS


def pytest_sessionfinish(session, exitstatus):
    if not _BROWSER_LAUNCHES:
        return
    session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line("REAL BROWSER OR AGENT CONSOLE LAUNCH ATTEMPTED DURING TESTS:", red=True)
        for nodeid, exe in _BROWSER_LAUNCHES:
            reporter.write_line(f"  {nodeid} -> {exe}", red=True)


@pytest.fixture(autouse=True)
def _no_sai_accounts_plane():
    """No automated run may see a REAL SAI Accounts control plane.

    The plane is an optional ambient service on the operator's machine, and
    ``discover_accounts`` now consults it. Left alone it leaks into every test
    that isolates a fake HOME: a real registry holding two Claude config
    directories merges them into a fake-home run that asked for one account and
    gets three. The suite must test AUDAPACK, not the developer's desktop.

    The seam is neutralized the same way the Qt platform and the browser spawn
    are, and a test that is specifically ABOUT the federation sets the plane it
    wants through its own ``monkeypatch``, which wins over this.
    """
    from audapack import sai_accounts
    previous_engine, previous_run = sai_accounts.TestEngine, sai_accounts.TestRun
    previous_cache = list(sai_accounts._LIST_CACHE)
    sai_accounts.TestEngine = lambda: ""
    sai_accounts.TestRun = None
    sai_accounts.remember([])
    try:
        yield
    finally:
        sai_accounts.TestEngine, sai_accounts.TestRun = previous_engine, previous_run
        sai_accounts._LIST_CACHE[:] = previous_cache


@pytest.fixture(autouse=True)
def isolate_audapack_runtime():
    """A fresh runtime per test, not per session.

    W2-009 (audit/3.md): this was session-scoped, so a test that writes durable
    security state -- the legacy-token revocation marker -- leaked it into every
    later test in the process, and
    `test_legacy_token_acceptance_marker_roundtrip` before
    `test_legacy_candidates_env_based_and_revocable` failed only because of
    collection order. A per-test runtime costs one mkdtemp per test and removes
    the whole class: nothing can leak in either direction, and no test can ever
    inspect or mutate the operator's real runtime directory.
    """
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

