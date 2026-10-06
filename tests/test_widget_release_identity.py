"""T-196: different shipped userscript bytes require a different @version.

DEFECT THIS CLOSES. The Widget implementation changed materially between two
deliveries (responsive panel, supercompact toolbar, wider project picker,
viewport positioning, name disambiguation, tooltips, outside-click/Escape
close, Shift-click picker, ZIP regressions) while `// @version` stayed
`0.0.59` in both. `@version` is the only thing Tampermonkey compares when it
decides whether an installed script is current, so a real installed browser
kept executing the OLD bytes while the repository, its tests and the served
endpoint all exercised newer `0.0.59` bytes: the layout looked fixed in the
repository and stayed broken in the browser.

The invariant, checked here against the committed release ledger:

    DIFFERENT SHIPPED BYTES  ->  DIFFERENT (strictly later) @version

File timestamps are deliberately NOT release identity: a checkout, a copy and
a touch all rewrite them, and two different builds can share one timestamp.
"""

from __future__ import annotations

import json
import os
import time
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
def release(tmp_path, monkeypatch):
    """A userscript + release ledger pair whose reads are redirected."""
    resources = tmp_path / "resources"
    resources.mkdir()
    script = resources / widget_mod.WIDGET_FILE_NAME
    ledger = resources / widget_mod.WIDGET_RELEASE_FILE_NAME
    monkeypatch.setattr(widget_mod, "get_bundled_widget_path", lambda: script)
    monkeypatch.setattr(widget_mod, "get_widget_release_path", lambda: ledger)

    def write(version: str, body: str = "original", *, record: bool = True) -> None:
        # A CRLF-free write, so the digest is a function of content only.
        script.write_bytes(SCRIPT.format(version=version, body=body).encode("utf-8"))
        if record:
            record_ = widget_mod.widget_release_record()
            ledger.write_text(json.dumps(record_, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    write("0.0.59")
    return script, ledger, write


def test_the_repository_itself_ships_a_consistent_release():
    """The guard that would have caught the T-196 defect directly.

    `widget_release_errors()` is asserted empty against the REAL committed
    bundle and ledger, so a materially changed widget shipped under an
    unchanged `@version` cannot pass the suite.
    """
    errors = widget_mod.widget_release_errors()
    assert errors == [], "shipped widget release identity is inconsistent:\n" + "\n".join(errors)


def test_a_recorded_release_reads_back(release):
    _script, _ledger, _write = release
    assert widget_mod.widget_release_errors() == []
    record = widget_mod.read_widget_release()
    assert record["version"] == "0.0.59"
    assert record["sha256"] == _script_sha(_script)


def test_bytes_change_without_a_version_bump_is_refused(release):
    """The exact T-196 defect: new bytes, same `@version`."""
    _script, _ledger, write = release
    write("0.0.59", body="materially different implementation", record=False)
    errors = widget_mod.widget_release_errors()
    assert errors, "same-version byte drift was accepted"
    joined = "\n".join(errors)
    assert "shipped bytes changed without a release" in joined
    assert "same // @version" in joined


def test_a_version_bump_without_re_recording_the_release_is_refused(release):
    """Bumping the header alone must not launder unrecorded bytes."""
    _script, _ledger, write = release
    write("0.0.60", body="new implementation", record=False)
    errors = widget_mod.widget_release_errors()
    assert errors, "a bumped header over unrecorded bytes was accepted"
    assert "shipped bytes changed without a release" in "\n".join(errors)


def test_a_matching_bump_and_record_is_accepted(release):
    _script, _ledger, write = release
    write("0.0.60", body="new implementation")
    assert widget_mod.widget_release_errors() == []


def test_the_ledger_and_the_header_must_name_the_same_version(release):
    """A ledger edited to the wrong version is not a release either."""
    _script, ledger, _write = release
    record = json.loads(ledger.read_text(encoding="utf-8"))
    record["version"] = "0.0.58"
    ledger.write_text(json.dumps(record), encoding="utf-8")
    errors = widget_mod.widget_release_errors()
    assert errors
    assert "records version '0.0.58'" in "\n".join(errors)


def test_a_missing_ledger_names_the_recorder(release):
    _script, ledger, _write = release
    ledger.unlink()
    errors = widget_mod.widget_release_errors()
    assert errors
    joined = "\n".join(errors)
    assert "release ledger missing or unreadable" in joined
    assert "update_widget_release.py" in joined


def test_a_missing_bundle_is_reported(release):
    script, _ledger, _write = release
    script.unlink()
    errors = widget_mod.widget_release_errors()
    assert errors and "bundled userscript is missing" in errors[0]


def test_a_bundle_with_no_version_is_reported(release):
    script, _ledger, _write = release
    script.write_bytes(b"// ==UserScript==\n// @name x\n// ==/UserScript==\n")
    errors = widget_mod.widget_release_errors()
    assert errors and "declares no // @version" in errors[0]


def test_a_timestamp_alone_never_changes_the_verdict(release):
    """Release identity is content, not mtime (T-196 explicitly forbids mtime)."""
    script, _ledger, _write = release
    assert widget_mod.widget_release_errors() == []
    future = time.time() + 1000
    os.utime(script, (future, future))
    assert widget_mod.widget_release_errors() == []


def test_the_recorded_digest_is_the_sha256_of_the_shipped_bytes(release):
    script, _ledger, _write = release
    record = widget_mod.widget_release_record()
    assert record["sha256"] == _script_sha(script)
    assert record["script"] == widget_mod.WIDGET_FILE_NAME
    assert record["schema"] == widget_mod.WIDGET_RELEASE_SCHEMA


def test_the_bridge_reports_the_same_version_as_the_ledger():
    """The served/status surface and the release ledger cannot disagree."""
    from audapack.bridge.server import _get_widget_bundle_info

    version, sha_prefix = _get_widget_bundle_info()
    record = widget_mod.read_widget_release()
    assert version == record["version"]
    assert record["sha256"].startswith(sha_prefix)


def _script_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
