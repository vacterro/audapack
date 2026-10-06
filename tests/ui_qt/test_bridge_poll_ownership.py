"""PERF-004 (audit/9.md): one composite refresh per bridge timer emission.

``bridge_timer.timeout`` used to be connected to BOTH ``_on_check_bridge_
generation`` and ``_refresh_audit_runs_async``, and the generation check calls
that refresh itself. ``TaskRunner.submit_coalesced`` does not drop the second
submission -- it records dirty work and runs a whole extra pass afterwards -- so
one tick that saw a generation advance cost two full ``runtime_status()`` +
``browser_jobs()`` + journal-aggregation passes.

These tests assert OWNERSHIP, not UI-equivalent output: a future refactor that
hides the duplicate work behind an identical-looking panel still goes red.
"""

from __future__ import annotations

import json

import pytest

from audapack.ui_qt.main_window import MainWindow


class PollStub:
    """The smallest object the three real methods need.

    The methods under test are called unbound against this stub, so what is
    exercised is the production code, not a re-implementation of it.
    """

    def __init__(self, tmp_path, *, audit_gen=0, dispatch_gen=0):
        self.refreshes = 0
        self.targeted: list[str] = []
        self.watch_calls = 0
        self._last_audit_generation = 0
        self._last_dispatch_generation = 0
        self._audit_gen = audit_gen
        self._dispatch_gen = dispatch_gen
        self._dispatch_generation_path = tmp_path / "dispatch_generation.json"
        self._dispatch_generation_path.write_text(
            json.dumps({"generation": dispatch_gen}), encoding="utf-8"
        )
        self.task_runner = self

    # --- collaborators -------------------------------------------------
    def _refresh_audit_runs_async(self):
        self.refreshes += 1

    def submit_coalesced(self, key, work, on_success=None, on_error=None):
        self.targeted.append(key)

    def _generation_watch_paths(self):
        self.watch_calls += 1

    # --- production methods, bound to this stub ------------------------
    # Adopted unbound so the tests exercise the shipped code, never a copy.
    _consume_bridge_generations = MainWindow._consume_bridge_generations
    _on_bridge_poll_tick = MainWindow._on_bridge_poll_tick
    _on_check_bridge_generation = MainWindow._on_check_bridge_generation

    def consume(self):
        return self._consume_bridge_generations()

    def tick(self):
        return self._on_bridge_poll_tick()

    def event(self):
        return self._on_check_bridge_generation()


@pytest.fixture
def stub(tmp_path, monkeypatch):
    import audapack.ui_qt.main_window as module

    holder = PollStub(tmp_path)

    def fake_generation_info():
        return {"generation": holder._audit_gen, "project_id": "p1"}

    monkeypatch.setattr(module, "get_generation_info", fake_generation_info)
    return holder


def test_one_connection_owns_the_bridge_timer():
    """The wiring itself is the defect: two slots, one signal, same work."""
    import inspect

    source = inspect.getsource(MainWindow.__init__)
    connects = [
        line.strip() for line in source.splitlines()
        if "bridge_timer.timeout.connect" in line
    ]
    assert connects == ["self.bridge_timer.timeout.connect(self._on_bridge_poll_tick)"], (
        f"bridge_timer must have exactly one owner, found: {connects}"
    )


def test_an_unchanged_tick_still_refreshes_exactly_once(stub):
    """Browser liveness changes without touching any generation file."""
    stub.tick()
    assert stub.refreshes == 1
    assert stub.targeted == []


def test_an_audit_generation_advance_costs_exactly_one_composite_refresh(stub):
    stub._audit_gen = 7
    stub.tick()
    assert stub.refreshes == 1, "a generation-changing tick must not refresh twice"
    assert stub.targeted == ["audit:p1"], "the targeted project refresh must survive"
    assert stub._last_audit_generation == 7


def test_a_dispatch_generation_advance_costs_exactly_one_composite_refresh(stub):
    stub._dispatch_generation_path.write_text(json.dumps({"generation": 4}), encoding="utf-8")
    stub.tick()
    assert stub.refreshes == 1
    assert stub._last_dispatch_generation == 4


def test_both_generations_advancing_still_cost_one_refresh(stub):
    stub._audit_gen = 3
    stub._dispatch_generation_path.write_text(json.dumps({"generation": 9}), encoding="utf-8")
    stub.tick()
    assert stub.refreshes == 1
    assert stub.targeted == ["audit:p1"]


def test_repeated_ticks_refresh_once_each(stub):
    for _ in range(5):
        stub.tick()
    assert stub.refreshes == 5, "each emission owns exactly one refresh, no more, no fewer"


def test_the_debounced_filesystem_event_refreshes_only_on_a_real_advance(stub):
    """Event-driven prompt refresh must survive the ownership change."""
    stub.event()
    assert stub.refreshes == 0, "an event with no generation advance must not refresh"

    stub._audit_gen = 2
    stub.event()
    assert stub.refreshes == 1
    assert stub.targeted == ["audit:p1"]

    # Same generation again: nothing new to publish.
    stub.event()
    assert stub.refreshes == 1


def test_consume_reports_the_advance_without_performing_the_refresh(stub):
    """The two questions are separated: "did it change" vs "who refreshes"."""
    assert stub.consume() is False
    assert stub.refreshes == 0

    stub._audit_gen = 5
    assert stub.consume() is True
    assert stub.refreshes == 0, "consuming a generation must not schedule the composite pass"
    assert stub.watch_calls == 2, "watcher paths are restored after every read"


def test_a_malformed_dispatch_generation_file_is_not_an_advance(stub):
    stub._dispatch_generation_path.write_text("{not json", encoding="utf-8")
    stub.tick()
    assert stub.refreshes == 1
    assert stub._last_dispatch_generation == 0


def test_the_adaptive_poll_interval_contract_is_unchanged():
    import inspect

    source = inspect.getsource(MainWindow.__init__)
    assert "self.BRIDGE_POLL_ACTIVE_MS = 4000" in source
    assert "self.BRIDGE_POLL_IDLE_MS = 30000" in source
    assert "self.bridge_timer.setInterval(self.BRIDGE_POLL_IDLE_MS)" in source
