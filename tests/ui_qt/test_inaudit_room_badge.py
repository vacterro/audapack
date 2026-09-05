"""The project room answers "has this project pending audits" (SRC-035:R1).

The badge exists so the operator does not open every project's INAUDIT inbox to
find out where audits are waiting. A count that only reflects the classifier's
guess sends the operator back into the inbox for exactly the captures they
already triaged by hand.
"""

import uuid

import pytest

from audapack.config import AppConfig, AuditsConfig
from audapack.inaudit_capture import InauditCaptureStore
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_room_model import ProjectRoomModel


@pytest.fixture
def room(tmp_path, qapp):
    projects = [
        Project(id="p1", display_name="Project 1", source_path=str(tmp_path / "p1"),
                priority_group="MAIN0", slot=1),
        Project(id="p2", display_name="Project 2", source_path=str(tmp_path / "p2"),
                priority_group="MAIN0", slot=2),
    ]
    for project in projects:
        (tmp_path / project.id).mkdir()
    config = AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits")), projects=projects)
    service = ProjectService(config, base_dir=tmp_path)
    model = ProjectRoomModel(service)
    store = InauditCaptureStore(base_dir=tmp_path)
    return model, projects, store, tmp_path


def _label(model, project_id: str) -> str:
    index = model.index_for_project_id(project_id)
    return str(model.data(index, model.ROLES["inaudit_label"]) or "")


def _capture(store, projects, text: str) -> str:
    capture_id = str(uuid.uuid4())
    store.capture(
        {
            "capture_id": capture_id,
            "text": text,
            "capture_kind": "response",
            "source": "test",
        },
        projects,
    )
    return capture_id


def test_layer_count_is_visible_without_opening_the_inbox(room):
    model, _projects, _store, tmp_path = room
    audit_dir = tmp_path / "p1" / "audit"
    audit_dir.mkdir()
    (audit_dir / "1.md").write_text("first", encoding="utf-8")
    (audit_dir / "2.md").write_text("second", encoding="utf-8")

    model.reload()

    assert _label(model, "p1") == "IA 2"
    assert _label(model, "p2") == ""


def test_a_suggested_capture_counts_as_pending_on_its_project(room):
    model, projects, store, tmp_path = room
    _capture(store, projects, f"# audit of {tmp_path / 'p1'}\nbody")

    model.reload()

    assert _label(model, "p1") == "IA 0 +1"
    assert _label(model, "p2") == ""


def test_a_pinned_capture_counts_on_the_project_the_operator_chose(room):
    model, projects, store, _tmp_path = room
    capture_id = _capture(store, projects, "# generic note\nnothing identifying here")
    record = store.get(capture_id)["record"]
    assert not record.get("suggested_project_id"), record.get("classification_evidence")

    store.set_target_project(capture_id, "p2", projects)
    model.reload()

    assert _label(model, "p2") == "IA 0 +1"
    assert _label(model, "p1") == ""
