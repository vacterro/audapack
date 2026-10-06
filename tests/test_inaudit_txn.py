"""CORE-002 + CORE-003 regressions: primary/secondary commit boundary and
transactional, replay-idempotent reorder.

CORE-002: canonical numbered-layer mutation is PRIMARY; user-layer tracking is
SECONDARY. A tracker persistence failure must never make create/delete/rename
report failure, never duplicate a layer on retry, and managed delivery must
complete (no wedge, no re-enqueue). Repair intents are durable and idempotent.

CORE-003: reorder is one recoverable transaction. Failure at any park/place
step restores the exact pre-state (names, bytes, selection, ownership) or
leaves a RECOVERY_REQUIRED journal. Same operation id replays as already-
committed success; a new id is a new gesture. Scratch layers are hidden
noncanonical names, never numbered .md files.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from audapack import inaudit
from audapack.inaudit import (
    ensure_next_layer,
    get_inaudit_selected,
    last_user_layer,
    list_inaudit_layers,
    reorder_inaudit_layers,
    set_inaudit_selected,
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


def _snap(project: Project) -> dict:
    """Exact observable state: names, bytes, selection, user layers."""
    d = Path(project.source_path) / "audit"
    return {
        "layers": {p.name: p.read_text(encoding="utf-8") for p in sorted(d.iterdir()) if p.name.endswith(".md")},
        "all_entries": sorted(p.name for p in d.iterdir()),
        "selection": get_inaudit_selected(project),
        "user_layers": sorted(
            json.loads(inaudit.user_layer_registry_path(project).read_text(encoding="utf-8"))["user_layers"]
        )
        if inaudit.user_layer_registry_path(project).exists()
        else [],
    }


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDAPACK_RUNTIME_DIR", str(tmp_path / "runtime"))
    yield


@contextmanager
def _swap(obj, name, replacement):
    """Scoped attribute swap. Never monkeypatch.undo(): it would also revert
    the autouse AUDAPACK_RUNTIME_DIR fixture."""
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    try:
        yield
    finally:
        setattr(obj, name, original)


@pytest.fixture(autouse=True)
def _clear_move_hooks():
    inaudit.reorder_move_hooks.clear()
    inaudit.reorder_recovery_hooks.clear()
    yield
    inaudit.reorder_move_hooks.clear()
    inaudit.reorder_recovery_hooks.clear()


def _fail_at_move(stage: str, index: int, exception: Exception):
    """Abort reorder at the Nth park/place boundary (1-based, own counter)."""
    counter = [0]

    def _hook(observed_stage, src, dst):
        if observed_stage == stage:
            counter[0] += 1
            if counter[0] == index:
                raise exception

    inaudit.reorder_move_hooks.append(_hook)


def _count_moves():
    inaudit._hook_counter = [0]

    def _hook(stage, src, dst):
        inaudit._hook_counter[0] += 1

    inaudit.reorder_move_hooks.append(_hook)


# ---------------------------------------------------------------- CORE-002

class TestCreatePrimaryTruth:
    def test_tracker_failure_is_not_create_failure(self, tmp_path):
        project = _project(tmp_path)
        calls = {"n": 0}

        def _failing_save(path, numbers):
            calls["n"] += 1
            raise OSError("simulated sidecar write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save):
            created = ensure_next_layer(project)  # must NOT raise
        assert created.name == "1.md"
        assert created.is_file()
        # Retry after the sidecar writer heals: no N+1 phantom layer.
        created2 = ensure_next_layer(project)
        assert created2.name == "2.md"
        numbers = sorted(layer.number for layer in list_inaudit_layers(project))
        assert numbers == [1, 2]

    def test_repair_intent_replays_idempotently(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        ensure_next_layer(project)
        assert last_user_layer(project) == 1
        # Inject a tracker failure via the repair queue: forget the marker
        # directly (simulating a lost tracker update), then enqueue a record
        # intent and replay.
        queue_path = inaudit._repair_queue_path(project)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "queue": [
                        {"op": "record", "number": 1},
                        {"op": "record", "number": 1},
                        {"op": "record", "number": 9},  # layer 9 absent: dropped
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert inaudit.replay_tracker_repairs(project) == ""
        numbers = json.loads(inaudit.user_layer_registry_path(project).read_text(encoding="utf-8"))["user_layers"]
        assert numbers.count(1) == 1  # record N twice has ONE effect
        assert 9 not in numbers  # absent layer never recorded
        assert last_user_layer(project) == 1
        # Idempotent: replaying again changes nothing, queue stays empty.
        assert inaudit.replay_tracker_repairs(project) == ""
        remaining = json.loads(queue_path.read_text(encoding="utf-8"))["queue"]
        assert remaining == []

    def test_forget_and_renumber_repair_ops_are_idempotent(self, tmp_path):
        project = _project(tmp_path)
        _write_layer(project, 2, "user two")
        _write_layer(project, 5, "moved body")
        inaudit._edit_user_layers(project, lambda nums: nums.append(2))
        queue_path = inaudit._repair_queue_path(project)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "queue": [
                        {"op": "forget", "number": 3},  # absent: succeeds
                        {"op": "forget", "number": 3},  # replay: still succeeds
                        {"op": "renumber", "old": 2, "new": 5},
                        {"op": "renumber", "old": 2, "new": 5},  # replay: converges
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert inaudit.replay_tracker_repairs(project) == ""
        numbers = json.loads(inaudit.user_layer_registry_path(project).read_text(encoding="utf-8"))["user_layers"]
        assert numbers.count(5) == 1 and 2 not in numbers

    def test_renumber_repair_without_canonical_postcondition_dropped(self, tmp_path):
        # old exists, new does NOT: the rename never committed; replay must
        # not move the marker onto a nonexistent layer.
        project = _project(tmp_path)
        _write_layer(project, 1, "body")
        inaudit._edit_user_layers(project, lambda nums: nums.append(1))
        queue_path = inaudit._repair_queue_path(project)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps({"schema_version": 1, "queue": [{"op": "renumber", "old": 1, "new": 7}]}),
            encoding="utf-8",
        )
        assert inaudit.replay_tracker_repairs(project) == ""
        numbers = json.loads(inaudit.user_layer_registry_path(project).read_text(encoding="utf-8"))["user_layers"]
        # Old source still present, new target absent: rename never committed.
        assert numbers == [1]


class TestTrackerTruthfulness:
    def test_tracker_failure_with_durable_repair_reports_pending(self, tmp_path):
        project = _project(tmp_path)
        _write_layer(project, 1, "body")

        def _failing_save(path, numbers):
            raise OSError("simulated sidecar write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save):
            warning = inaudit.record_user_layer(project, 1)
        assert warning == inaudit.TRACKING_REPAIR_PENDING
        assert inaudit._repair_queue_path(project).is_file()

    def test_tracker_and_repair_queue_failure_is_truthful(self, tmp_path):
        project = _project(tmp_path)
        _write_layer(project, 1, "body")

        def _failing_save(path, numbers):
            raise OSError("simulated sidecar write failure")

        def _failing_json(path, data):
            raise OSError("simulated repair-queue write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save), _swap(
            inaudit, "_atomic_write_json", _failing_json
        ):
            warning = inaudit.record_user_layer(project, 1)
        assert warning == inaudit.TRACKING_REPAIR_UNPERSISTED
        # No durable repair exists, so a restart must not claim one.
        assert not inaudit._repair_queue_path(project).exists()
        assert inaudit.replay_tracker_repairs(project) == ""

    def test_repair_queue_unwritable_never_fails_primary_create(self, tmp_path):
        project = _project(tmp_path)

        def _failing_save(path, numbers):
            raise OSError("simulated sidecar write failure")

        def _failing_json(path, data):
            raise OSError("simulated repair-queue write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save), _swap(
            inaudit, "_atomic_write_json", _failing_json
        ):
            created = inaudit.ensure_next_layer(project)  # must not raise
        assert created.is_file()
        assert sorted(layer.number for layer in list_inaudit_layers(project)) == [1]


class TestDeleteRenamePrimaryTruth:
    def test_delete_tracker_failure_keeps_deletion_committed(self, tmp_path):
        project = _project(tmp_path)
        ensure_next_layer(project)
        layer = Path(project.source_path) / "audit" / "1.md"

        def _failing_save(path, numbers):
            raise OSError("simulated sidecar write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save):
            reason = inaudit.delete_inaudit_layer(project, 1)
        assert reason == ""
        assert not layer.exists()  # deletion REMAINS committed
        assert inaudit._repair_queue_path(project).exists()
        assert inaudit.replay_tracker_repairs(project) == ""
        assert last_user_layer(project) is None  # repair forgot the marker

    def test_rename_tracker_failure_keeps_rename_committed(self, tmp_path):
        project = _project(tmp_path)
        ensure_next_layer(project)  # user layer 1
        d = Path(project.source_path) / "audit"

        def _failing_save(path, numbers):
            raise OSError("simulated sidecar write failure")

        with _swap(inaudit, "_save_user_layers", _failing_save):
            reason = inaudit.rename_inaudit_layer(project, 1, 4)
        assert reason == ""
        assert (d / "4.md").is_file() and not (d / "1.md").exists()
        assert inaudit.replay_tracker_repairs(project) == ""
        assert last_user_layer(project) == 4  # repair moved the marker


# ---------------------------------------------------------------- CORE-003

class TestReorderTransaction:
    def test_park_step_failure_restores_exact_pre_state(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            _write_layer(project, n, text)
        ensure_next_layer(project)  # tracker sees user layer 4 too
        set_inaudit_selected(project, 2)
        before = _snap(project)
        assert before["user_layers"]

        _fail_at_move("park", 2, OSError("simulated park failure"))
        reason = reorder_inaudit_layers(project, [4, 3, 1, 2], operation_id="op-park")
        assert reason != ""
        after = _snap(project)
        assert after == before  # exact pre-state: names, bytes, selection, ownership

    def test_place_step_failure_restores_exact_pre_state(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            _write_layer(project, n, text)
        before = _snap(project)

        _fail_at_move("place", 1, OSError("simulated place failure"))
        reason = reorder_inaudit_layers(project, [3, 1, 2], operation_id="op-place")
        assert reason != ""
        assert _snap(project) == before

    @pytest.mark.parametrize("fail_at", range(1, 9))
    def test_every_boundary_restores_pre_state_for_five_layers(self, tmp_path, fail_at):
        project = _project(tmp_path)
        for n in range(1, 6):
            _write_layer(project, n, f"body-{n}")

        _count_moves()
        assert reorder_inaudit_layers(project, [5, 4, 3, 2, 1], operation_id="probe") == ""
        total_moves = inaudit._hook_counter[0]
        park_moves = total_moves // 2  # parks == places for a full rotation
        if fail_at > park_moves:
            return  # index beyond the last real park boundary

        # Reset to the pre-state (probe committed the [5,4,3,2,1] order).
        for n in range(1, 6):
            _write_layer(project, n, f"body-{n}")
        inaudit.set_inaudit_selected(project, None)
        before2 = _snap(project)

        _fail_at_move("park", fail_at, OSError("simulated failure"))
        reason = reorder_inaudit_layers(project, [5, 4, 3, 2, 1], operation_id=f"op-park-{fail_at}")
        assert reason != ""
        assert _snap(project) == before2

    @pytest.mark.parametrize("fail_at", range(1, 9))
    def test_every_place_boundary_restores_pre_state(self, tmp_path, fail_at):
        project = _project(tmp_path)
        for n in range(1, 6):
            _write_layer(project, n, f"body-{n}")
        before = _snap(project)

        _fail_at_move("place", fail_at, OSError("simulated place failure"))
        reason = reorder_inaudit_layers(project, [5, 4, 3, 2, 1], operation_id=f"op-place-{fail_at}")
        if reason == "":
            return  # fail_at beyond the place-step count: no injection happened
        assert _snap(project) == before

    def test_tracker_failure_keeps_reorder_truth_coherent(self, tmp_path):
        project = _project(tmp_path)
        _write_layer(project, 1, "a")
        _write_layer(project, 2, "user two")
        d = Path(project.source_path) / "audit"
        # Tracker has a marker for layer 2 so the reorder has something to move.
        inaudit._edit_user_layers(project, lambda nums: nums.append(2))
        registry = inaudit.user_layer_registry_path(project)
        assert registry.is_file()

        real_save = inaudit._save_user_layers
        state = {"failed_once": False}

        def _failing_save(path, numbers):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise OSError("simulated sidecar write failure")
            return real_save(path, numbers)

        with _swap(inaudit, "_save_user_layers", _failing_save):
            assert reorder_inaudit_layers(project, [2, 1], operation_id="op-track") == ""
        # Primary truth committed: bodies reordered exactly.
        assert (d / "1.md").read_text(encoding="utf-8") == "user two"
        assert (d / "2.md").read_text(encoding="utf-8") == "a"
        # Repair converges the tracker to the final mapping (1 <- 2).
        assert inaudit.replay_tracker_repairs(project) == ""
        numbers = json.loads(inaudit.user_layer_registry_path(project).read_text(encoding="utf-8"))["user_layers"]
        assert numbers == [1]

    def test_no_numeric_scratch_residue(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            _write_layer(project, n, text)
        reorder_inaudit_layers(project, [3, 1, 2], operation_id="op-clean")
        d = Path(project.source_path) / "audit"
        assert sorted(p.name for p in d.iterdir()) == ["1.md", "2.md", "3.md"]
        # Agent-inbox view: only canonical numbered layers are ever visible.
        assert [layer.number for layer in list_inaudit_layers(project)] == [1, 2, 3]

    def test_scratch_names_never_match_inaudit_re(self, tmp_path):
        from audapack.inaudit import INAUDIT_RE, _scratch_name

        assert not INAUDIT_RE.match(_scratch_name("op-1", 4))
        assert not INAUDIT_RE.match(_scratch_name("weird/op:id", 12))

    def test_selection_follows_physical_layer_identity(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            _write_layer(project, n, text)
        set_inaudit_selected(project, 2)
        assert reorder_inaudit_layers(project, [2, 3, 1], operation_id="op-sel") == ""
        # Layer 2 (body "two") moved to the front: canonical number 1.
        d = Path(project.source_path) / "audit"
        assert (d / "1.md").read_text(encoding="utf-8") == "two"
        assert get_inaudit_selected(project) == 1

    def test_stale_precondition_refused(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        _write_layer(project, 1, "a")
        _write_layer(project, 2, "b")
        # A foreign producer changes the layer set mid-gesture.
        def _boom(order):
            _write_layer(project, 9, "foreign")
            return order

        # Simulate: request order for a different set than what exists.
        reason = reorder_inaudit_layers(project, [1, 2, 9], operation_id="op-stale")
        assert reason != ""


class TestReorderRecovery:
    def test_journal_written_before_first_move(self, tmp_path):
        project = _project(tmp_path)
        _write_layer(project, 1, "one")
        _write_layer(project, 2, "two")

        captured = {}

        def _hook(stage, src, dst):
            journal_path = inaudit._reorder_journal_path(project, "op-j")
            captured["journal_exists"] = journal_path.exists()
            raise OSError("kill before first move")

        inaudit.reorder_move_hooks.append(_hook)
        reorder_inaudit_layers(project, [2, 1], operation_id="op-j")
        assert captured["journal_exists"] is True

    def test_crash_mid_park_recovers_to_exact_pre_or_final(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            _write_layer(project, n, text)

        _fail_at_move("park", 2, SystemExit(9))  # hard crash equivalent
        try:
            reorder_inaudit_layers(project, [3, 1, 2], operation_id="op-crash")
        except SystemExit:
            pass
        # Bounded recovery: exact pre-state (or already-exact final state).
        assert inaudit.recover_pending_reorders(project) == ""
        d = Path(project.source_path) / "audit"
        bodies = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.suffix == ".md"}
        assert bodies in (
            {"1.md": "one", "2.md": "two", "3.md": "three"},
            {"1.md": "three", "2.md": "one", "3.md": "two"},
        )
        # No scratch residue after recovery.
        assert not [p for p in d.iterdir() if p.name.startswith(".reorder-")]

    def test_recovery_is_idempotent(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        _write_layer(project, 1, "one")
        _write_layer(project, 2, "two")
        # Manually craft an interrupted journal (parked mid-transaction).
        inaudit._write_reorder_journal(
            project,
            inaudit._ReorderJournal(
                operation_id="op-idem",
                pre=inaudit._pre_state_fingerprint(project),
                final_order=[2, 1],
                moves=[{"src": 2, "scratch": inaudit._scratch_name("op-idem", 2), "dst": 1}],
                phase="parking",
                final_layers={},
            ),
        )
        d = Path(project.source_path) / "audit"
        (d / "2.md").rename(d / inaudit._scratch_name("op-idem", 2))
        assert inaudit.recover_pending_reorders(project) == ""
        # Recovery drives to exact PRE or exact FINAL (never a hybrid).
        bodies = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.suffix == ".md"}
        assert bodies in (
            {"1.md": "one", "2.md": "two"},
            {"1.md": "two", "2.md": "one"},
        )
        assert not [p for p in d.iterdir() if p.name.startswith(".reorder-")]
        # Second recovery run: nothing to do, no mutation.
        first = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.suffix == ".md"}
        assert inaudit.recover_pending_reorders(project) == ""
        assert {p.name: p.read_text(encoding="utf-8") for p in d.iterdir() if p.suffix == ".md"} == first

    def test_foreign_change_fails_closed_with_journal_preserved(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        _write_layer(project, 1, "one")
        _write_layer(project, 2, "two")
        pre = inaudit._pre_state_fingerprint(project)
        inaudit._write_reorder_journal(
            project,
            inaudit._ReorderJournal(
                operation_id="op-foreign",
                pre=pre,
                final_order=[2, 1],
                moves=[{"src": 2, "scratch": inaudit._scratch_name("op-foreign", 2), "dst": 1}],
                phase="parking",
                final_layers={},
            ),
        )
        # Foreign mutation: a NEW layer appears that pre-state never saw.
        _write_layer(project, 9, "foreign")
        reason = inaudit.recover_pending_reorders(project)
        assert reason != ""  # fail closed
        assert inaudit._read_reorder_journal(project, "op-foreign") is not None  # evidence preserved
        # Foreign bytes untouched.
        d = Path(project.source_path) / "audit"
        assert (d / "9.md").read_text(encoding="utf-8") == "foreign"
        assert (d / "1.md").read_text(encoding="utf-8") == "one"


def _journal_for(project: Project, operation_id: str, order: list[int]):
    """Write a pending PARK/PLACE journal exactly as reorder_inaudit_layers does."""
    pre = inaudit._pre_state_fingerprint(project)
    moves = []
    for at, number in enumerate(order):
        target = at + 1
        if number != target:
            moves.append(
                {
                    "src": number,
                    "scratch": inaudit._scratch_name(operation_id, number),
                    "dst": target,
                }
            )
    final_layers = {str(at + 1): pre["layers"][str(n)] for at, n in enumerate(order)}
    inaudit._write_reorder_journal(
        project,
        inaudit._ReorderJournal(
            operation_id=operation_id,
            pre=pre,
            final_order=list(order),
            moves=moves,
            phase="parking",
            final_layers=final_layers,
        ),
    )
    return pre, moves


class TestReorderForeignSameNumberRecovery:
    def test_foreign_same_number_target_fails_closed(self, tmp_path):
        """Exact reproduction: 1=A, 2 parked to scratch=B, then 2.md=FOREIGN."""
        project = _project(tmp_path)
        _write_layer(project, 1, "A")
        _write_layer(project, 2, "B")
        _journal_for(project, "op-foreign", [2, 1])
        d = Path(project.source_path) / "audit"
        scratch2 = inaudit._scratch_name("op-foreign", 2)
        (d / "2.md").rename(d / scratch2)  # park B
        (d / "2.md").write_text("FOREIGN", encoding="utf-8")  # foreign same-number

        reason = inaudit.recover_pending_reorders(project)
        assert reason != ""
        assert (d / "1.md").read_text(encoding="utf-8") == "A"
        assert (d / "2.md").read_text(encoding="utf-8") == "FOREIGN"
        assert (d / scratch2).read_text(encoding="utf-8") == "B"
        assert inaudit._read_reorder_journal(project, "op-foreign") is not None
        assert not [p for p in d.iterdir() if "recover" in p.name]

    def test_foreign_bytes_at_target_number_fail_closed(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        _journal_for(project, "op-tgt", [2, 3, 1])
        d = Path(project.source_path) / "audit"
        (d / "2.md").rename(d / inaudit._scratch_name("op-tgt", 2))  # park B
        (d / "1.md").write_text("FOREIGN", encoding="utf-8")  # 1 is a PLACE target
        reason = inaudit.recover_pending_reorders(project)
        assert reason != ""
        assert (d / "1.md").read_text(encoding="utf-8") == "FOREIGN"
        assert (d / "3.md").read_text(encoding="utf-8") == "C"

    def test_foreign_bytes_at_untouched_source_fail_closed(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        _journal_for(project, "op-src", [3, 1, 2])
        d = Path(project.source_path) / "audit"
        (d / "2.md").write_text("FOREIGN", encoding="utf-8")  # untouched source
        reason = inaudit.recover_pending_reorders(project)
        assert reason != ""
        assert (d / "1.md").read_text(encoding="utf-8") == "A"
        assert (d / "2.md").read_text(encoding="utf-8") == "FOREIGN"
        assert (d / "3.md").read_text(encoding="utf-8") == "C"

    def test_mid_park_state_recovers_to_final(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        _journal_for(project, "op-park", [3, 1, 2])
        d = Path(project.source_path) / "audit"
        (d / "3.md").rename(d / inaudit._scratch_name("op-park", 3))
        (d / "1.md").rename(d / inaudit._scratch_name("op-park", 1))
        assert inaudit.recover_pending_reorders(project) == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "C"
        assert (d / "2.md").read_text(encoding="utf-8") == "A"
        assert (d / "3.md").read_text(encoding="utf-8") == "B"
        assert not [p for p in d.iterdir() if p.name.startswith(".reorder-")]

    def test_mid_place_state_recovers_to_final(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        _journal_for(project, "op-place", [3, 1, 2])
        d = Path(project.source_path) / "audit"
        (d / "3.md").rename(d / inaudit._scratch_name("op-place", 3))
        (d / "1.md").rename(d / inaudit._scratch_name("op-place", 1))
        (d / "2.md").rename(d / inaudit._scratch_name("op-place", 2))
        (d / inaudit._scratch_name("op-place", 3)).rename(d / "1.md")  # place C
        (d / inaudit._scratch_name("op-place", 1)).rename(d / "2.md")  # place A
        assert inaudit.recover_pending_reorders(project) == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "C"
        assert (d / "2.md").read_text(encoding="utf-8") == "A"
        assert (d / "3.md").read_text(encoding="utf-8") == "B"

    def test_interrupted_recovery_preserves_evidence_and_converges(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        _journal_for(project, "op-crash", [3, 1, 2])
        d = Path(project.source_path) / "audit"
        (d / "3.md").rename(d / inaudit._scratch_name("op-crash", 3))
        (d / "1.md").rename(d / inaudit._scratch_name("op-crash", 1))

        calls = {"n": 0}

        def _boom(stage, target):
            if stage == "place":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("injected recovery failure")

        inaudit.reorder_recovery_hooks.append(_boom)
        reason = inaudit.recover_pending_reorders(project)
        assert reason != ""
        # Evidence preserved: journal, scratch and staged recovery bodies.
        assert inaudit._read_reorder_journal(project, "op-crash") is not None
        assert (d / inaudit._scratch_name("op-crash", 3)).is_file()
        assert (d / inaudit._scratch_name("op-crash", 1)).is_file()
        assert (d / inaudit._recovery_scratch_name("op-crash", 1)).is_file()

        inaudit.reorder_recovery_hooks.clear()
        assert inaudit.recover_pending_reorders(project) == ""
        assert (d / "1.md").read_text(encoding="utf-8") == "C"
        assert (d / "2.md").read_text(encoding="utf-8") == "A"
        assert (d / "3.md").read_text(encoding="utf-8") == "B"
        assert not [p for p in d.iterdir() if p.name.startswith(".reorder-")]

    def test_recovery_reconciles_selection_and_tracker(self, tmp_path):
        project = _project(tmp_path)
        for n, text in ((1, "A"), (2, "B"), (3, "C")):
            _write_layer(project, n, text)
        inaudit._edit_user_layers(project, lambda nums: nums.append(3))
        set_inaudit_selected(project, 3)
        _journal_for(project, "op-meta", [3, 1, 2])
        d = Path(project.source_path) / "audit"
        (d / "3.md").rename(d / inaudit._scratch_name("op-meta", 3))
        assert inaudit.recover_pending_reorders(project) == ""
        # Canonical final committed; metadata follows the same permutation.
        assert (d / "1.md").read_text(encoding="utf-8") == "C"
        assert get_inaudit_selected(project) == 1
        numbers = json.loads(
            inaudit.user_layer_registry_path(project).read_text(encoding="utf-8")
        )["user_layers"]
        assert numbers == [1]
