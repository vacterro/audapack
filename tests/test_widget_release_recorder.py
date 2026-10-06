"""T-196 TARGETS A/B/C/D: the RECORDER enforces monotonic release identity.

DEFECT THIS CLOSES. `scripts/update_widget_release.py` used to recompute the
shipped bytes' SHA-256 and overwrite the release ledger unconditionally. A
developer could materially change the Widget, keep the same `// @version`, run
the recorder, and the ledger was rewritten to the new digest -- after which
`--check` passed. The ledger proved internal consistency of the CURRENT tree
and nothing about release history: different production bytes, same userscript
version, checks green. That is the exact T-196 failure class the ledger exists
to prevent, so it is exercised here directly against the recorder, not only
against `widget_release_errors()`.

The rule under test (one canonical comparator, one transition):

    different bytes  ->  strictly greater numeric @version, or REFUSE

No write happens before validation, so a refusal leaves the previous ledger
byte-for-byte intact; a first record is the ONE explicit bootstrap path; a
malformed ledger fails closed; a failed write cannot destroy the old ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audapack.components import widget as widget_mod

SCRIPT = (
    "// ==UserScript==\n"
    "// @name         AUDAPACK Widget\n"
    "// @version      {version}\n"
    "// @updateURL    http://127.0.0.1:17843/widget.user.js\n"
    "// @downloadURL  http://127.0.0.1:17843/widget.user.js\n"
    "// ==/UserScript==\n"
    "// {body}\n"
)


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    """A userscript + ledger pair at explicit paths the recorder reads/writes."""
    resources = tmp_path / "resources"
    resources.mkdir()
    script = resources / widget_mod.WIDGET_FILE_NAME
    ledger = resources / widget_mod.WIDGET_RELEASE_FILE_NAME
    monkeypatch.setattr(widget_mod, "get_bundled_widget_path", lambda: script)
    monkeypatch.setattr(widget_mod, "get_widget_release_path", lambda: ledger)

    def write_script(version: str, body: str) -> None:
        # CRLF-free, so the digest is a function of content only.
        script.write_bytes(SCRIPT.format(version=version, body=body).encode("utf-8"))

    def record(version: str, body: str, *, bootstrap: bool = False) -> dict:
        write_script(version, body)
        return widget_mod.record_widget_release(bootstrap=bootstrap)

    write_script("0.0.60", "original")
    first = widget_mod.record_widget_release(bootstrap=True)
    assert first["ok"] and first["wrote"]
    return script, ledger, write_script, record


def _ledger(ledger: Path) -> dict:
    return json.loads(ledger.read_text(encoding="utf-8"))


def test_recorder_records_the_first_release_by_bootstrap(recorder):
    _script, ledger, _write, _record = recorder
    assert _ledger(ledger)["version"] == "0.0.60"


def test_same_version_changed_bytes_is_refused_and_ledger_unchanged(recorder):
    """TARGET C.1 + the red control: the pre-fix recorder laundered this."""
    _script, ledger, _write, record = recorder
    before = ledger.read_bytes()
    before_record = _ledger(ledger)

    # RED CONTROL: the PRE-FIX recorder did exactly this -- recompute the record
    # from current bytes and overwrite the ledger. It must launder, proving the
    # escape hatch was real.
    _write("0.0.60", "materially different implementation")
    laundered = widget_mod.widget_release_record()
    ledger.write_text(json.dumps(laundered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert widget_mod.widget_release_errors() == [], "pre-fix path no longer launders (control stale)"
    ledger.write_bytes(before)
    assert _ledger(ledger) == before_record

    # GREEN CONTROL: the fixed recorder refuses the same-version rewrite.
    result = record("0.0.60", "materially different implementation")
    assert result["ok"] is False
    assert result["code"] == "VERSION_NOT_INCREASED"
    assert result["wrote"] is False
    assert ledger.read_bytes() == before
    assert _ledger(ledger)["sha256"] == before_record["sha256"]


def test_a_downgrade_is_refused(recorder):
    """TARGET C.2: different bytes at a LOWER version never records."""
    _script, ledger, _write, record = recorder
    before = ledger.read_bytes()
    result = record("0.0.59", "older bytes, new digest")
    assert result["ok"] is False
    assert result["code"] == "VERSION_NOT_INCREASED"
    assert ledger.read_bytes() == before


def test_a_strictly_greater_version_records(recorder):
    """TARGET C.3: the legal transition."""
    _script, ledger, _write, record = recorder
    result = record("0.0.61", "new implementation")
    assert result["ok"] is True
    assert result["code"] == "RECORDED"
    assert result["wrote"] is True
    assert _ledger(ledger)["version"] == "0.0.61"
    assert widget_mod.widget_release_errors() == []


def test_unchanged_bytes_and_version_is_an_idempotent_no_op(recorder):
    """TARGET C.4/C.8: recording twice changes nothing, deterministically."""
    _script, ledger, _write, record = recorder
    before = ledger.read_bytes()
    for _ in range(2):
        result = record("0.0.60", "original")
        assert result["ok"] is True
        assert result["code"] == "NO_OP"
        assert result["wrote"] is False
        assert ledger.read_bytes() == before


def test_a_missing_ledger_bootstraps_only_when_explicit(recorder):
    """TARGET C.5: first record is an explicit bootstrap, never a silent one."""
    _script, ledger, _write, _record = recorder
    ledger.unlink()
    _write("0.0.60", "original")

    refused = widget_mod.record_widget_release()
    assert refused["ok"] is False
    assert refused["code"] == "MISSING_LEDGER"
    assert not ledger.exists()

    bootstrapped = widget_mod.record_widget_release(bootstrap=True)
    assert bootstrapped["ok"] and bootstrapped["wrote"]
    assert _ledger(ledger)["version"] == "0.0.60"


def test_a_malformed_ledger_fails_closed_without_bootstrap(recorder):
    """TARGET C.6: an unreadable ledger is never silently overwritten."""
    _script, ledger, write, _record = recorder
    ledger.write_text("{not json", encoding="utf-8")
    write("0.0.61", "new implementation")

    refused = widget_mod.record_widget_release()
    assert refused["ok"] is False
    assert refused["code"] == "MALFORMED_LEDGER"
    assert ledger.read_text(encoding="utf-8") == "{not json"

    recovered = widget_mod.record_widget_release(bootstrap=True)
    assert recovered["ok"] and recovered["wrote"]
    assert _ledger(ledger)["version"] == "0.0.61"


def test_a_failed_write_leaves_the_previous_ledger_intact(recorder, monkeypatch):
    """TARGET C.7: atomicity -- the evidence survives a failed write."""
    _script, ledger, _write, _record = recorder
    before = ledger.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(widget_mod.os, "replace", boom)
    _write("0.0.61", "new implementation")
    with pytest.raises(OSError):
        widget_mod.record_widget_release()
    assert ledger.read_bytes() == before
    leftovers = [
        p.name
        for p in ledger.parent.iterdir()
        if p.suffix == ".tmp" and ".widget-release-" in p.name
    ]
    assert leftovers == [], f"a failed write left temporary files behind: {leftovers}"


def test_a_prerelease_version_is_rejected_explicitly(recorder):
    """TARGET D: no silent comparison of an undefined ordering."""
    _script, ledger, _write, record = recorder
    before = ledger.read_bytes()
    result = record("0.0.61-beta", "new implementation")
    assert result["ok"] is False
    assert result["code"] == "UNSUPPORTED_VERSION"
    assert ledger.read_bytes() == before

    with pytest.raises(ValueError):
        widget_mod.widget_version_key("0.0.61-beta")


def test_the_canonical_version_comparator_is_numeric(recorder):
    """TARGET D: one comparator, numeric ordering, all consumers agree."""
    key = widget_mod.widget_version_key
    assert key("0.0.9") < key("0.0.10")
    assert key("0.0.59") < key("0.0.60") < key("0.0.61")
    assert not (key("0.0.60") < key("0.0.59"))
    with pytest.raises(ValueError):
        key("1.0.0.x")
    with pytest.raises(ValueError):
        key("")


def test_the_probe_and_the_recorder_share_one_comparator():
    """TARGET D: the probe orders versions through the SAME function."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "widget_update_probe_t197", root / "scripts" / "widget_update_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["widget_update_probe_t197"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.version_key("0.0.59") < module.version_key("0.0.60")
    assert module.version_key("0.0.60") == widget_mod.widget_version_key("0.0.60")
    assert module.version_key("not-a-version") == ()


def test_the_repository_itself_still_records_consistently():
    """The real committed bundle + ledger pass the fixed recorder's check."""
    errors = widget_mod.widget_release_errors()
    assert errors == [], "shipped widget release identity is inconsistent:\n" + "\n".join(errors)
    result = widget_mod.record_widget_release()
    assert result["ok"] is True
    assert result["code"] == "NO_OP"
    assert result["wrote"] is False
