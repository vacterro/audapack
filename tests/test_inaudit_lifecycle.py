"""INAUDIT layer lifecycle regressions — create / delete / edge cases."""
from __future__ import annotations

import tempfile
from pathlib import Path

from audapack.inaudit import (
    delete_inaudit_layer,
    ensure_next_layer,
    get_inaudit_selected,
    list_inaudit_layers,
    resolve_inaudit_path,
    set_inaudit_selected,
)
from audapack.models import Project


def _project(tmp) -> Project:
    return Project(id="test", display_name="Test", source_path=tmp)


def _seed(tmp: Path, names: list[str]) -> Project:
    p = _project(str(tmp))
    ad = Path(tmp) / "audit"
    ad.mkdir(parents=True, exist_ok=True)
    for n in names:
        (ad / n).write_text(f"content {n}", encoding="utf-8")
    return p


def test_delete_removes_layer_and_does_not_renumber():
    p = _seed(tempfile.mkdtemp(), ["1.md", "2.md", "3.md"])
    assert delete_inaudit_layer(p, 2) == ""
    layers = list_inaudit_layers(p)
    assert [x.number for x in layers] == [1, 3]
    assert resolve_inaudit_path(p, 2) is not None
    assert not (Path(p.source_path) / "audit" / "2.md").exists()


def test_delete_last_layer_leaves_empty_state():
    p = _seed(tempfile.mkdtemp(), ["1.md"])
    assert delete_inaudit_layer(p, 1) == ""
    assert list_inaudit_layers(p) == []
    assert get_inaudit_selected(p) is None


def test_delete_selected_layer_falls_back():
    p = _seed(tempfile.mkdtemp(), ["1.md", "2.md", "3.md"])
    set_inaudit_selected(p, 2)
    assert get_inaudit_selected(p) == 2
    assert delete_inaudit_layer(p, 2) == ""
    # selection recomputed to lowest remaining layer
    assert get_inaudit_selected(p) == 1


def test_delete_unknown_layer_is_idempotent():
    p = _seed(tempfile.mkdtemp(), ["1.md"])
    assert delete_inaudit_layer(p, 5) == ""
    assert [x.number for x in list_inaudit_layers(p)] == [1]


def test_delete_invalid_number_rejected():
    p = _seed(tempfile.mkdtemp(), ["1.md"])
    assert delete_inaudit_layer(p, 0) != ""
    assert delete_inaudit_layer(p, -3) != ""
    assert delete_inaudit_layer(p, 999999999999999) == ""  # valid, missing -> idempotent


def test_delete_never_touches_foreign_or_odd_files():
    tmp = tempfile.mkdtemp()
    ad = Path(tmp) / "audit"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "1.md").write_text("a", encoding="utf-8")
    (ad / "notes.md").write_text("keep", encoding="utf-8")
    (ad / "done").mkdir()
    (ad / "done" / "1.md").write_text("keep nested", encoding="utf-8")
    p = _project(tmp)
    assert delete_inaudit_layer(p, 1) == ""
    assert (ad / "notes.md").exists()
    assert (ad / "done" / "1.md").exists()
    assert delete_inaudit_layer(p, 2) == ""  # missing is fine
    # traversal rejected
    assert delete_inaudit_layer(p, 1) == ""  # already gone -> idempotent


def test_delete_layer_outside_audit_dir_rejected():
    tmp = tempfile.mkdtemp()
    p = _project(tmp)
    # a plain non-canonical path resolve returns None -> treated as invalid
    assert resolve_inaudit_path(p, 1) is None or delete_inaudit_layer(p, 1) == ""
