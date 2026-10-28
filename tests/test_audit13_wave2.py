"""Red-to-green regression controls for audit/13 Wave 2: W2-001, W2-002, W2-003."""

from __future__ import annotations

import datetime
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from audapack.config import AppConfig
from audapack.handoff_drop import materialize
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_audit import AuditProgress, AuditProgressState
from audapack.prepared_worker import PreparedWorker

# ==============================================================================
# W2-001: Stage-resumable specialized Prepared recovery
# ==============================================================================


class TestW2001ResumableSpecializedRecovery:
    def test_audit_preparing_absent_resumes_start(self, tmp_path: Path) -> None:
        """PREPARING + ABSENT must resume via start() rather than deadlocking as ACTIVE."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-audit-1",
            name="Audit 1",
            project_id="proj1",
            launcher_id="l1",
            account_id="acc1",
            trigger=Trigger.ON_TIME,
            payload=Payload.AUDIT,
            payload_config={"account_policy": "ACCOUNT_AGNOSTIC_EXPLICIT"},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        # Create an expired PREPARING execution
        execution_id = store.claim("p-audit-1", "evt-1", "owner-1", now)
        receipt = store.receipt("p-audit-1", "evt-1")
        assert receipt is not None
        # Advance CLAIMED -> PREPARING
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.PREPARING, now=now)

        # Set lease to expired
        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        # Setup worker
        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        # Mock audit runtime where poll() returns ABSENT
        mock_audit_runtime = MagicMock()
        mock_audit_runtime.poll.return_value = AuditProgress(state=AuditProgressState.ABSENT)
        # Mock start() returning ok
        start_result = MagicMock(ok=True, duplicate=False, intent_id="intent-1", dispatch_id="disp-1")
        mock_audit_runtime.start.return_value = start_result
        worker.audit_runtime = mock_audit_runtime

        # Run tick
        worker.tick()

        # The worker should have called start() to resume the un-started audit
        assert mock_audit_runtime.start.call_count == 1, (
            "AUDIT PREPARING + ABSENT must resume through start()"
        )
        updated_receipt = store.receipt("p-audit-1", "evt-1")
        assert updated_receipt["state"] == JobState.DELIVERING.value

    def test_sync_preparing_pending_resumes_prepare(self, tmp_path: Path) -> None:
        """SYNC PREPARING + PENDING must resume at prepare() without requiring CLAIMED."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-sync-1",
            name="Sync 1",
            project_id="proj1",
            launcher_id="l1",
            account_id="acc1",
            trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT,
            payload_config={},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        execution_id = store.claim("p-sync-1", "evt-sync-1", "owner-1", now)
        receipt = store.receipt("p-sync-1", "evt-sync-1")
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.PREPARING, now=now)

        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        mock_sync_store = MagicMock()
        mock_sync_store.get_by_event.return_value = {
            "sync_group_id": "sg-1",
            "state": "PENDING",
            "process_skew_ms": None,
        }
        mock_sync_coord = MagicMock()
        mock_sync_coord.prepare.return_value = True
        mock_sync_coord.release.return_value = {"state": "LAUNCHED", "process_skew_ms": 10}

        worker.sync_store = mock_sync_store
        worker.sync_coordinator = mock_sync_coord

        worker.tick()

        assert mock_sync_coord.prepare.call_count == 1, (
            "SYNC PREPARING + PENDING must resume at prepare() instead of failing CLAIMED -> PREPARING"
        )

    def test_prime_preparing_pending_resumes_execute(self, tmp_path: Path) -> None:
        """PRIME PREPARING + PENDING must advance to LAUNCHING and resume execute()."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-prime-1",
            name="Prime 1",
            project_id="proj1",
            launcher_id="l1",
            account_id="claude:p1",
            trigger=Trigger.PRIME,
            payload=Payload.STATIC_PROMPT,
            payload_config={},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        execution_id = store.claim("p-prime-1", "evt-prime-1", "owner-1", now)
        receipt = store.receipt("p-prime-1", "evt-prime-1")
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.PREPARING, now=now)

        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        from audapack.account_registry import AccountIdentity
        acc = AccountIdentity(account_id="claude:p1", provider_id="claude", display_name="Claude 1",
                              profile_locator="", launcher_ids=("l1",), discovery_source="",
                              last_seen_at="", enabled=True)
        worker._known_accounts = lambda: {"claude:p1": acc}

        mock_prime_store = MagicMock()
        mock_prime_store.get_by_event.return_value = {
            "prime_id": "prime-1",
            "state": "PENDING",
        }
        mock_prime_coord = MagicMock()
        mock_prime_coord.execute.return_value = "DONE"

        worker.prime_store = mock_prime_store
        worker.prime_coordinator = mock_prime_coord

        worker.tick()

        assert mock_prime_coord.execute.call_count == 1, (
            "PRIME PREPARING + PENDING must resume at execute()"
        )

    def test_audit_with_existing_dispatch_adopts_without_second_start(self, tmp_path: Path) -> None:
        """AUDIT with existing dispatch must adopt/poll and never execute a second start()."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-audit-exist",
            name="Audit Existing",
            project_id="proj1",
            launcher_id="l1",
            account_id="acc1",
            trigger=Trigger.ON_TIME,
            payload=Payload.AUDIT,
            payload_config={"account_policy": "ACCOUNT_AGNOSTIC_EXPLICIT"},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        execution_id = store.claim("p-audit-exist", "evt-exist-1", "owner-1", now)
        receipt = store.receipt("p-audit-exist", "evt-exist-1")
        assert receipt is not None
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.DELIVERING, now=now)

        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        mock_audit_runtime = MagicMock()
        mock_audit_runtime.poll.return_value = AuditProgress(
            state=AuditProgressState.RUNNING, dispatch_id="disp-exist-1", detail="in flight"
        )
        worker.audit_runtime = mock_audit_runtime

        worker.tick()
        assert mock_audit_runtime.start.call_count == 0, "Must not call start() when intent/dispatch exists"
        assert mock_audit_runtime.poll.call_count == 1

        # Expire lease again and run recovery second time: verify no second start
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))
        worker.tick()
        assert mock_audit_runtime.start.call_count == 0, "Second recovery pass must not call start()"
        assert mock_audit_runtime.poll.call_count == 2

    def test_sync_releasing_fails_closed_to_recovery_required_no_second_release(self, tmp_path: Path) -> None:
        """SYNC RELEASING must fail closed into recovery-required and never execute a second release()."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-sync-rel",
            name="Sync Releasing",
            project_id="proj1",
            launcher_id="l1",
            account_id="acc1",
            trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT,
            payload_config={},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        execution_id = store.claim("p-sync-rel", "evt-sync-rel", "owner-1", now)
        receipt = store.receipt("p-sync-rel", "evt-sync-rel")
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.LAUNCHING, now=now)

        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        mock_sync_store = MagicMock()
        mock_sync_store.get_by_event.return_value = {
            "sync_group_id": "sg-rel",
            "state": "RELEASING",
        }
        mock_sync_coord = MagicMock()
        worker.sync_store = mock_sync_store
        worker.sync_coordinator = mock_sync_coord

        worker.tick()
        assert mock_sync_coord.release.call_count == 0, "RELEASING must never call release()"
        assert mock_sync_coord.prepare.call_count == 0, "RELEASING must never call prepare()"
        assert mock_sync_store.recover_interrupted_release.call_count == 1

        rec = store.receipt("p-sync-rel", "evt-sync-rel")
        assert rec["state"] == JobState.RECOVERY_REQUIRED.value

        # Second recovery pass
        worker.tick()
        assert mock_sync_coord.release.call_count == 0
        assert mock_sync_coord.prepare.call_count == 0

    def test_prime_submitting_fails_closed_to_recovery_required_no_second_execute(self, tmp_path: Path) -> None:
        """PRIME SUBMITTING must fail closed into recovery-required and never execute a second execute()."""
        db_path = tmp_path / "prepared.db"
        store = PreparedStore(db_path)
        job = PreparedJob(
            prepared_id="p-prime-sub",
            name="Prime Submitting",
            project_id="proj1",
            launcher_id="l1",
            account_id="claude:p1",
            trigger=Trigger.PRIME,
            payload=Payload.STATIC_PROMPT,
            payload_config={},
            trigger_config={},
            model="default",
            effort="medium",
            enabled=True,
        )
        store.save(job)
        now = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.timezone.utc)

        execution_id = store.claim("p-prime-sub", "evt-prime-sub", "owner-1", now)
        receipt = store.receipt("p-prime-sub", "evt-prime-sub")
        store.advance(execution_id, "owner-1", receipt["claim_generation"],
                      JobState.CLAIMED, JobState.LAUNCHING, now=now)

        expired_time = (now - datetime.timedelta(seconds=60)).isoformat()
        with store._connect() as con:
            con.execute("UPDATE prepared_executions SET lease_expires_at = ? WHERE execution_id = ?",
                        (expired_time, execution_id))

        cfg = AppConfig()
        worker = PreparedWorker(cfg, clock=lambda: now)
        worker.jobs = store

        mock_prime_store = MagicMock()
        mock_prime_store.get_by_event.return_value = {
            "prime_id": "prime-sub-1",
            "state": "SUBMITTING",
        }
        mock_prime_coord = MagicMock()
        worker.prime_store = mock_prime_store
        worker.prime_coordinator = mock_prime_coord

        worker.tick()
        assert mock_prime_coord.execute.call_count == 0, "SUBMITTING must never call execute()"
        assert mock_prime_store.recover_interrupted.call_count == 1

        rec = store.receipt("p-prime-sub", "evt-prime-sub")
        assert rec["state"] == JobState.RECOVERY_REQUIRED.value

        # Second recovery pass
        worker.tick()
        assert mock_prime_coord.execute.call_count == 0


# ==============================================================================
# W2-002: PreparedWorker restart lifecycle executor recreation
# ==============================================================================


class TestW2002WorkerRestartLifecycle:
    def test_worker_stop_start_recreates_executors_and_allows_submit(self) -> None:
        """Stopping and restarting PreparedWorker must recreate pools so submissions succeed."""
        cfg = AppConfig()
        worker = PreparedWorker(cfg)
        worker.start()
        assert worker.lifecycle == "OPEN"

        # Stop worker
        stopped = worker.stop(timeout=2.0)
        assert stopped is True
        assert worker.lifecycle == "CLOSED"

        # Restart worker
        worker.start()
        assert worker.lifecycle == "OPEN"

        # Submitting to _pool should succeed without RuntimeError
        fut = worker._submit(worker._pool, lambda: 123)
        assert fut is not None
        assert fut.result(timeout=2.0) == 123

        # Submitting to _probe_pool should succeed
        fut_probe = worker._submit(worker._probe_pool, lambda: 456)
        assert fut_probe is not None
        assert fut_probe.result(timeout=2.0) == 456

        # Clean shutdown
        worker.stop(timeout=2.0)

    def test_restart_pools_are_distinct_instances(self) -> None:
        """New pools after worker restart must be distinct instances from the previous generation."""
        cfg = AppConfig()
        worker = PreparedWorker(cfg)
        worker.start()
        pool1 = worker._pool
        probe_pool1 = worker._probe_pool
        assert worker.stop(timeout=2.0) is True

        worker.start()
        pool2 = worker._pool
        probe_pool2 = worker._probe_pool
        assert pool1 is not pool2, "Recreated launch pool must be a distinct instance"
        assert probe_pool1 is not probe_pool2, "Recreated probe pool must be a distinct instance"

        # Both can submit and complete work
        f1 = worker._submit(worker._pool, lambda: "ok1")
        f2 = worker._submit(worker._probe_pool, lambda: "ok2")
        assert f1.result(timeout=2.0) == "ok1"
        assert f2.result(timeout=2.0) == "ok2"
        worker.stop(timeout=2.0)

    def test_repeated_stop_start_cycles_stay_bounded(self) -> None:
        """Repeated stop/start cycles must succeed cleanly and keep inflight bounded at 0."""
        cfg = AppConfig()
        worker = PreparedWorker(cfg)
        for i in range(5):
            worker.start()
            assert worker.lifecycle == "OPEN"
            f = worker._submit(worker._pool, lambda x=i: x * 2)
            assert f.result(timeout=2.0) == i * 2
            stopped = worker.stop(timeout=2.0)
            assert stopped is True
            assert worker.lifecycle == "CLOSED"
            assert len(worker._inflight) == 0

    def test_start_while_closing_refuses(self) -> None:
        """start() while lifecycle is CLOSING must refuse to construct new pools or open admission."""
        cfg = AppConfig()
        worker = PreparedWorker(cfg)
        worker.start()
        with worker._admission:
            worker._lifecycle = "CLOSING"

        # start() should refuse
        worker.start()
        assert worker.lifecycle == "CLOSING"
        # Force finish stop for cleanup
        worker._lifecycle = "CLOSED"
        worker.stop(timeout=2.0)

    def test_stale_old_generation_futures_not_owned_by_new_generation(self) -> None:
        """Stale futures from prior generation must not become owned by the new generation."""
        cfg = AppConfig()
        worker = PreparedWorker(cfg)
        worker.start()
        f_old = worker._submit(worker._pool, lambda: 999)
        assert f_old.result(timeout=2.0) == 999
        assert worker.stop(timeout=2.0) is True

        # Now start new generation
        worker.start()
        assert f_old not in worker._inflight
        assert len(worker._inflight) == 0
        worker.stop(timeout=2.0)


# ==============================================================================
# W2-003: Handoff materialization transaction & byte contract
# ==============================================================================


class TestW2003HandoffMaterialization:
    def test_crlf_bytes_preserved_and_verified(self, tmp_path: Path) -> None:
        """CRLF text must be written byte-exact without normalization mismatch error."""
        crlf_text = "ProTrail\r\n\r\nSAIHANDOFF — BRIEF\r\n\r\nMISSION\r\n\r\nCRLF body line\r\n"
        path, reused = materialize(crlf_text, "ProTrail", tmp_path)
        assert not reused
        assert path.read_bytes() == crlf_text.encode("utf-8")

    def test_concurrent_same_minute_different_content_never_collides(self, tmp_path: Path) -> None:
        """Two concurrent materialize calls for same label & minute must both succeed with distinct files."""
        when = datetime.datetime(2026, 10, 7, 10, 30)
        text1 = "ProTrail\n\nSAIHANDOFF — BRIEF ONE\n\nMISSION\n\nBody 1\n"
        text2 = "ProTrail\n\nSAIHANDOFF — BRIEF TWO\n\nMISSION\n\nBody 2\n"

        pool = ThreadPoolExecutor(max_workers=2)
        fut1 = pool.submit(materialize, text1, "ProTrail", tmp_path, when)
        fut2 = pool.submit(materialize, text2, "ProTrail", tmp_path, when)
        path1, reused1 = fut1.result(timeout=5.0)
        path2, reused2 = fut2.result(timeout=5.0)
        pool.shutdown()

        assert not reused1
        assert not reused2
        assert path1 != path2
        assert path1.exists() and path2.exists()
        assert path1.read_bytes() == text1.encode("utf-8")
        assert path2.read_bytes() == text2.encode("utf-8")

    def test_publication_race_with_external_writer_never_clobbers_foreign_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Destination created by external writer before publication must not be clobbered."""
        when = datetime.datetime(2026, 10, 7, 10, 30)
        label = "ProTrail"
        candidate_path = tmp_path / "ProTrail_20261007_1030.md"
        text = "ProTrail\n\nSAIHANDOFF — BRIEF\n\nMISSION\n\nBody content\n"

        foreign_bytes = b"EXTERNAL-WINNER"
        injected = False

        orig_replace = Path.replace

        def hook_replace(self_path, target_path):
            nonlocal injected
            if not injected and Path(target_path).name == candidate_path.name:
                injected = True
                Path(target_path).write_bytes(foreign_bytes)
            return orig_replace(self_path, target_path)

        orig_os_open = os.open

        def hook_os_open(file_path, flags, *args, **kwargs):
            nonlocal injected
            if not injected and Path(file_path).name == candidate_path.name:
                injected = True
                Path(file_path).write_bytes(foreign_bytes)
            return orig_os_open(file_path, flags, *args, **kwargs)

        monkeypatch.setattr(Path, "replace", hook_replace)
        monkeypatch.setattr("os.open", hook_os_open)

        path, reused = materialize(text, label, tmp_path, when)

        assert not reused
        assert path != candidate_path, "Publication must not use candidate path when foreign file won"
        assert path == tmp_path / "ProTrail_20261007_1030_2.md"
        assert candidate_path.exists(), "Foreign candidate file must still exist"
        assert candidate_path.read_bytes() == foreign_bytes, "Foreign bytes must be completely untouched"
        assert path.exists(), "Handoff file must exist at suffix path"
        assert path.read_bytes() == text.encode("utf-8"), "Handoff file must contain exact text bytes"
