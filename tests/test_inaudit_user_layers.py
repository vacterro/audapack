"""User-layer tracker: the [edit] row button must never open a widget layer.

Layers are otherwise indistinguishable -- they are all canonical `<N>.md`
files -- so the one fact that separates them is WHO created them. The tracker
records only desktop-created layers (ensure_next_layer) and must survive a
restart, follow renames and reorders, and forget deletions.
"""

from __future__ import annotations

from pathlib import Path

from audapack.inaudit import (
    ensure_next_layer,
    last_user_layer,
    list_inaudit_layers,
    renumber_user_layer,
    reorder_inaudit_layers,
    user_layer_registry_path,
)
from audapack.models import Project
from audapack.saipen_transport import is_managed


def _project(tmp_path: Path) -> Project:
    root = tmp_path / "P"
    root.mkdir()
    return Project(id="p1", display_name="P", source_path=str(root))


def _write_layer(project: Project, number: int, text: str) -> None:
    d = Path(project.source_path) / "audit"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{number}.md").write_text(text, encoding="utf-8")


class TestUserLayerTracker:
    def test_ensure_next_layer_marks_the_layer_as_user_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        created = ensure_next_layer(project)
        assert created.name == "1.md"
        assert last_user_layer(project) == 1

    def test_a_widget_layer_is_never_recorded_and_edit_skips_it(self, tmp_path, monkeypatch):
        """A layer that arrived without going through ensure_next_layer is
        opaque to [edit]: last_user_layer must return None, not the widget's."""
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        _write_layer(project, 1, "widget delivery")
        assert last_user_layer(project) is None

    def test_user_layer_creation_order_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        ensure_next_layer(project)  # user layer 1
        _write_layer(project, 2, "widget between")
        ensure_next_layer(project)  # user layer 3
        assert last_user_layer(project) == 3

    def test_delete_forgets_the_layer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        from audapack.inaudit import delete_inaudit_layer

        project = _project(tmp_path)
        ensure_next_layer(project)
        assert last_user_layer(project) == 1
        assert delete_inaudit_layer(project, 1) == ""
        assert last_user_layer(project) is None

    def test_rename_moves_the_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        from audapack.inaudit import rename_inaudit_layer

        project = _project(tmp_path)
        ensure_next_layer(project)
        _write_layer(project, 2, "other")
        assert rename_inaudit_layer(project, 1, 5) == ""
        assert last_user_layer(project) == 5

    def test_reorder_keeps_the_last_user_layer_pointing_at_its_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        _write_layer(project, 1, "widget one")
        ensure_next_layer(project)  # user layer 2
        _write_layer(project, 2, "user two")
        assert reorder_inaudit_layers(project, [2, 1]) == ""
        # layer 2 (the user layer) is now first, canonical number 1
        numbers = [layer.number for layer in list_inaudit_layers(project)]
        assert numbers == [1, 2]
        assert last_user_layer(project) == 1
        path = Path(project.source_path) / "audit" / "1.md"
        assert path.read_text(encoding="utf-8") == "user two"

    def test_renumber_user_layer_follows_a_move(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        ensure_next_layer(project)
        _write_layer(project, 4, "moved body")
        renumber_user_layer(project, 1, 4)
        assert last_user_layer(project) == 4

    def test_registry_is_restart_surviving(self, tmp_path, monkeypatch):
        runtime = tmp_path / "runtime"
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(runtime))
        project = _project(tmp_path)
        ensure_next_layer(project)
        assert user_layer_registry_path(project).is_file()
        assert last_user_layer(project) == 1

    def test_managed_projects_forbid_reorder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        root = tmp_path / "M"
        root.mkdir()
        (root / ".saipen").mkdir()
        project = Project(id="m1", display_name="M", source_path=str(root))
        assert is_managed(root)
        _write_layer(project, 1, "a")
        _write_layer(project, 2, "b")
        reason = reorder_inaudit_layers(project, [2, 1])
        assert reason and "SAIPEN owns" in reason


class TestReorderInauditLayers:
    def test_reorder_renumbers_without_overwriting(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        for n, text in ((1, "first"), (2, "second"), (3, "third")):
            _write_layer(project, n, text)
        assert reorder_inaudit_layers(project, [3, 1, 2]) == ""
        d = Path(project.source_path) / "audit"
        assert (d / "1.md").read_text(encoding="utf-8") == "third"
        assert (d / "2.md").read_text(encoding="utf-8") == "first"
        assert (d / "3.md").read_text(encoding="utf-8") == "second"

    def test_reorder_rejects_a_stale_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        _write_layer(project, 1, "a")
        _write_layer(project, 2, "b")
        assert reorder_inaudit_layers(project, [1, 2, 99]) != ""

    def test_reorder_keeps_selection_on_the_moved_layer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        from audapack.inaudit import get_inaudit_selected, set_inaudit_selected

        project = _project(tmp_path)
        for n in (1, 2, 3):
            _write_layer(project, n, str(n))
        set_inaudit_selected(project, 2)
        assert reorder_inaudit_layers(project, [2, 3, 1]) == ""
        assert get_inaudit_selected(project) == 1  # layer 2 moved to the front
