"""Save with no physical layer must mean something.

The editor showed Save as enabled for any selected project, and pressing it
with no audit/N.md on disk returned silently: no layer, no capture, no message.
An enabled control that does nothing is indistinguishable from a broken app, so
Save in that state is now read as "create the first canonical internal audit" --
managed through the capture -> SAIPEN enqueue path, unmanaged through the
allocator plus the durable commit primitive. Empty text mutates nothing and says
so. Every other refusal is visible too.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import audapack.inaudit as inaudit  # noqa: E402
from audapack.config import AppConfig, AuditsConfig  # noqa: E402
from audapack.models import Project  # noqa: E402
from audapack.services.project_service import ProjectService  # noqa: E402
from audapack.ui_qt.main_window import MainWindow  # noqa: E402


def _wait(widget, qapp, key="inaudit:assign"):
    deadline = time.monotonic() + 6
    while widget._task_runner.is_running(key) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture
def plain_window(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    src = tmp_path / "plain_src"
    src.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="plain", display_name="Plain", source_path=str(src),
                    priority_group="MAIN0", slot=1)
        ],
    )
    window = MainWindow(ProjectService(cfg, base_dir=tmp_path / "service"))
    project = window._service.get_project("plain")
    assert not inaudit.list_inaudit_layers(project), "the fixture must start with no layer"
    window.inaudit_widget.set_project(project)
    yield window
    window.close()


@pytest.fixture
def managed_window(tmp_path, qapp, monkeypatch):
    from tests.test_saipen_transport import _bind, _fake_cli

    home, _enqueue_calls = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    src = tmp_path / "managed_src"
    src.mkdir(parents=True, exist_ok=True)
    _bind(src, home)
    cfg = AppConfig(
        audits=AuditsConfig(root=str(tmp_path / "audits")),
        projects=[
            Project(id="managed", display_name="_AUDAPACK", source_path=str(src),
                    priority_group="MAIN0", slot=1)
        ],
    )
    window = MainWindow(ProjectService(cfg, base_dir=tmp_path / "service"))
    project = window._service.get_project("managed")
    assert not inaudit.list_inaudit_layers(project)
    window.inaudit_widget.set_project(project)
    yield window
    window.close()


def test_a_project_with_no_layer_reports_that_state(plain_window):
    widget = plain_window.inaudit_widget
    assert widget._action_state() == "NO_LAYER"
    assert widget.btn_save.isEnabled(), (
        "Save must stay usable here: it is how the first audit gets written"
    )
    # ...and nothing that needs a real layer may pretend there is one.
    assert not widget.btn_open.isEnabled()
    assert not widget.btn_delete.isEnabled()
    assert not widget.btn_rename.isEnabled()


def test_saving_an_empty_editor_says_nothing_to_save(plain_window):
    widget = plain_window.inaudit_widget
    project = plain_window._service.get_project("plain")
    widget.editor.setPlainText("   \n  ")
    widget._on_save()
    assert widget.status.text() == "Nothing to save"
    assert inaudit.list_inaudit_layers(project) == [], "empty text mutates nothing"


def test_saving_typed_text_with_no_layer_creates_the_first_audit(plain_window, qapp):
    widget = plain_window.inaudit_widget
    project = plain_window._service.get_project("plain")
    widget.editor.setPlainText("first canonical audit body")
    QApplication.processEvents()

    widget._on_save()
    qapp.processEvents()

    layers = inaudit.list_inaudit_layers(project)
    assert len(layers) == 1, f"expected one new layer, got {layers}"
    assert layers[0].path.read_text(encoding="utf-8") == "first canonical audit body"
    assert widget._editor_path == layers[0].path, "the editor binds to what it just wrote"
    assert not widget._dirty
    assert "saved" in widget.status.text().lower(), "a visible success, not a silent one"


def test_an_existing_active_layer_is_edited_never_replaced_by_a_new_one(plain_window, qapp):
    """Save only creates when NOTHING is bound; a real layer stays the target."""
    widget = plain_window.inaudit_widget
    project = plain_window._service.get_project("plain")
    audit_dir = Path(project.source_path) / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "7.md").write_text("seven", encoding="utf-8")

    widget.set_project(project)
    assert widget._action_state() in ("EXISTING_LAYER_CLEAN", "EXISTING_LAYER_DIRTY")
    widget.editor.setPlainText("next body")
    QApplication.processEvents()
    widget._on_save()
    qapp.processEvents()

    numbers = sorted(layer.number for layer in inaudit.list_inaudit_layers(project))
    assert numbers == [7], "an existing layer is edited in place, never duplicated"
    assert (audit_dir / "7.md").read_text(encoding="utf-8") == "next body"


def test_the_first_layer_number_comes_from_the_canonical_allocator(plain_window, qapp, monkeypatch):
    """Never a hardcoded 1.md: SAIPEN/inaudit owns which number is next."""
    from audapack.ui_qt.dialogs import inaudit_widget as iw

    seen = []
    real_allocate = iw.ensure_next_layer

    def _record(project):
        seen.append(project.id)
        return real_allocate(project)

    monkeypatch.setattr(iw, "ensure_next_layer", _record)
    widget = plain_window.inaudit_widget
    widget.editor.setPlainText("allocator driven")
    QApplication.processEvents()
    widget._on_save()
    qapp.processEvents()

    assert seen == ["plain"], "the allocator decides the number"


def test_a_managed_first_save_delivers_through_the_canonical_path(managed_window, qapp):
    """Managed projects must not get a raw ensure_next_layer: SAIPEN owns numbering."""
    widget = managed_window.inaudit_widget
    project = managed_window._service.get_project("managed")
    calls = {"n": 0}
    real_assign = widget._capture_store.assign

    def _assign(capture_id, project_id, projects, action="", after_assign=None):
        calls["n"] += 1
        return real_assign(capture_id, project_id, projects, action=action, after_assign=after_assign)

    widget._capture_store.assign = _assign

    widget.editor.setPlainText("managed first audit body")
    QApplication.processEvents()
    widget._on_save()
    _wait(widget, qapp)

    assert calls["n"] == 1, "SAIPEN enqueued exactly once"
    assert len(inaudit.list_inaudit_layers(project)) == 1
    written = inaudit.list_inaudit_layers(project)[0].path.read_text(encoding="utf-8")
    assert "managed first audit body" in written, "the typed text survives the draft switch"
    assert widget.managed_draft_delivery_pending is False
    assert not widget.editor.isReadOnly()


def test_a_managed_first_save_keeps_one_stable_capture_identity(managed_window, qapp, monkeypatch):
    """T-175 stands: a retry resolves the SAME operation, never a second layer."""
    widget = managed_window.inaudit_widget
    monkeypatch.setattr(
        widget._capture_store, "assign",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("saipen offline")),
    )
    widget.editor.setPlainText("retry me")
    QApplication.processEvents()

    widget._on_save()
    _wait(widget, qapp)
    first_id = widget.managed_draft_capture_id
    assert first_id, "a failed delivery keeps the draft recoverable"

    widget._on_save()
    _wait(widget, qapp)
    assert widget.managed_draft_capture_id == first_id


def test_an_unreadable_layer_never_becomes_a_phantom_target(plain_window, qapp):
    """A layer we cannot decode is not 'no layer'.

    Save must not then invent a brand new layer from a blanked editor, and it
    must not report success over a target it never actually read.
    """
    widget = plain_window.inaudit_widget
    project = plain_window._service.get_project("plain")
    audit_dir = Path(project.source_path) / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "1.md").write_bytes(b"\xff\xfe not utf-8 \xff")

    widget.set_project(project)
    assert widget._editor_path is None, "a failed read binds no path"
    assert "load" in widget.status.text().lower() or "read" in widget.status.text().lower()

    widget.editor.setPlainText("typed into an unknown target")
    QApplication.processEvents()
    widget._on_save()
    qapp.processEvents()

    assert [layer.number for layer in inaudit.list_inaudit_layers(project)] == [1], (
        "Save must not overwrite an unknown target from a stale editor"
    )
    assert widget.status.text() != ""
    assert "refused" in widget.status.text().lower() or "reload" in widget.status.text().lower()


def test_saving_with_no_project_selected_says_so(plain_window):
    widget = plain_window.inaudit_widget
    widget.set_project(None)
    widget.editor.setEnabled(False)
    widget._on_save()
    assert widget.status.text() != "", "even a refused Save has to say why"
