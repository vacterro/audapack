"""PERF-001: the release marker is read once per bundle revision, not per call.

The bundled userscript is ~841 KB and read_bundled_widget_metadata re-read and
re-scanned all of it every time, with no cache. It sits under
_get_required_widget_build, so ONE dispatcher.status() with six live workers did
seven full reads, a /v1/browser/status response thirteen, and a single
/v1/browser/poll nine -- on a four-second poll, for a marker that changes when
the operator upgrades the widget and at no other time.

Measured by the audit: 3.76 ms and 802 KB per call; 1.02 s and 401 MiB per 500.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from audapack.components import widget as widget_mod

SCRIPT = "// ==UserScript==\n// @name AUDAPACK Widget\n// @version {version}\n// ==/UserScript==\n"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A stand-in userscript whose reads are counted."""
    path = tmp_path / "AUDAPACK_WIDGET.user.js"
    path.write_text(SCRIPT.format(version="1.2.3") + "x" * 200_000, encoding="utf-8")
    monkeypatch.setattr(widget_mod, "get_bundled_widget_path", lambda: path)
    widget_mod._WIDGET_METADATA_CACHE.clear()

    reads: list[str] = []
    original = Path.read_text

    def counted(self, *args, **kwargs):
        if self == path:
            reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    return path, reads


def test_unchanged_content_is_read_once_however_often_it_is_asked(bundle):
    path, reads = bundle
    first = widget_mod.read_bundled_widget_metadata()
    for _ in range(50):
        widget_mod.read_bundled_widget_metadata()
    assert first["version"] == "1.2.3"
    assert len(reads) == 1, f"{len(reads)} bundle reads for 51 lookups"


def test_the_dispatcher_hot_path_reads_it_once_not_once_per_worker(bundle):
    """dispatcher.status() with six workers did seven full reads."""
    _path, reads = bundle
    from audapack.bridge.browser_dispatch import _get_required_widget_build

    for _ in range(13):  # the count a six-worker /v1/browser/status reached
        _get_required_widget_build()
    assert len(reads) <= 1, f"{len(reads)} bundle reads across a status-sized burst"


def test_new_content_invalidates_immediately(bundle):
    """Replacing the bundle under a live Bridge must not need a restart."""
    path, reads = bundle
    assert widget_mod.read_bundled_widget_metadata()["version"] == "1.2.3"

    time.sleep(0.01)
    path.write_text(SCRIPT.format(version="9.9.9") + "y" * 200_001, encoding="utf-8")
    assert widget_mod.read_bundled_widget_metadata()["version"] == "9.9.9"
    assert len(reads) == 2


def test_a_touch_alone_invalidates_it(bundle):
    """The key is (path, mtime_ns, size), so mtime alone has to count."""
    import os

    path, reads = bundle
    widget_mod.read_bundled_widget_metadata()
    stamp = time.time() + 10
    os.utime(path, (stamp, stamp))
    widget_mod.read_bundled_widget_metadata()
    assert len(reads) == 2


def test_a_missing_bundle_is_reported_not_cached(tmp_path, monkeypatch):
    """And the placeholder must not become the answer for the process's life."""
    missing = tmp_path / "gone.user.js"
    monkeypatch.setattr(widget_mod, "get_bundled_widget_path", lambda: missing)
    widget_mod._WIDGET_METADATA_CACHE.clear()

    meta = widget_mod.read_bundled_widget_metadata()
    assert meta["exists"] is False
    assert meta["version"] == "0.0.01"

    missing.write_text(SCRIPT.format(version="4.5.6"), encoding="utf-8")
    assert widget_mod.read_bundled_widget_metadata()["version"] == "4.5.6"


def test_an_unreadable_bundle_is_not_cached(bundle, monkeypatch):
    """Or one bad read pins the placeholder version until the Bridge restarts."""
    path, _reads = bundle
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")))
    assert widget_mod.read_bundled_widget_metadata()["version"] == "0.0.01"

    monkeypatch.setattr(Path, "read_text", original)
    assert widget_mod.read_bundled_widget_metadata()["version"] == "1.2.3"


def test_the_caller_may_still_mutate_what_it_gets_back(bundle):
    """Callers have always been handed a dict of their own."""
    _path, _reads = bundle
    first = widget_mod.read_bundled_widget_metadata()
    first["version"] = "tampered"
    assert widget_mod.read_bundled_widget_metadata()["version"] == "1.2.3"


def test_cost_no_longer_scales_with_bundle_size(tmp_path, monkeypatch):
    """The audit's own shape of proof: a bigger bundle must not cost more."""
    widget_mod._WIDGET_METADATA_CACHE.clear()

    def timed(size: int) -> float:
        path = tmp_path / f"w{size}.user.js"
        path.write_text(SCRIPT.format(version="1.0.0") + "z" * size, encoding="utf-8")
        monkeypatch.setattr(widget_mod, "get_bundled_widget_path", lambda: path)
        widget_mod.read_bundled_widget_metadata()  # prime
        start = time.perf_counter()
        for _ in range(200):
            widget_mod.read_bundled_widget_metadata()
        return time.perf_counter() - start

    small = timed(10_000)
    large = timed(2_000_000)  # 200x the bytes
    assert large < small * 5, f"200x the bundle cost {large / max(small, 1e-9):.1f}x the time"


def test_the_install_root_is_resolved_once():
    """app_dir() resolves a path on disk and sits under the same hot path.

    get_bundled_widget_path() calls it, so every widget-metadata lookup paid
    for it: 0.14 ms a call, 70 ms of the 103 ms that 500 lookups cost once the
    metadata itself was cached. A running process cannot have its own source
    move out from under it.
    """
    from audapack.config import app_dir

    app_dir()
    info = app_dir.cache_info()
    before = info.hits
    for _ in range(20):
        app_dir()
    assert app_dir.cache_info().hits >= before + 20
    assert app_dir().is_dir()
