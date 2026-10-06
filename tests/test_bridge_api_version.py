"""W2-004 (SRC-041:R008): /health must require real API-version compatibility.

`bool(supported_api_versions)` accepted any truthy form -- the string "3", the
dict {"3": True}, the bare int 99 -- as compatible, so a Bridge advertising
api_version=99 + supported=[99] answered healthy and the client then spoke a
protocol neither side understood. Compatibility is now an intersection of real
integer versions.
"""
from __future__ import annotations

import json

import pytest

import audapack.bridge.lifecycle as lc


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _health(monkeypatch, payload: dict, status: int = 200):
    monkeypatch.setattr(
        lc.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(payload, status),
    )
    return lc.check_bridge_health("127.0.0.1", 1)


@pytest.mark.parametrize("api_version,supported,healthy", [
    # 99 + [99] -> incompatible: no shared version, despite a truthy list.
    (99, [99], False),
    # 99 + [3, 99] -> healthy: 3 is shared.
    (99, [3, 99], True),
    # Supported primaries, no list needed.
    (3, None, True),
    (2, None, True),
    (3, [], True),
    # Unsupported primary + empty/absent/malformed list -> incompatible.
    (99, [], False),
    (99, None, False),
    (99, "3", False),
    (99, {"3": True}, False),
    # Mixed invalid members are dropped; a remaining valid 2 keeps it healthy.
    (99, ["3", {"3": True}, 2], True),
    # bool api_version is not an int version.
    (True, None, False),
])
def test_version_matrix(api_version, supported, healthy, monkeypatch):
    payload = {"service": "AUDAPACK Bridge", "api_version": api_version}
    if supported is not None:
        payload["supported_api_versions"] = supported
    ok, detail = _health(monkeypatch, payload)
    assert ok is healthy
    if not healthy:
        assert detail["status"] == "incompatible_api_version", detail


def test_legacy_acbbridge_status_unchanged(monkeypatch):
    ok, detail = _health(monkeypatch, {"service": "ACBBridge"})
    assert ok is False
    assert detail["status"] == "legacy_acbbridge"


def test_wrong_service_status_unchanged(monkeypatch):
    ok, detail = _health(monkeypatch, {"service": "SomeOtherDaemon"})
    assert ok is False
    assert detail["status"] == "wrong_service"


def test_one_supported_set_shared_with_the_server():
    from audapack.bridge import server as server_mod

    assert lc.SUPPORTED_API_VERSIONS == server_mod.SUPPORTED_API_VERSIONS
    assert set(lc.SUPPORTED_API_VERSIONS) == {2, 3}


def test_start_does_not_short_circuit_on_an_incompatible_bridge(monkeypatch):
    """I5: an incompatible endpoint owning the port is not 'already healthy'.

    The old probe returned healthy for it, so start_bridge_background answered
    True and never started anything -- while every subsequent call spoke a
    protocol the endpoint did not understand. It must attempt the start and
    report honestly when the incompatible owner keeps the port.
    """
    monkeypatch.setattr(
        lc, "check_bridge_health",
        lambda *a, **k: (False, {"status": "incompatible_api_version"}),
    )
    monkeypatch.setattr(lc, "is_bridge_healthy", lambda *a, **k: False)
    started = {"popen": 0}

    class _Proc:
        pass

    monkeypatch.setattr(
        lc.subprocess, "Popen",
        lambda *a, **k: started.__setitem__("popen", started["popen"] + 1) or _Proc(),
    )
    monkeypatch.setattr(lc.time, "sleep", lambda *a: None)

    assert lc.start_bridge_background() is False
    assert started["popen"] == 1, "an incompatible endpoint was treated as already healthy"
