"""CORE-001 / W2-003 consumer side: an epoch reset is adopted even when lower.

The GUI previously consumed an audit/dispatch generation only when the numeric
counter advanced, so a recovered stream that restarted below the last observed
value was silently ignored. The epoch is the explicit reset identity that tells
the consumer to resync once and adopt the recovered baseline.
"""
from __future__ import annotations

from audapack.bridge.state import GenerationStateCorruptionError
from audapack.ui_qt.main_window import MainWindow


class ConsumerStub:
    """The smallest object _consume_bridge_generations needs, bound unbound.

    Exercising the production method against a stub keeps the test on shipped
    code without paying the cost of a full MainWindow.
    """

    _consume_bridge_generations = MainWindow._consume_bridge_generations

    def __init__(self, tmp_path, info):
        self.info = info
        self.targeted: list[str] = []
        self._last_audit_generation = 0
        self._last_dispatch_generation = 0
        self._last_audit_epoch = ""
        self._last_dispatch_epoch = ""
        self._dispatch_generation_path = tmp_path / "dispatch_generation.json"
        self.task_runner = self

    def submit_coalesced(self, key, work, on_success=None, on_error=None):
        self.targeted.append(key)

    def _generation_watch_paths(self):
        pass


def test_consumer_adopts_lower_recovered_generation_on_epoch_change(tmp_path, monkeypatch):
    import audapack.ui_qt.main_window as module

    holder = ConsumerStub(tmp_path, {"generation": 2, "epoch": "new", "project_id": ""})
    holder._last_audit_generation = 50
    holder._last_audit_epoch = "old"

    monkeypatch.setattr(module, "get_generation_info", lambda: holder.info)

    assert holder._consume_bridge_generations() is True
    assert holder._last_audit_generation == 2
    assert holder._last_audit_epoch == "new"

    # Same epoch, no advance: no second refresh.
    assert holder._consume_bridge_generations() is False


def test_consumer_ignores_same_epoch_lower_counter(tmp_path, monkeypatch):
    import audapack.ui_qt.main_window as module

    holder = ConsumerStub(tmp_path, {"generation": 3, "epoch": "e", "project_id": ""})
    holder._last_audit_generation = 10
    holder._last_audit_epoch = "e"
    monkeypatch.setattr(module, "get_generation_info", lambda: holder.info)

    assert holder._consume_bridge_generations() is False


def test_consumer_resyncs_on_corruption_then_settles(tmp_path, monkeypatch):
    import audapack.ui_qt.main_window as module

    def _raise():
        raise GenerationStateCorruptionError("boom")

    holder = ConsumerStub(tmp_path, None)
    holder._last_audit_generation = 50
    monkeypatch.setattr(module, "get_generation_info", _raise)

    assert holder._consume_bridge_generations() is True
    assert holder._consume_bridge_generations() is False, "corruption must not refresh forever"
