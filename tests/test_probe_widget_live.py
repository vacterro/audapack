"""T-267: the live probe must never carry a literal production version.

`scripts/probe_widget_live.py` judged worker freshness against a hardcoded
`== "0.0.93"`, so every widget release after that one turned a healthy fleet
into a FAIL and a real defect into a PASS. The expected build now comes from
canonical runtime truth (`dispatch.required_widget_build`), and the assertion
path is a pure function of (required, worker).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_probe():
    path = REPO / "scripts" / "probe_widget_live.py"
    imported = sys.modules.get("probe_widget_live")
    if imported is not None:
        return imported
    spec = importlib.util.spec_from_file_location("probe_widget_live", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_widget_live"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_equal_build_is_current():
    probe = _load_probe()
    assert probe.classify_worker_build("9.9.9", "9.9.9") == "current"


def test_one_older_build_is_stale():
    probe = _load_probe()
    assert probe.classify_worker_build("0.0.94", "0.0.93") == "stale"


def test_ahead_build_is_also_not_current():
    probe = _load_probe()
    assert probe.classify_worker_build("0.0.94", "0.0.95") == "stale"


def test_missing_side_is_unknown_not_pass():
    probe = _load_probe()
    assert probe.classify_worker_build("", "0.0.94") == "unknown"
    assert probe.classify_worker_build("0.0.94", "") == "unknown"
    assert probe.classify_worker_build(None, None) == "unknown"


def test_required_build_comes_from_the_bridge_not_a_literal(monkeypatch):
    probe = _load_probe()
    monkeypatch.setattr(
        probe,
        "bridge_get",
        lambda path: {"dispatch": {"required_widget_build": "0.0.99"}},
    )
    assert probe.required_widget_build() == "0.0.99"


def test_no_hardcoded_version_in_the_assertion_path():
    source = (REPO / "scripts" / "probe_widget_live.py").read_text(encoding="utf-8")
    assert '== "0.0.' not in source
