"""T-196 Target D: the update probe's own verdicts are machine-checkable.

The probe exists because Tampermonkey's dashboard step is not scriptable, so the
part that CAN be proven mechanically must actually be proven -- not merely
printed. These tests drive `scripts/widget_update_probe.py` against the real
Bridge endpoint and assert the verdict flips for the right reasons.

A probe that always says PASS would be worse than no probe: it would launder an
unbumped delivery into "verified".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from audapack.components import widget as widget_mod

REPO = Path(__file__).resolve().parent.parent


def _load_probe():
    path = REPO / "scripts" / "widget_update_probe.py"
    imported = sys.modules.get("widget_update_probe")
    if imported is not None:
        return imported
    spec = importlib.util.spec_from_file_location("widget_update_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["widget_update_probe"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return _load_probe()


def test_the_probe_passes_against_a_live_bridge_with_an_older_install(probe, bridge_server):
    _config, base_url = bridge_server
    result = probe.probe(base_url, installed_version="0.0.59")
    assert result["verdict"] == "PASS", result["checks"]
    assert result["checks"]["served_version_is_newer"] is True
    assert result["checks"]["served_bytes_match_recorded_release"] is True
    assert result["checks"]["directives_point_at_this_bridge"] is True


def test_the_probe_fails_when_the_served_version_is_not_newer(probe, bridge_server):
    """The T-196 symptom: the browser is already 'current' and never updates."""
    _config, base_url = bridge_server
    served = widget_mod.read_widget_release()["version"]
    result = probe.probe(base_url, installed_version=served)
    assert result["verdict"] == "FAIL"
    assert result["checks"]["served_version_is_newer"] is False


def test_the_probe_fails_when_an_installed_version_is_ahead(probe, bridge_server):
    _config, base_url = bridge_server
    result = probe.probe(base_url, installed_version="99.0.0")
    assert result["verdict"] == "FAIL"


def test_the_probe_checks_the_delivered_bytes_not_only_the_version(probe, bridge_server, tmp_path, monkeypatch):
    """A served script that is not the shipped one is not a release."""
    _config, base_url = bridge_server
    other = tmp_path / "other.user.js"
    other.write_bytes(b"// ==UserScript==\n// @version 0.0.60\n// ==/UserScript==\n")
    monkeypatch.setattr(probe, "get_bundled_widget_path", lambda: other)
    result = probe.probe(base_url, installed_version="0.0.59")
    assert result["verdict"] == "FAIL"
    assert result["checks"]["served_bytes_match_recorded_release"] is False
    assert result["checks"]["served_bytes_are_the_bundled_file"] is False


def test_the_probe_reports_a_dead_bridge_as_a_failure(probe):
    """An unreachable endpoint is never a PASS by omission."""
    result = probe.probe("http://127.0.0.1:9", installed_version="0.0.59")
    assert result["verdict"] == "FAIL"
    assert result["checks"]["http_200"] is False


def test_version_ordering_is_numeric_not_lexical(probe):
    assert probe.version_key("0.0.9") < probe.version_key("0.0.10")
    assert probe.version_key("0.0.59") < probe.version_key("0.0.60")
    assert not (probe.version_key("0.0.60") < probe.version_key("0.0.59"))
