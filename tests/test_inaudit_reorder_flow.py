"""T-163: layer priority reorder must survive the audit flow.

Dragging layers into priority order renumbers canonical files. The Agent
Inbox reads canonical numbers from disk, so the inbox state of a reordered
project must be IDENTICAL to the pre-reorder state it renders for the same
set of bytes (same verdicts, same presence), the user-layer tracker must
agree with disk, and rename-after-reorder must still refuse a taken number.
"""

from __future__ import annotations

from pathlib import Path

from audapack import agent_inbox
from audapack.inaudit import (
    ensure_next_layer,
    last_user_layer,
    list_inaudit_layers,
    rename_inaudit_layer,
    reorder_inaudit_layers,
)
from audapack.models import Project


def _project(tmp_path: Path) -> Project:
    root = tmp_path / "P"
    root.mkdir()
    return Project(id="p1", display_name="P", source_path=str(root))


def _write_layer(project: Project, number: int, text: str) -> None:
    d = Path(project.source_path) / "audit"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{number}.md").write_text(text, encoding="utf-8")


def _inbox_layers(root: Path) -> list[tuple[int, str, bool]]:
    state = agent_inbox.read_inbox(root)
    rows = []
    for item in state.layers:
        verdict = item.verdict.value if hasattr(item.verdict, "value") else str(item.verdict)
        rows.append((item.layer, verdict, item.present))
    return rows


class TestReorderSurvivesAuditFlow:
    def test_agent_inbox_verdicts_are_identical_across_a_reorder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        root = Path(project.source_path)
        _write_layer(project, 1, "widget one")
        ensure_next_layer(project)
        _write_layer(project, 2, "user two")
        _write_layer(project, 3, "third")
        before = _inbox_layers(root)
        assert reorder_inaudit_layers(project, [3, 2, 1]) == ""
        after = _inbox_layers(root)
        # Same bytes -> same verdicts; only the NUMBERS moved (they are the
        # priority), and UNREAD presence survives every step.
        assert [v for _l, v, _p in before] == [v for _l, v, _p in after]
        assert [p for _l, _v, p in before] == [p for _l, _v, p in after]
        assert [v for _l, v, _p in after] == ["UNREAD", "UNREAD", "UNREAD"]

    def test_tracker_agrees_with_disk_after_reorder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        _write_layer(project, 1, "a")
        ensure_next_layer(project)
        _write_layer(project, 2, "user two")
        assert reorder_inaudit_layers(project, [2, 1]) == ""
        disk_numbers = sorted(layer.number for layer in list_inaudit_layers(project))
        assert last_user_layer(project) in disk_numbers

    def test_rename_after_reorder_refuses_a_taken_number(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        for n in (1, 2, 3):
            _write_layer(project, n, str(n))
        assert reorder_inaudit_layers(project, [3, 2, 1]) == ""
        assert rename_inaudit_layer(project, 1, 2) != ""
        assert (Path(project.source_path) / "audit" / "1.md").read_text(encoding="utf-8") == "3"

    def test_reorder_idempotency_is_operation_identity_not_filename_only(self, tmp_path, monkeypatch):
        """CORE-003: the old test called [2,1] twice and asserted only that the
        FILENAMES stayed [1,2] -- while the bodies flipped back every replay.
        A replay of the SAME operation id must return the already-committed
        success; a NEW operation id is a new intentional gesture."""
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        _write_layer(project, 1, "A")
        _write_layer(project, 2, "B")
        d = Path(project.source_path) / "audit"
        assert reorder_inaudit_layers(project, [1, 2]) == ""  # already in order
        assert reorder_inaudit_layers(project, [2, 1], operation_id="op-1") == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "B"
        assert (d / "2.md").read_text(encoding="utf-8") == "A"
        # Replay of the SAME operation id: no second permutation.
        assert reorder_inaudit_layers(project, [2, 1], operation_id="op-1") == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "B"
        assert (d / "2.md").read_text(encoding="utf-8") == "A"
        # A NEW operation id represents a new intentional reverse gesture.
        assert reorder_inaudit_layers(project, [2, 1], operation_id="op-2") == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "A"
        assert (d / "2.md").read_text(encoding="utf-8") == "B"

    def test_five_layers_rotate_through_the_full_priority_cycle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
        project = _project(tmp_path)
        bodies = {n: f"body-{n}" for n in range(1, 6)}
        for n, text in bodies.items():
            _write_layer(project, n, text)
        # bottom to top, then back
        assert reorder_inaudit_layers(project, [5, 4, 3, 2, 1]) == ""
        d = Path(project.source_path) / "audit"
        for canonical in range(1, 6):
            assert (d / f"{canonical}.md").read_text(encoding="utf-8") == f"body-{6 - canonical}"
        assert reorder_inaudit_layers(project, [5, 4, 3, 2, 1]) == ""
        for canonical in range(1, 6):
            assert (d / f"{canonical}.md").read_text(encoding="utf-8") == f"body-{canonical}"
