"""Red-to-green regression controls for audit/13 Wave 3: PERF-001, PERF-002, PERF-003."""

from __future__ import annotations

import datetime
import http.client
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from audapack import prepared
from audapack.bridge.server import AudapackBridgeHandler
from audapack.config import AppConfig, BridgeConfig
from audapack.prepared import (
    Payload,
    PreparedJob,
    PreparedStore,
    PreparedSyncMember,
    Trigger,
)
from audapack.prepared_sync import SyncCoordinator, SyncMemberSpec, SyncStore
from audapack.services.audit_run_service import AuditStartIntentStore

MAX_SYNC_MEMBERS = getattr(prepared, "MAX_SYNC_MEMBERS", 32)


# ==============================================================================
# PERF-001: SYNC member count bounded by MAX_SYNC_MEMBERS
# ==============================================================================


class TestPerf001SyncMemberBounds:
    def test_save_sync_members_rejects_exceeding_max(self, tmp_path: Path) -> None:
        """PreparedStore.save_sync_members must raise ValueError when members > MAX_SYNC_MEMBERS."""
        store = PreparedStore(tmp_path / "prepared.db")
        job = PreparedJob(
            prepared_id="p-sync-bound",
            name="Sync Bound",
            project_id="proj1",
            launcher_id="",
            account_id="AUTO",
            trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT,
            payload_config={},
            trigger_config={},
            enabled=True,
        )
        store.save(job)

        oversized_members = [
            PreparedSyncMember("p-sync-bound", i, f"acc_{i}", f"l_{i}")
            for i in range(MAX_SYNC_MEMBERS + 1)
        ]

        with pytest.raises(ValueError, match="exceed"):
            store.save_sync_members("p-sync-bound", oversized_members)

    def test_sync_store_create_rejects_exceeding_max(self, tmp_path: Path) -> None:
        """SyncStore.create must raise ValueError when members > MAX_SYNC_MEMBERS."""
        store = SyncStore(tmp_path / "prepared.db")
        now = datetime.datetime.now(datetime.timezone.utc)
        oversized_specs = [
            SyncMemberSpec(
                account_id=f"acc_{i}",
                launcher_id=f"l_{i}",
                provider_id="claude",
                model="default",
                effort="medium",
                payload_sha256="a" * 64,
            )
            for i in range(MAX_SYNC_MEMBERS + 1)
        ]

        with pytest.raises(ValueError, match="exceed"):
            store.create("p-1", "evt-1", "five_hour", "ALL_READY", oversized_specs, now)

    def test_sync_member_exact_boundary_acceptance_and_rejection(self, tmp_path: Path) -> None:
        """2 and MAX_SYNC_MEMBERS must be accepted; MAX_SYNC_MEMBERS + 1 must be rejected."""
        prep_store = PreparedStore(tmp_path / "prepared.db")
        sync_store = SyncStore(tmp_path / "prepared.db")
        now = datetime.datetime.now(datetime.timezone.utc)

        # Boundary: 2 members -> accepted
        job2 = PreparedJob(
            prepared_id="p-bound-2", name="Bound 2", project_id="proj1",
            launcher_id="", account_id="AUTO", trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT, payload_config={}, trigger_config={}, enabled=True,
        )
        prep_store.save(job2)
        members_2 = [PreparedSyncMember("p-bound-2", i, f"acc_{i}", f"l_{i}") for i in range(2)]
        prep_store.save_sync_members("p-bound-2", members_2)
        assert len(prep_store.get_sync_members("p-bound-2")) == 2

        specs_2 = [
            SyncMemberSpec(f"acc_{i}", f"l_{i}", "claude", "default", "medium", "0" * 64)
            for i in range(2)
        ]
        gid_2 = sync_store.create("p-bound-2", "evt-2", "five_hour", "ALL_READY", specs_2, now)
        assert gid_2 is not None

        # Boundary: MAX_SYNC_MEMBERS -> accepted
        job_max = PreparedJob(
            prepared_id="p-bound-max", name="Bound Max", project_id="proj1",
            launcher_id="", account_id="AUTO", trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT, payload_config={}, trigger_config={}, enabled=True,
        )
        prep_store.save(job_max)
        members_max = [PreparedSyncMember("p-bound-max", i, f"acc_{i}", f"l_{i}") for i in range(MAX_SYNC_MEMBERS)]
        prep_store.save_sync_members("p-bound-max", members_max)
        assert len(prep_store.get_sync_members("p-bound-max")) == MAX_SYNC_MEMBERS

        specs_max = [
            SyncMemberSpec(f"acc_{i}", f"l_{i}", "claude", "default", "medium", "0" * 64)
            for i in range(MAX_SYNC_MEMBERS)
        ]
        gid_max = sync_store.create("p-bound-max", "evt-max", "five_hour", "ALL_READY", specs_max, now)
        assert gid_max is not None

        # Boundary: MAX_SYNC_MEMBERS + 1 -> rejected
        job_over = PreparedJob(
            prepared_id="p-bound-over", name="Bound Over", project_id="proj1",
            launcher_id="", account_id="AUTO", trigger=Trigger.SYNC,
            payload=Payload.STATIC_PROMPT, payload_config={}, trigger_config={}, enabled=True,
        )
        prep_store.save(job_over)
        members_over = [PreparedSyncMember("p-bound-over", i, f"acc_{i}", f"l_{i}") for i in range(MAX_SYNC_MEMBERS + 1)]
        with pytest.raises(ValueError, match="exceed"):
            prep_store.save_sync_members("p-bound-over", members_over)

        specs_over = [
            SyncMemberSpec(f"acc_{i}", f"l_{i}", "claude", "default", "medium", "0" * 64)
            for i in range(MAX_SYNC_MEMBERS + 1)
        ]
        with pytest.raises(ValueError, match="exceed"):
            sync_store.create("p-bound-over", "evt-over", "five_hour", "ALL_READY", specs_over, now)

    def test_oversized_legacy_group_fails_closed_before_releasing(self, tmp_path: Path) -> None:
        """An oversized legacy group in SQLite must fail closed without releasing any member."""
        db_path = tmp_path / "prepared.db"
        store = SyncStore(db_path)
        group_id = "legacy-oversized-group"
        now = datetime.datetime.now(datetime.timezone.utc)

        # Directly insert oversized group into SQLite
        with sqlite3.connect(db_path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS sync_groups (
                    sync_group_id TEXT PRIMARY KEY,
                    prepared_id TEXT NOT NULL,
                    trigger_event_id TEXT NOT NULL,
                    target_window TEXT NOT NULL,
                    release_policy TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    preflight_deadline TEXT,
                    barrier_at TEXT,
                    process_skew_ms INTEGER
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS sync_members (
                    sync_group_id TEXT NOT NULL,
                    member_index INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    launcher_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    preflight_state TEXT NOT NULL,
                    result TEXT NOT NULL,
                    spawn_json TEXT,
                    PRIMARY KEY (sync_group_id, account_id)
                )"""
            )
            db.execute(
                """INSERT INTO sync_groups
                (sync_group_id, prepared_id, trigger_event_id, target_window, release_policy,
                 state, result, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (group_id, "p-1", "evt-1", "five_hour", "ALL_READY", "READY", "", now.isoformat()),
            )
            for i in range(MAX_SYNC_MEMBERS + 5):
                db.execute(
                    """INSERT INTO sync_members
                    (sync_group_id, account_id, launcher_id, provider_id,
                     model, effort, payload_sha256, preflight_state, state, result)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (group_id, f"acc_{i}", f"l_{i}", "claude", "default", "medium",
                     "0" * 64, "READY", "PENDING", ""),
                )
            db.commit()

        mock_launch = MagicMock()
        coordinator = SyncCoordinator(store, MagicMock(), MagicMock(), mock_launch)
        result = coordinator.release(group_id, now)

        assert result is None, "Release must fail closed for oversized group"
        assert mock_launch.call_count == 0, "No member must launch"

        # Check members were not set to RELEASING
        with sqlite3.connect(db_path) as db:
            row = db.execute("SELECT state, result FROM sync_groups WHERE sync_group_id=?",
                             (group_id,)).fetchone()
            assert row[0] in ("READY", "RECOVERY_REQUIRED")
            members = db.execute("SELECT preflight_state FROM sync_members WHERE sync_group_id=?",
                                 (group_id,)).fetchall()
            for m in members:
                assert m[0] != "RELEASING"


# ==============================================================================
# PERF-002: Bounded hot audit intent journal with indexed durable replay store
# ==============================================================================


class TestPerf002BoundedAuditIntentJournal:
    def test_hot_journal_bounds_history_and_tombstone_retains_replay(self, tmp_path: Path) -> None:
        """Settled prepared source intents must move to indexed tombstones and keep hot journal bounded."""
        journal_path = tmp_path / "intents.json"
        bound = 5
        store = AuditStartIntentStore(journal_path, history_bound=bound)

        # Seed 30 settled source intents
        for i in range(30):
            source_id = f"src-exec-{i:03d}"
            intent, created = store.begin(f"proj-{i}", f"Proj {i}", "quick3", source_execution_id=source_id)
            assert created is True
            # Advance to completed/settled
            store.update(intent["intent_id"], status="READY", campaign_run_id=f"run-{i}")

        # The hot journal must be bounded
        hot_intents = store.list()
        assert len(hot_intents) <= bound * 4, (
            f"Hot journal count {len(hot_intents)} must be bounded, not growing with all lifetime sources"
        )

        # Oldest source execution must still be resolved via indexed find_for_source
        old_record = store.find_for_source("src-exec-000")
        assert old_record is not None, "Oldest source execution must remain resolvable"
        assert old_record["source_execution_id"] == "src-exec-000"

        # Oldest source execution must refuse replay in begin()
        intent, created = store.begin("proj-0", "Proj 0", "quick3", source_execution_id="src-exec-000")
        assert created is False, "Oldest source execution must refuse duplicate begin()"
        assert intent["source_execution_id"] == "src-exec-000"

    def test_deterministic_proof_10000_cold_source_executions_bounded_and_resolvable(self, tmp_path: Path) -> None:
        """10,000 cold settled source executions must migrate to SQLite and keep hot JSON bounded."""
        journal_path = tmp_path / "intents.json"
        bound = 5
        store = AuditStartIntentStore(journal_path, history_bound=bound)

        # Batch-populate 10,000 settled source executions
        entries = []
        for i in range(10000):
            entries.append({
                "intent_id": f"int-{i}",
                "source_execution_id": f"src-exec-{i:05d}",
                "project_id": f"proj-{i}",
                "project_name": f"Proj {i}",
                "status": "READY",
                "dispatch_id": f"disp-{i}",
                "created_at": 1000.0 + i,
                "updated_at": 1001.0 + i,
                "completed_at": 1002.0 + i,
                "compacted": True,
            })
        store.path.write_text(json.dumps({"schema_version": 1, "updated_at": time.time(), "intents": entries}), encoding="utf-8")
        trimmed = store._trim_intents(entries)
        store.path.write_text(json.dumps({"schema_version": 1, "updated_at": time.time(), "intents": trimmed}), encoding="utf-8")

        # Hot JSON remains bounded
        hot = store.list()
        assert len(hot) <= bound * 4, f"Hot journal count {len(hot)} must be bounded"
        assert len(hot) == 0, "All compacted settled source entries must be migrated to tombstones"

        # Oldest identity still resolves via indexed find_for_source
        oldest = store.find_for_source("src-exec-00000")
        assert oldest is not None, "Oldest source identity must resolve"
        assert oldest["source_execution_id"] == "src-exec-00000"

        # Oldest identity still refuses replay in begin()
        intent, created = store.begin("p-0", "Proj 0", "quick3", source_execution_id="src-exec-00000")
        assert created is False, "Oldest source execution must refuse replay"
        assert intent["source_execution_id"] == "src-exec-00000"

        # Dashboard refresh / list() does not enumerate tombstones
        assert len(store.list()) <= 1

    def test_migration_ordering_tombstone_durable_before_hot_json_prune(self, tmp_path: Path) -> None:
        """A failure after tombstone commit but before hot JSON rewrite must still refuse replay."""
        journal_path = tmp_path / "intents.json"
        store = AuditStartIntentStore(journal_path, history_bound=5)

        # Create one settled intent
        intent, created = store.begin("p-crash", "Proj Crash", "quick3", source_execution_id="src-crash-01")
        assert created is True
        store.update(intent["intent_id"], status="READY", campaign_run_id="run-crash")

        # Directly insert tombstone into SQLite before pruning JSON to simulate state after tombstone commit
        with closing(store._get_tombstone_db()) as db:
            db.execute(
                """INSERT OR REPLACE INTO source_tombstones
                (source_execution_id, intent_id, project_id, project_name, status,
                 dispatch_id, created_at, updated_at, completed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("src-crash-01", intent["intent_id"], "p-crash", "Proj Crash", "READY",
                 "disp-crash", time.time(), time.time(), time.time(), ""),
            )
            db.commit()

        # Simulated crash: JSON has NOT been rewritten yet, both SQLite and JSON contain the record
        # A new begin() attempt must still refuse duplicate execution
        new_intent, new_created = store.begin("p-crash", "Proj Crash", "quick3", source_execution_id="src-crash-01")
        assert new_created is False, "Crash after tombstone durable must refuse replay"
        assert new_intent["source_execution_id"] == "src-crash-01"


# ==============================================================================
# PERF-003: Resident lightweight /health decoupled from load_config
# ==============================================================================


class TestPerf003LightweightHealthEndpoint:
    def test_health_does_not_invoke_load_config_or_path_healing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """GET /health must return status 200 without calling load_config or auto_heal_project_path."""
        cfg = AppConfig()
        cfg.bridge = BridgeConfig(host="127.0.0.1", port=0, token="test-token")

        class TestHandler(AudapackBridgeHandler):
            pass
        TestHandler.config = cfg
        TestHandler.test_base_dir = None

        server = ThreadingHTTPServer((cfg.bridge.host, cfg.bridge.port), TestHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)
        port = server.server_address[1]

        def poison_load_config(*args, **kwargs):
            raise AssertionError("load_config must not be invoked during /health")

        def poison_auto_heal(*args, **kwargs):
            raise AssertionError("auto_heal_project_path must not be invoked during /health")

        monkeypatch.setattr("audapack.config.load_config", poison_load_config)
        monkeypatch.setattr("audapack.config.auto_heal_project_path", poison_auto_heal)

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["service"] == "AUDAPACK Bridge"
            assert data["pid"] == os.getpid()
            assert "instance_nonce" in data
            assert "registry_revision" in data
        finally:
            server.shutdown()
            server.server_close()

    def test_repeated_unchanged_health_calls_perform_no_source_path_probes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Repeated GET /health calls must hit resident cache and perform zero project source filesystem probes."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("audapack.config.config_path", lambda base_dir=None: cfg_path)

        projects = [{"name": f"proj_{i}", "source_path": f"V:/fake/project/{i}"} for i in range(12)]
        cfg_path.write_text(json.dumps({"projects": projects}), encoding="utf-8")

        cfg = AppConfig()
        cfg.bridge = BridgeConfig(host="127.0.0.1", port=0, token="test-token")

        class TestHandler(AudapackBridgeHandler):
            pass
        TestHandler.config = cfg
        TestHandler.test_base_dir = None

        server = ThreadingHTTPServer((cfg.bridge.host, cfg.bridge.port), TestHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)
        port = server.server_address[1]

        def poison_load_config(*args, **kwargs):
            raise AssertionError("load_config must not be invoked during /health")

        def poison_auto_heal(*args, **kwargs):
            raise AssertionError("auto_heal_project_path must not be invoked during /health")

        monkeypatch.setattr("audapack.config.load_config", poison_load_config)
        monkeypatch.setattr("audapack.config.auto_heal_project_path", poison_auto_heal)

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
            for _ in range(5):
                conn.request("GET", "/health")
                resp = conn.getresponse()
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["ok"] is True
                assert data["registry_revision"] == 12
        finally:
            server.shutdown()
            server.server_close()

    def test_health_registry_revision_scaling_representative_configs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GET /health must scale cleanly across 12, 300, and 1000 projects without project healing."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr("audapack.config.config_path", lambda base_dir=None: cfg_path)

        cfg = AppConfig()
        cfg.bridge = BridgeConfig(host="127.0.0.1", port=0, token="test-token")

        class TestHandler(AudapackBridgeHandler):
            pass
        TestHandler.config = cfg
        TestHandler.test_base_dir = None

        server = ThreadingHTTPServer((cfg.bridge.host, cfg.bridge.port), TestHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)
        port = server.server_address[1]

        monkeypatch.setattr("audapack.config.load_config", lambda *a, **k: pytest.fail("forbidden"))
        monkeypatch.setattr("audapack.config.auto_heal_project_path", lambda *a, **k: pytest.fail("forbidden"))

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
            for count in (12, 300, 1000):
                projects = [{"name": f"p_{i}", "source_path": f"V:/fake/src/{i}"} for i in range(count)]
                cfg_path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
                time.sleep(0.01)

                t0 = time.monotonic()
                conn.request("GET", "/health")
                resp = conn.getresponse()
                elapsed = time.monotonic() - t0
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["registry_revision"] == count
                assert elapsed < 1.0, f"Health check took {elapsed}s for {count} projects"
        finally:
            server.shutdown()
            server.server_close()
