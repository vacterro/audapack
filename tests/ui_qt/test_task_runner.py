"""Unit tests for TaskRunner (Wave M).

Verifies:
- Background execution without blocking caller thread.
- Result delivery via Qt signals / callbacks.
- Task deduplication and coalescing (dirty re-run pattern).
- Task keys and isolation.
"""

import time
import pytest
from PySide6.QtCore import QCoreApplication

from audapack.ui_qt.task_runner import TaskRunner


def test_task_runner_basic_execution(qapp):
    runner = TaskRunner(max_threads=2)

    results = []
    def _worker():
        return 42

    def _on_success(val):
        results.append(val)

    runner.submit("calc:simple", _worker, on_success=_on_success)

    # Process Qt event loop until finished
    start = time.time()
    while not results and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    assert results == [42]


def test_task_runner_coalescing_event_storm(qapp):
    runner = TaskRunner(max_threads=2)

    execution_counter = [0]
    results = []

    def _heavy_task():
        time.sleep(0.05)
        execution_counter[0] += 1
        return execution_counter[0]

    def _on_success(val):
        results.append(val)

    # Fire 5 rapid coalesced requests for the same key
    for _ in range(5):
        runner.submit_coalesced("audit:fastprompter", _heavy_task, on_success=_on_success)

    # Wait for completion of running + trailing run
    start = time.time()
    while len(results) < 2 and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    # Invariant: Instead of 5 runs, exactly 2 runs executed (the initial one + 1 dirty trailing run with latest state)
    assert execution_counter[0] == 2
    assert len(results) == 2


def test_task_runner_error_handling(qapp):
    runner = TaskRunner(max_threads=2)

    errors = []
    def _failing_task():
        raise ValueError("Simulated worker error")

    def _on_error(err):
        errors.append(err)

    runner.submit("task:fail", _failing_task, on_error=_on_error)

    start = time.time()
    while not errors and time.time() - start < 3.0:
        qapp.processEvents()
        time.sleep(0.01)

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "Simulated worker error" in str(errors[0])
