"""Whether the agent has read a delivered audit, or the same work goes out twice.

READY means the station finished. It says nothing about whether anyone read the
result, and that is the fact the operator needs before pressing START AUDIT
again. SAIPEN journals it in `<project>/.saipen/intake/audit_inbox.json`; this
reads it and never writes anything anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from audapack import agent_inbox as si


def _project(tmp_path: Path, layers: dict[str, str] | None = None, binding: dict | None = None,
             residue: tuple[str, ...] = (), allocator: dict | None = None) -> Path:
    root = tmp_path / "proj"
    inbox = root / si.AUDIT_DIRNAME
    inbox.mkdir(parents=True)
    for name, text in (layers or {}).items():
        (inbox / name).write_text(text, encoding="utf-8")
    for name in residue:
        (inbox / name).write_text("x", encoding="utf-8")
    if binding is not None:
        binding = {"schema_version": si.SUPPORTED_SCHEMA_VERSION, **binding}
        target = root / si.BINDING_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(binding), encoding="utf-8")
    if allocator is not None:
        target = root / si.DEFAULT_ALLOCATOR_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(
            {"schema_version": si.SUPPORTED_SCHEMA_VERSION, **allocator}), encoding="utf-8")
    return root


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_project_with_no_inbox_says_so(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    state = si.read_inbox(root)
    assert state.verdict == si.NO_INBOX
    assert state.wants_new_audit is False, "no inbox is not proof the agent is idle"


def test_an_empty_inbox_is_ready_for_a_new_audit(tmp_path):
    state = si.read_inbox(_project(tmp_path))
    assert state.verdict == si.EMPTY
    assert state.wants_new_audit is True


def test_a_delivered_layer_nobody_captured_reads_unread(tmp_path):
    state = si.read_inbox(_project(tmp_path, layers={"1.md": "audit"}))
    assert state.verdict == si.UNREAD
    assert state.unread_count == 1
    assert state.wants_new_audit is False
    assert "cc" in state.guidance


def test_a_captured_layer_in_work_names_its_ticket(tmp_path):
    root = _project(tmp_path, layers={"1.md": "audit"}, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("audit"),
                       "receipt_id": "SRC-012", "linked_work": "T-1222", "generation": 1},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.IN_WORK
    assert "T-1222" in state.summary()
    assert state.wants_new_audit is False


def test_a_closed_and_deleted_layer_reads_done(tmp_path):
    """SAIPEN's own repo looks exactly like this: inbox empty, records DELETED."""
    root = _project(tmp_path, binding={"layers": {
        "audit/1.md": {"state": "DELETED", "file_sha256": _sha("audit"), "receipt_id": "SRC-012"},
        "audit/2.md": {"state": "DELETED", "file_sha256": _sha("more"), "receipt_id": "SRC-013"},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.CONSUMED
    assert state.wants_new_audit is True
    assert [item.present for item in state.layers] == [False, False]


def test_rewritten_bytes_are_a_new_generation_nobody_has_read(tmp_path):
    """Identity is content, never mtime: copy and checkout move mtime alone."""
    root = _project(tmp_path, layers={"1.md": "second audit"}, binding={"layers": {
        "audit/1.md": {"state": "DELETED", "file_sha256": _sha("first audit"),
                       "receipt_id": "SRC-012", "generation": 1},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.UNREAD
    assert state.layers[0].generation == 2
    assert "rewritten" in state.layers[0].detail


def test_a_captured_layer_that_vanished_while_still_owed_is_flagged(tmp_path):
    root = _project(tmp_path, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("audit"), "receipt_id": "SRC-012"},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.BLOCKED
    assert "no longer in the inbox" in state.layers[0].detail


def test_the_worst_layer_decides_the_project_verdict(tmp_path):
    root = _project(tmp_path, layers={"1.md": "old", "2.md": "new"}, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("old"), "receipt_id": "SRC-012"},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.UNREAD, "one unread layer outranks one in progress"


def test_only_canonical_names_are_layers(tmp_path):
    """`01.md`, `notes.md`, `1.txt` are foreign: never read, never deleted."""
    root = _project(tmp_path, residue=("01.md", "notes.md", "1.txt", "PROJ__00_AUDIT_ALL_3.md"))
    state = si.read_inbox(root)
    assert state.verdict == si.EMPTY
    assert state.residue == ["01.md", "1.txt", "PROJ__00_AUDIT_ALL_3.md", "notes.md"]


def test_our_own_gitignore_is_infrastructure_not_residue(tmp_path):
    root = _project(tmp_path)
    (root / si.AUDIT_DIRNAME / ".gitignore").write_text("*\n", encoding="utf-8")
    assert si.read_inbox(root).residue == []


def test_a_subdirectory_is_never_a_layer(tmp_path):
    root = _project(tmp_path)
    (root / si.AUDIT_DIRNAME / "done").mkdir()
    (root / si.AUDIT_DIRNAME / "done" / "1.md").write_text("nested", encoding="utf-8")
    state = si.read_inbox(root)
    assert state.verdict == si.EMPTY
    assert state.residue == ["done"]


def test_next_layer_number_skips_live_and_settled_numbers(tmp_path):
    root = _project(tmp_path, layers={"2.md": "live"}, binding={"layers": {
        "audit/5.md": {"state": "DELETED", "file_sha256": "x"},
    }})
    assert si.next_layer_number(root) == 6


def test_next_layer_number_starts_at_one(tmp_path):
    assert si.next_layer_number(_project(tmp_path)) == 1


def test_an_unreadable_journal_is_unknown_not_a_confident_guess(tmp_path):
    """A copied contract needs the one field that says the contract moved."""
    root = _project(tmp_path, layers={"1.md": "audit"})
    target = root / si.BINDING_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    state = si.read_inbox(root)
    assert state.verdict == si.UNKNOWN
    assert state.wants_new_audit is False
    assert state.label != si.UNKNOWN, "a verdict with no label leaks a raw enum into the UI"


def test_a_journal_from_another_schema_is_not_parsed(tmp_path):
    root = _project(tmp_path, layers={"1.md": "audit"}, binding={"layers": {}})
    target = root / si.BINDING_REL
    target.write_text(json.dumps({"schema_version": 99, "layers": {}}), encoding="utf-8")
    assert si.read_inbox(root).verdict == si.UNKNOWN


def test_a_missing_journal_is_simply_never_consumed(tmp_path):
    """Absent is fine; only a journal that cannot be trusted is UNKNOWN."""
    assert si.read_inbox(_project(tmp_path, layers={"1.md": "audit"})).verdict == si.UNREAD


def test_residue_stops_a_settled_inbox_reading_clean(tmp_path):
    """The agent answers clean:false on this state; two tools must not disagree."""
    root = _project(tmp_path, residue=("notes.md",))
    state = si.read_inbox(root)
    assert state.verdict == si.EMPTY
    assert state.wants_new_audit is False
    assert "clean" not in state.guidance.lower() or "not clean" in state.guidance.lower()
    assert "notes.md" in state.guidance
    assert "+1 residue" in state.summary()


def test_residue_is_named_beside_a_real_verdict_too(tmp_path):
    root = _project(tmp_path, layers={"1.md": "audit"}, residue=("notes.md", "campaign.json"))
    state = si.read_inbox(root)
    assert state.verdict == si.UNREAD
    assert "never reads" in state.guidance
    assert "+2 residue" in state.summary()


def test_a_canonical_name_that_is_a_directory_is_a_bad_layer_not_residue(tmp_path):
    """Name decides. Calling it residue would disagree with the agent."""
    root = _project(tmp_path)
    (root / si.AUDIT_DIRNAME / "1.md").mkdir()
    state = si.read_inbox(root)
    assert state.residue == []
    assert state.verdict == si.BLOCKED
    assert "not a regular file" in state.layers[0].detail


def test_the_allocator_floor_covers_a_reserved_but_unplaced_number(tmp_path):
    """SAIPEN reserves an id before the bytes land, so it is on neither side.

    Live proof: its allocator held next_id 5 with layer 4 committed while disk
    and binding topped out at 3. A two-source floor hands out 4 and keys two
    different audits on the same audit/4.md.
    """
    root = _project(
        tmp_path,
        binding={"layers": {"audit/3.md": {"state": "DELETED", "file_sha256": "x"}}},
        allocator={"next_id": 5, "operations": {"manual op-1": {"layer": 4}}},
    )
    assert si.next_layer_number(root) == 5


def test_an_allocator_from_another_schema_contributes_nothing(tmp_path):
    root = _project(tmp_path, layers={"2.md": "live"})
    target = root / si.DEFAULT_ALLOCATOR_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": 99, "next_id": 40}), encoding="utf-8")
    assert si.next_layer_number(root) == 3


def test_the_cache_follows_the_inbox_rather_than_a_clock(tmp_path):
    root = _project(tmp_path)
    assert si.read_inbox_cached(root, now=1000.0).verdict == si.EMPTY
    (root / si.AUDIT_DIRNAME / "1.md").write_text("audit", encoding="utf-8")
    # Same instant, changed directory: a delivery must be visible at once.
    assert si.read_inbox_cached(root, now=1000.0).verdict == si.UNREAD


def test_an_active_capture_with_no_work_is_not_someone_working_it(tmp_path):
    """Live _SAIWORK2: audit/1.md ACTIVE, SRC-002, linked_work null.

    Reading the state field alone said "the agent is working this now. Running
    a new one duplicates the work" and the operator waits for a worker that
    does not exist. The capture never became a ticket; it is still owed.
    """
    root = _project(tmp_path, layers={"1.md": "audit"}, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("audit"),
                       "receipt_id": "SRC-002", "linked_work": None},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.UNREAD
    assert state.stalled_count == 1
    assert "SRC-002" in state.layers[0].detail
    assert "never turned into work" in state.guidance
    assert "cc" in state.guidance


def test_an_active_capture_that_became_work_is_in_progress(tmp_path):
    root = _project(tmp_path, layers={"1.md": "audit"}, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("audit"),
                       "receipt_id": "SRC-002", "linked_work": "T-1222"},
    }})
    assert si.read_inbox(root).verdict == si.IN_WORK


def test_a_blocked_diagnostic_never_starves_a_workable_layer(tmp_path):
    """Live __SAITULS: orphan record for a gone audit/1.md, unread 2.md on disk.

    A vanished transport is a diagnostic -- the receipt is already durable
    authority -- so it must not become the headline and send the operator
    hunting a failure reason instead of running cc on the audit sitting there.
    The agent pins the same rule: an invalid lower layer never starves a later
    workable one.
    """
    root = _project(tmp_path, layers={"2.md": "fresh"}, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("gone"),
                       "receipt_id": "SRC-004", "linked_work": "T-66"},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.UNREAD
    assert state.blocked_count == 1
    assert "!1 blocked" in state.summary(), "the diagnostic stays visible"
    assert "not a blocker" in state.guidance


def test_a_blocked_layer_is_still_the_headline_when_it_is_all_there_is(tmp_path):
    root = _project(tmp_path, binding={"layers": {
        "audit/1.md": {"state": "ACTIVE", "file_sha256": _sha("gone"), "receipt_id": "SRC-004"},
    }})
    state = si.read_inbox(root)
    assert state.verdict == si.BLOCKED
    assert "!1 blocked" not in state.summary(), "no note duplicating the headline"


def test_unread_outranks_in_work_which_outranks_blocked(tmp_path):
    """The headline is the ACTION, not how alarming a layer looks."""
    assert si._VERDICT_URGENCY[si.UNREAD] > si._VERDICT_URGENCY[si.IN_WORK]
    assert si._VERDICT_URGENCY[si.IN_WORK] > si._VERDICT_URGENCY[si.BLOCKED]
    assert si._VERDICT_URGENCY[si.UNKNOWN] > si._VERDICT_URGENCY[si.UNREAD]


# ------------------------------------------------------------ T-134 residue
#
# An assert on a runtime condition vanishes under python -O, and an
# `except Exception` around a layer scan disguises a real bug in the scanner
# as "this project has no layers" -- an empty inbox and no error anywhere.


def test_a_project_with_no_source_path_gets_a_real_error():
    import pytest as _pytest

    from audapack.inaudit import ensure_next_layer
    from audapack.models import Project

    with _pytest.raises(ValueError, match="no audit inbox"):
        ensure_next_layer(Project(id="p", display_name="P", source_path=""))


def test_a_broken_scanner_is_not_reported_as_an_empty_inbox(tmp_path, monkeypatch):
    import pytest as _pytest

    from audapack import inaudit
    from audapack.models import Project

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "1.md").write_text("layer", encoding="utf-8")
    project = Project(id="p", display_name="P", source_path=str(tmp_path))
    assert len(inaudit.list_inaudit_layers(project)) == 1

    def boom(_n):
        raise RuntimeError("a real bug in here")

    monkeypatch.setattr(inaudit, "_human_size", boom)
    with _pytest.raises(RuntimeError):
        inaudit.list_inaudit_layers(project)


def test_a_non_numeric_file_is_simply_not_a_layer(tmp_path):
    from audapack.inaudit import list_inaudit_layers
    from audapack.models import Project

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "1.md").write_text("layer", encoding="utf-8")
    (audit_dir / "notes.md").write_text("not a layer", encoding="utf-8")
    layers = list_inaudit_layers(Project(id="p", display_name="P", source_path=str(tmp_path)))
    assert [layer.number for layer in layers] == [1]


# ------------------------------------------------------------------ R008
#
# The layer number came from a directory scan and the empty file was written
# afterwards, so a layer created in between was emptied instead of skipped.


def test_a_layer_created_after_the_scan_is_skipped_not_emptied(tmp_path, monkeypatch):
    from audapack import inaudit
    from audapack.models import Project

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "1.md").write_text("someone else's audit text", encoding="utf-8")
    project = Project(id="p", display_name="P", source_path=str(tmp_path))
    monkeypatch.setattr(inaudit, "list_inaudit_layers", lambda _project: [])

    target = inaudit.ensure_next_layer(project)

    assert target.name == "2.md"
    assert (audit_dir / "1.md").read_text(encoding="utf-8") == "someone else's audit text"
