"""T-184: POST /v1/audits/materialize -- physical representation recovery.

Manual SYNC/SAVE is not ingest. Ingest commits a NEW complete wave and treats a
completed wave as immutable; materialization re-creates the physical files that
already-canonical content is supposed to occupy. These tests pin the contract
that separates them: identical canonical bytes always succeed, different bytes
always fail closed with nothing written, and no path here may mint a second
logical run or move a wave's receipt / sha256 / completed_at.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from audapack.bridge.server import AudapackBridgeHandler
from audapack.bridge.state import get_bridge_state_dir, get_run_state
from audapack.config import AppConfig, save_config
from audapack.models import Project

CORE_CONTENT = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-09-09T00:00:00
WAVE: AUDIT CORE
TARGET: SAIPEN repo
BASELINE: v1.0
STATUS: AUDIT_CORE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [CORE-001] audapack/core.py
EVIDENCE: broken loop
DEFECT: off by one
REPAIR: fix index
VERIFY: unit test

CORE_DONE_WHEN: tests pass"""

SECOND_CONTENT = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-09-09T00:00:00
WAVE: AUDIT SECOND WAVE
TARGET: SAIPEN repo
BASELINE: v1.0
CORE_BASELINE: v1.0
STATUS: SECOND_WAVE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P2] [W2-001] audapack/second.py
EVIDENCE: missing null check
DEFECT: crash on null
REPAIR: add guard
VERIFY: test null input

SECOND_WAVE_DONE_WHEN: tests pass"""

PERF_CONTENT = """PROJECT_NAME: SAIPEN
DATE_TIME: 2026-09-09T00:00:00
WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS
TARGET: SAIPEN repo
BASELINE: v1.0
PREVIOUS_BASELINE: v1.0
STATUS: PERFORMANCE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P2] [PERF-001] LOW-RISK SIMPLIFICATION audapack/perf.py
EVIDENCE: quadratic lookup
ISSUE: slow scan
OPTIMIZE: use set
GUARDRAIL: preserve ordering
VERIFY: benchmark

PERFORMANCE_DONE_WHEN: benchmark passes"""

WAVE_CONTENT = {"core": CORE_CONTENT, "second": SECOND_CONTENT, "performance": PERF_CONTENT}


def _strip_generated_stamps(text: str) -> str:
    """Drops synthesiser generation timestamps so two rebuilds compare equal."""
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?", "<GENERATED>", text)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MaterializeTestCase(unittest.TestCase):
    port = 18977

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_root = Path(self.temp_dir) / "AUDITING_IMPLEMENTATION"
        self.audit_root.mkdir(parents=True)

        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.config.bridge.host = "127.0.0.1"
        self.config.bridge.port = type(self).port
        type(self).port += 1
        self.config.bridge.token = "materialize_secret_token_123456"
        self.config.projects = [
            Project(
                id="saipen",
                display_name="SAIPEN",
                source_path=str(Path(self.temp_dir) / "SAIPEN"),
                priority_group="MAIN0",
                slot=1,
            ),
            Project(
                id="other",
                display_name="OTHER",
                source_path=str(Path(self.temp_dir) / "OTHER"),
                priority_group="MAIN0",
                slot=2,
            ),
        ]

        class TestHandler(AudapackBridgeHandler):
            pass

        TestHandler.config = self.config
        TestHandler.test_base_dir = self.temp_dir
        save_config(self.config, base_dir=self.temp_dir)
        self.server = ThreadingHTTPServer((self.config.bridge.host, self.config.bridge.port), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.run_id = f"acb-{secrets.token_hex(6)}"
        self.project_dir = self.audit_root / "MAIN0" / "SAIPEN"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"http://{self.config.bridge.host}:{self.config.bridge.port}{path}"

    def _post(self, path: str, payload: dict, token: str | None = "__default__"):
        headers = {"Content-Type": "application/json"}
        if token == "__default__":
            headers["X-ACB-Token"] = self.config.bridge.token
        elif token is not None:
            headers["X-ACB-Token"] = token
        req = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body}

    def _ingest(self, wave: str, receipt: str, content: str | None = None):
        status, data = self._post("/v1/audits", {
            "api_version": 2,
            "run_id": self.run_id,
            "project": "SAIPEN",
            "wave": wave,
            "status": "complete",
            "receipt": receipt,
            "content": content if content is not None else WAVE_CONTENT[wave],
        })
        self.assertEqual(status, 200, data)
        return data

    def _materialize(self, waves, receipt="mat-001", token="__default__", **overrides):
        payload = {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": receipt,
            "waves": [
                {"wave_id": wave, "sha256": sha256(WAVE_CONTENT[wave]), "content": WAVE_CONTENT[wave]}
                for wave in waves
            ],
        }
        payload.update(overrides)
        return self._post("/v1/audits/materialize", payload, token=token)

    def _ingest_core(self):
        self._ingest("core", "rcpt-core")

    def _ingest_all_three(self):
        self._ingest("core", "rcpt-core")
        self._ingest("second", "rcpt-second")
        self._ingest("performance", "rcpt-perf")

    def _wave_identity(self, wave: str) -> dict:
        state = get_run_state(self.run_id)
        stored = state["waves"][wave]
        return {
            "receipt": stored.get("receipt"),
            "sha256": stored.get("sha256"),
            "completed_at": stored.get("completed_at"),
            "complete": stored.get("complete"),
        }

    # -- 1..5 identity and precondition gates ----------------------------

    def test_01_auth_required(self):
        self._ingest_core()
        status, data = self._materialize(["core"], token=None)
        self.assertEqual(status, 403, data)

    def test_02_unknown_run_rejected(self):
        self._ingest_core()
        status, data = self._materialize(["core"], source_run_id="acb-does-not-exist")
        self.assertEqual(status, 404, data)
        self.assertEqual(data["error"]["code"], "unknown_run")

    def test_03_unknown_project_rejected(self):
        self._ingest_core()
        status, data = self._materialize(["core"], project_id="not-a-project")
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"]["code"], "invalid_project_id")

    def test_04_run_project_conflict_rejected(self):
        self._ingest_core()
        status, data = self._materialize(["core"], project_id="other")
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "project_identity_conflict")

    def test_05_wave_not_complete_rejected(self):
        self._ingest_core()
        status, data = self._materialize(["second"])
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "materialize_wave_not_complete")

    # -- 6..9 physical materialization -----------------------------------

    def test_06_matching_sha_and_content_materializes(self):
        self._ingest_core()
        status, data = self._materialize(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertFalse(data["duplicate"])
        self.assertEqual([w["wave_id"] for w in data["waves"]], ["core"])
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        self.assertTrue(latest.exists())
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)

    def test_07_missing_latest_file_recreated(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        self.assertFalse(latest.exists())
        status, data = self._materialize(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(latest.exists())
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)

    def test_08_missing_history_file_recreated(self):
        self._ingest_core()
        state = get_run_state(self.run_id)
        history_path = Path(state["waves"]["core"]["history_path"])
        history_path.unlink()
        self.assertFalse(history_path.exists())
        status, data = self._materialize(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(history_path.exists())
        self.assertEqual(history_path.read_text(encoding="utf-8"), CORE_CONTENT)

    def test_09_existing_files_overwritten_with_canonical_bytes(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.write_text("SOMEONE TRUNCATED THIS FILE", encoding="utf-8")
        status, data = self._materialize(["core"])
        self.assertEqual(status, 200, data)
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)

    # -- 10..11 fail-closed content conflicts ----------------------------

    def test_10_different_content_rejected_without_mutation(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        before_identity = self._wave_identity("core")
        mutated = CORE_CONTENT.replace("off by one", "totally different defect")
        status, data = self._post("/v1/audits/materialize", {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": "mat-conflict",
            "waves": [{"wave_id": "core", "sha256": sha256(mutated), "content": mutated}],
        })
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "materialize_content_conflict")
        self.assertFalse(data["error"]["retriable"])
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)
        self.assertEqual(self._wave_identity("core"), before_identity)

    def test_11_declared_sha_mismatch_rejected(self):
        self._ingest_core()
        status, data = self._post("/v1/audits/materialize", {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": "mat-sha",
            "waves": [{"wave_id": "core", "sha256": "0" * 64, "content": CORE_CONTENT}],
        })
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "materialize_content_conflict")

    # -- 12..13 receipt idempotency --------------------------------------

    def test_12_receipt_retry_is_idempotent(self):
        self._ingest_core()
        first_status, first = self._materialize(["core"], receipt="mat-retry")
        self.assertEqual(first_status, 200, first)
        self.assertFalse(first["duplicate"])
        second_status, second = self._materialize(["core"], receipt="mat-retry")
        self.assertEqual(second_status, 200, second)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["files"], first["files"])

    def test_13_receipt_reuse_with_different_payload_conflicts(self):
        self._ingest_all_three()
        status, data = self._materialize(["core"], receipt="mat-reuse")
        self.assertEqual(status, 200, data)
        status, data = self._materialize(["second"], receipt="mat-reuse")
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "receipt_conflict")

    # -- 14..15 transactional multi-wave ---------------------------------

    def test_14_multiple_waves_materialized_transactionally(self):
        self._ingest_all_three()
        for name in ("SAIPEN__01_AUDIT_CORE.md", "SAIPEN__02_AUDIT_SECOND_WAVE.md"):
            candidate = self.project_dir / name
            if candidate.exists():
                candidate.unlink()
        status, data = self._materialize(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertEqual({w["wave_id"] for w in data["waves"]}, {"core", "second", "performance"})
        state = get_run_state(self.run_id)
        for wave in ("core", "second", "performance"):
            latest = Path(state["waves"][wave]["latest_path"])
            self.assertTrue(latest.exists(), latest)
            self.assertEqual(latest.read_text(encoding="utf-8"), WAVE_CONTENT[wave])

    def test_15_injected_write_failure_rolls_all_files_back(self):
        self._ingest_all_three()
        state = get_run_state(self.run_id)
        core_latest = Path(state["waves"]["core"]["latest_path"])
        second_latest = Path(state["waves"]["second"]["latest_path"])
        core_latest.write_text("PREVIOUS CORE BYTES", encoding="utf-8")
        second_latest.write_text("PREVIOUS SECOND BYTES", encoding="utf-8")

        import audapack.bridge.server as server_module

        real_atomic_write = server_module.atomic_write
        calls = {"n": 0}

        def flaky(path, content):
            calls["n"] += 1
            if calls["n"] > 3:
                raise OSError("injected materialize write failure")
            return real_atomic_write(path, content)

        with mock.patch.object(server_module, "atomic_write", flaky):
            status, data = self._materialize(["core", "second", "performance"], receipt="mat-fail")
        self.assertEqual(status, 503, data)
        self.assertIn(data["error"]["code"], {"atomic_write_failed", "rollback_incomplete"})
        self.assertEqual(core_latest.read_text(encoding="utf-8"), "PREVIOUS CORE BYTES")
        self.assertEqual(second_latest.read_text(encoding="utf-8"), "PREVIOUS SECOND BYTES")

    # -- 16 final artifact -----------------------------------------------

    def test_16_all3_rebuilt_transactionally_when_3_of_3_requested(self):
        self._ingest_all_three()
        all3 = self.project_dir / "SAIPEN__00_AUDIT_ALL_3.md"
        self.assertTrue(all3.exists())
        original = all3.read_text(encoding="utf-8")
        all3.unlink()
        status, data = self._materialize(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["final_rebuilt"])
        self.assertTrue(data["all3_ready"])
        self.assertTrue(all3.exists())
        # The synthesiser stamps its own generation time, so compare everything
        # except that stamp: the rebuilt ALL_3 must be the same campaign, not a
        # byte-for-byte replay of a timestamp.
        self.assertEqual(
            _strip_generated_stamps(all3.read_text(encoding="utf-8")),
            _strip_generated_stamps(original),
        )

    def test_16b_partial_request_on_a_complete_campaign_rebuilds_all3(self):
        """T-185 B5: the required set is the campaign's, not the request's.

        The old contract let a single-wave request on a 3/3 run answer success
        while ALL_3 was deleted -- the same false durability the receipt replay
        had (P0-2). A partial request may never skip a required artifact.
        """
        self._ingest_all_three()
        all3 = self.project_dir / "SAIPEN__00_AUDIT_ALL_3.md"
        all3.unlink()
        status, data = self._materialize(["core"], receipt="mat-partial")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["final_rebuilt"])
        self.assertTrue(all3.exists())

    def test_16c_incomplete_campaign_materialize_never_requires_all3(self):
        """B1: a 1/3 run has no final artifact requirement -- Core saves truthfully."""
        self._ingest_core()
        all3 = self.project_dir / "SAIPEN__00_AUDIT_ALL_3.md"
        self.assertFalse(all3.exists(), "precondition: no final artifact yet")
        status, data = self._materialize(["core"], receipt="mat-1of3")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["final_rebuilt"])
        self.assertFalse(all3.exists())

    # -- 17..20 no logical mutation --------------------------------------

    def test_17_no_new_campaign_run_created(self):
        self._ingest_all_three()
        runs_dir = get_bridge_state_dir()
        before = sorted(p.name for p in runs_dir.glob("*.json"))
        status, data = self._materialize(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        after = sorted(p.name for p in runs_dir.glob("*.json"))
        self.assertEqual(before, after)
        state = get_run_state(self.run_id)
        self.assertEqual(state["run_id"], self.run_id)

    def test_18_original_completion_receipt_unchanged(self):
        self._ingest_core()
        before = self._wave_identity("core")
        self._materialize(["core"])
        self.assertEqual(self._wave_identity("core")["receipt"], before["receipt"])
        self.assertEqual(before["receipt"], "rcpt-core")

    def test_19_original_completed_at_unchanged(self):
        self._ingest_core()
        before = self._wave_identity("core")
        self._materialize(["core"])
        self.assertEqual(self._wave_identity("core")["completed_at"], before["completed_at"])

    def test_20_original_wave_sha_unchanged(self):
        self._ingest_core()
        before = self._wave_identity("core")
        self._materialize(["core"])
        after = self._wave_identity("core")
        self.assertEqual(after["sha256"], before["sha256"])
        self.assertEqual(after["sha256"], sha256(CORE_CONTENT))
        self.assertTrue(after["complete"])

    def test_20b_materialize_does_not_move_the_campaign_pointer(self):
        """A 1/3 materialization must leave `current_wave_id` on the next wave.

        save_live_campaign_index falls back to waves[0] when handed
        active_wave_id=None on an unfinished campaign, so a Core-only
        materialize could rewrite the live pointer from 'second' back to
        'core' -- SAVE silently mutating campaign state.
        """
        self._ingest_core()
        campaign_file = self.project_dir / "campaign.json"
        before = json.loads(campaign_file.read_text(encoding="utf-8"))
        self.assertEqual(before["current_wave_id"], "second")
        self.assertEqual(before["campaign_status"], "CAMPAIGN_READY_FOR_WAVE")

        status, data = self._materialize(["core"])
        self.assertEqual(status, 200, data)

        after = json.loads(campaign_file.read_text(encoding="utf-8"))
        self.assertEqual(after["current_wave_id"], "second")
        self.assertEqual(after["current_wave_index"], before["current_wave_index"])
        self.assertEqual(after["campaign_status"], before["campaign_status"])
        self.assertEqual(after["completed_waves"], before["completed_waves"])

    def test_21_history_artifact_is_not_duplicated(self):
        self._ingest_core()
        state = get_run_state(self.run_id)
        history_dir = Path(state["history_dir"])
        before = sorted(p.name for p in history_dir.iterdir())
        self._materialize(["core"], receipt="mat-hist-1")
        self._materialize(["core"], receipt="mat-hist-2")
        after = sorted(p.name for p in history_dir.iterdir())
        self.assertEqual(before, after)


    # -- T-185: duplicate acknowledgement must not name unverified files ----

    def test_22_duplicate_ingest_restores_a_deleted_canonical_file(self):
        """A 200 duplicate used to name recorded paths it never looked at.

        Deleting the canonical Core file and re-delivering identical content
        answered a clean duplicate, so the widget wrote bridgeSavedAt for a
        file that was gone. The content is proven identical, so the duplicate
        path now restores it byte-for-byte.
        """
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        self.assertFalse(latest.exists())

        data = self._ingest("core", "rcpt-core")
        self.assertTrue(data["duplicate"])
        self.assertTrue(latest.exists(), "the duplicate path restored the canonical file")
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)
        self.assertIn(str(latest), data["repaired_files"])

    def test_23_duplicate_ingest_refuses_success_when_a_file_cannot_be_restored(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()

        import audapack.bridge.server as server_module

        with mock.patch.object(server_module, "atomic_write", lambda path, content: None):
            status, data = self._post("/v1/audits", {
                "api_version": 2,
                "run_id": self.run_id,
                "project": "SAIPEN",
                "wave": "core",
                "status": "complete",
                "receipt": "rcpt-core",
                "content": CORE_CONTENT,
            })
        self.assertEqual(status, 503, data)
        self.assertEqual(data["error"]["code"], "duplicate_files_missing")
        self.assertTrue(data["error"]["retriable"])
        self.assertIn(str(latest), data["error"]["files_missing"])

    # -- T-185: verify_only ------------------------------------------------

    def _verify(self, waves, **overrides):
        payload = {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "verify_only": True,
            "waves": [{"wave_id": wave} for wave in waves],
        }
        payload.update(overrides)
        return self._post("/v1/audits/materialize", payload)

    def test_24_verify_only_reports_intact_files_and_needs_no_receipt(self):
        self._ingest_core()
        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["verify_only"])
        self.assertTrue(data["files_intact"])
        self.assertEqual(data["missing_files"], [])
        self.assertEqual(data["waves"][0]["wave_id"], "core")

    def test_25_verify_only_reports_a_vanished_file_and_writes_nothing(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"])
        self.assertIn(str(latest), data["missing_files"])
        self.assertFalse(latest.exists(), "a verification never writes")

    def test_26_verify_only_records_no_materialization_receipt(self):
        self._ingest_core()
        self._verify(["core"], receipt="verify-should-not-persist")
        state = get_run_state(self.run_id)
        self.assertNotIn("verify-should-not-persist", state.get("materializations", {}))

    def test_27_verify_only_still_rejects_an_unknown_run(self):
        self._ingest_core()
        status, data = self._verify(["core"], source_run_id="acb-nope")
        self.assertEqual(status, 404, data)
        self.assertEqual(data["error"]["code"], "unknown_run")

    # -- T-185: handoff project cross-check --------------------------------

    def test_28_materialize_rejects_a_foreign_handoff_project_name(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        before = latest.read_text(encoding="utf-8")
        foreign = CORE_CONTENT.replace("PROJECT_NAME: SAIPEN", "PROJECT_NAME: OTHER", 1)

        # Make the canonical sha agree so ONLY the project identity is wrong.
        state = get_run_state(self.run_id)
        state["waves"]["core"]["sha256"] = sha256(foreign)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        status, data = self._post("/v1/audits/materialize", {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": "mat-foreign",
            "waves": [{"wave_id": "core", "sha256": sha256(foreign), "content": foreign}],
        })
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "project_identity_conflict")
        self.assertEqual(latest.read_text(encoding="utf-8"), before, "nothing was written")


    # =========================================================================
    # T-185 residues: physical-byte durability, legacy derivation, read-only
    # verification, canonical project identity.
    # =========================================================================

    def _duplicate_ingest(self, receipt="rcpt-core"):
        return self._post("/v1/audits", {
            "api_version": 2,
            "run_id": self.run_id,
            "project": "SAIPEN",
            "wave": "core",
            "status": "complete",
            "receipt": receipt,
            "content": CORE_CONTENT,
        })

    def _clear_recorded_paths(self):
        state = get_run_state(self.run_id)
        for wave in state["waves"].values():
            wave.pop("latest_path", None)
            wave.pop("history_path", None)
        state.pop("history_dir", None)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

    # 1 + 2: corrupted existing file is false durability; repair restores it.
    def test_29_duplicate_ingest_does_not_accept_a_corrupted_file(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.write_text("CORRUPT", encoding="utf-8")

        status, data = self._duplicate_ingest()
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertIn(str(latest), data["repaired_files"], "corruption is repaired by the proven canonical bytes")
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)
        self.assertEqual(data["integrity"][str(latest)], "INTACT")

    def test_30_duplicate_refuses_success_while_corruption_cannot_be_repaired(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.write_text("CORRUPT", encoding="utf-8")

        import audapack.bridge.server as server_module
        with mock.patch.object(server_module, "atomic_write", lambda path, content: None):
            status, data = self._duplicate_ingest()
        self.assertEqual(status, 503, data)
        self.assertEqual(data["error"]["code"], "duplicate_files_missing")
        self.assertTrue(data["error"]["retriable"])
        self.assertIn(str(latest), data["error"]["files_missing"])
        self.assertEqual(latest.read_text(encoding="utf-8"), "CORRUPT")

    # 3: corrupted history repaired too.
    def test_31_duplicate_repairs_a_corrupted_history_file(self):
        self._ingest_core()
        state = get_run_state(self.run_id)
        history_path = Path(state["waves"]["core"]["history_path"])
        history_path.write_text("CORRUPT HISTORY", encoding="utf-8")

        status, data = self._duplicate_ingest()
        self.assertEqual(status, 200, data)
        self.assertIn(str(history_path), data["repaired_files"])
        self.assertEqual(history_path.read_text(encoding="utf-8"), CORE_CONTENT)

    # 4: transactional repair -- second write failing rolls back the first.
    def test_32_duplicate_repair_is_transactional(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        history_path = Path(get_run_state(self.run_id)["waves"]["core"]["history_path"])
        latest.write_text("PREVIOUS LATEST BYTES", encoding="utf-8")
        history_path.write_text("PREVIOUS HISTORY BYTES", encoding="utf-8")

        import audapack.bridge.server as server_module
        real_atomic_write = server_module.atomic_write

        def flaky(path, content):
            # Repair order walks dict.fromkeys((latest, history)): latest first.
            # Let only the history write fail so the latest rollback is observable.
            if Path(path).name.startswith("SAIPEN__01_AUDIT_CORE__"):
                raise OSError("injected history repair failure")
            return real_atomic_write(path, content)

        with mock.patch.object(server_module, "atomic_write", flaky):
            status, data = self._duplicate_ingest()
        self.assertEqual(status, 503, data)
        self.assertEqual(latest.read_text(encoding="utf-8"), "PREVIOUS LATEST BYTES", "file 1 must return to its exact pre-request bytes")
        self.assertEqual(history_path.read_text(encoding="utf-8"), "PREVIOUS HISTORY BYTES")

    # 5: path exists but is a directory -- fail closed.
    def test_33_duplicate_refuses_a_directory_at_a_canonical_path(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        latest.mkdir()

        status, data = self._duplicate_ingest()
        self.assertEqual(status, 503, data)
        self.assertEqual(data["error"]["code"], "duplicate_files_unverified")
        self.assertIn(str(latest), data["error"]["files_unverified"])
        self.assertTrue(latest.is_dir(), "a directory is never silently replaced")

    # 6: unreadable canonical file -- no durability success.
    def test_34_duplicate_refuses_an_unreadable_canonical_file(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"

        import audapack.bridge.storage as storage_module
        real = storage_module.read_canonical_file

        def unreadable(path):
            if Path(path) == latest:
                return None, "UNREADABLE"
            return real(path)

        with mock.patch.object(storage_module, "read_canonical_file", unreadable):
            status, data = self._duplicate_ingest()
        self.assertEqual(status, 503, data)
        self.assertEqual(data["error"]["code"], "duplicate_files_unverified")

    # 7: legacy completed wave with no recorded paths never answers files=[].
    def test_35_duplicate_with_legacy_state_derives_expected_paths(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        history_path = Path(get_run_state(self.run_id)["waves"]["core"]["history_path"])

        self._clear_recorded_paths()
        latest.unlink()
        history_path.unlink()
        self.assertFalse(latest.exists())
        self.assertFalse(history_path.exists())

        status, data = self._duplicate_ingest()
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["files"], "a completed wave always has a representation")
        self.assertTrue(latest.exists())
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)

    # 8: no safely derivable representation path -> named non-success.
    def test_36_duplicate_with_unresolvable_identity_fails_named(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()

        import audapack.bridge.server as server_module

        def escape(*args, **kwargs):
            raise server_module.InvalidProjectPathError("resolved destination escapes the audit root")

        with mock.patch.object(server_module, "resolve_project_audit_dir", escape):
            status, data = self._duplicate_ingest()
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"]["code"], "invalid_project_path")
        self.assertNotIn("files", data)

    # 9: verify_only detects corrupted bytes.
    def test_37_verify_only_detects_content_corruption(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.write_text("CORRUPT", encoding="utf-8")

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"])
        self.assertIn(str(latest), data["mismatched_files"])
        self.assertEqual(data["waves"][0]["intact"], False)
        self.assertTrue(latest.exists(), "a verification never repairs")

    # 10: verify_only keeps reporting missing files.
    def test_38_verify_only_still_reports_missing_files(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"])
        self.assertIn(str(latest), data["missing_files"])

    # 11: verify_only reports exact intact files with no repair.
    def test_39_verify_only_reports_intact_and_never_repairs(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        before = latest.read_bytes()
        history_path = Path(get_run_state(self.run_id)["waves"]["core"]["history_path"])
        history_before = history_path.read_bytes()

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["files_intact"])
        self.assertEqual(data["mismatched_files"], [])
        self.assertEqual(data["unreadable_files"], [])
        self.assertEqual(latest.read_bytes(), before)
        self.assertEqual(history_path.read_bytes(), history_before)


    # 12: verify_only never repairs anything (corrupted file stays corrupted).
    def test_40_verify_only_never_repairs(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.write_text("CORRUPT", encoding="utf-8")

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertEqual(latest.read_text(encoding="utf-8"), "CORRUPT", "repair belongs to SYNC/SAVE, not verify")

    # 13: verify_only with a deleted project directory leaves it absent.
    def test_41_verify_only_does_not_recreate_the_project_directory(self):
        self._ingest_core()
        import shutil
        shutil.rmtree(self.project_dir)
        self.assertFalse(self.project_dir.exists())

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertFalse(self.project_dir.exists(), "a verification must not even create a directory")
        self.assertTrue(data["missing_files"])

    # 14: verify_only changes zero config/registry/run-state bytes.
    def test_42_verify_only_changes_zero_state_bytes(self):
        self._ingest_core()
        from audapack.bridge.state import get_run_state_file
        state_file = get_run_state_file(self.run_id)
        state_before = state_file.read_bytes()
        campaign = (self.project_dir / "campaign.json").read_bytes()

        registry_count_before = len(load_config_registries(self.temp_dir))

        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)

        self.assertEqual(state_file.read_bytes(), state_before, "run state must not be touched")
        self.assertEqual((self.project_dir / "campaign.json").read_bytes(), campaign)
        self.assertEqual(
            len(load_config_registries(self.temp_dir)), registry_count_before,
            "a verification never registers projects"
        )

    # 15: legacy run with no project_id cannot auto-register during verify_only.
    def test_43_verify_only_never_auto_registers_a_legacy_run(self):
        self._ingest_core()
        state = get_run_state(self.run_id)
        state.pop("project_id", None)
        state.pop("project", None)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        before_count = len(load_config_registries(self.temp_dir))

        # project name in the request resolves (SAIPEN is registered), so the
        # verification succeeds -- but a name that CANNOT resolve must produce
        # a named error, never a registration.
        status, data = self._verify(["core"], project_name="TOTALLY_UNKNOWN_PROJECT_XYZ")
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "project_unresolvable_readonly")
        self.assertEqual(len(load_config_registries(self.temp_dir)), before_count, "verify never registers")

    # 16: materialize accepts display_name/audit_project_name aliases of the
    # same canonical project id (T-185 G).
    def test_44_materialize_accepts_audit_name_alias_of_same_project(self):
        self.config.projects[0].audit_project_name = "SAIPEN AUDIT ALIAS"
        save_config(self.config, base_dir=self.temp_dir)
        self._ingest_core()

        aliased = CORE_CONTENT.replace("PROJECT_NAME: SAIPEN", "PROJECT_NAME: SAIPEN AUDIT ALIAS", 1)
        state = get_run_state(self.run_id)
        state["waves"]["core"]["sha256"] = sha256(aliased)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        status, data = self._post("/v1/audits/materialize", {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": "mat-alias",
            "waves": [{"wave_id": "core", "sha256": sha256(aliased), "content": aliased}],
        })
        self.assertEqual(status, 200, data)

    def test_44b_duplicate_ingest_accepts_audit_name_alias(self):
        self.config.projects[0].audit_project_name = "SAIPEN AUDIT ALIAS"
        save_config(self.config, base_dir=self.temp_dir)
        self._ingest_core()

        aliased = CORE_CONTENT.replace("PROJECT_NAME: SAIPEN", "PROJECT_NAME: SAIPEN AUDIT ALIAS", 1)
        state = get_run_state(self.run_id)
        state["waves"]["core"]["sha256"] = sha256(aliased)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        status, data = self._post("/v1/audits", {
            "api_version": 2,
            "run_id": self.run_id,
            "project": "SAIPEN AUDIT ALIAS",
            "wave": "core",
            "status": "complete",
            "receipt": "rcpt-alias",
            "content": aliased,
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"], "an audit_project_name alias is the same canonical project")

    # 17: foreign handoff project remains 409 (existing test_28 pins it);
    # unknown ambiguous alias fails closed.
    def test_45_materialize_unknown_ambiguous_alias_fails_closed(self):
        self._ingest_core()
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        before = latest.read_text(encoding="utf-8")
        unknown = CORE_CONTENT.replace("PROJECT_NAME: SAIPEN", "PROJECT_NAME: UNKNOWN ALIAS PROJECT", 1)
        state = get_run_state(self.run_id)
        state["waves"]["core"]["sha256"] = sha256(unknown)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        status, data = self._post("/v1/audits/materialize", {
            "api_version": 3,
            "source_run_id": self.run_id,
            "project_name": "SAIPEN",
            "profile_id": "quick3",
            "receipt": "mat-unknown",
            "waves": [{"wave_id": "core", "sha256": sha256(unknown), "content": unknown}],
        })
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "project_identity_conflict")
        self.assertEqual(latest.read_text(encoding="utf-8"), before, "nothing was written")

    # =========================================================================
    # T-185 P0 residues: receipt replay must prove its physical EFFECT, and a
    # complete campaign's durability set must include the final handoff.
    # =========================================================================

    def _final_all3(self) -> Path:
        return self.project_dir / "SAIPEN__00_AUDIT_ALL_3.md"

    # P0-1 / A1: replay verifies the stored response's physical effect.
    def test_46_receipt_replay_restores_a_deleted_latest_file(self):
        self._ingest_core()
        status, first = self._materialize(["core"], receipt="mat-replay-1")
        self.assertEqual(status, 200, first)
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        self.assertFalse(latest.exists())

        status, data = self._materialize(["core"], receipt="mat-replay-1")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["files_intact"])
        self.assertTrue(latest.exists(), "replay must restore the physical effect")
        self.assertEqual(latest.read_text(encoding="utf-8"), CORE_CONTENT)

    def test_47_receipt_replay_restores_a_deleted_history_file(self):
        self._ingest_core()
        status, _first = self._materialize(["core"], receipt="mat-replay-2")
        self.assertEqual(status, 200)
        history_path = Path(get_run_state(self.run_id)["waves"]["core"]["history_path"])
        history_path.unlink()
        self.assertFalse(history_path.exists())

        status, data = self._materialize(["core"], receipt="mat-replay-2")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["files_intact"])
        self.assertTrue(history_path.exists())
        self.assertEqual(history_path.read_text(encoding="utf-8"), CORE_CONTENT)

    # P0-1 / P0-2: the final handoff is part of the replay's effect too.
    def test_48_receipt_replay_restores_a_deleted_all3(self):
        self._ingest_all_three()
        status, _first = self._materialize(["core", "second", "performance"], receipt="mat-replay-3")
        self.assertEqual(status, 200)
        all3 = self._final_all3()
        all3.unlink()
        self.assertFalse(all3.exists())

        status, data = self._materialize(["core", "second", "performance"], receipt="mat-replay-3")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["files_intact"])
        self.assertTrue(all3.exists(), "replay must restore the campaign-final artifact")

    # P0-1 / A4: a failed repair keeps the prior success history and records
    # the failure separately; the physical loss stays visible.
    def test_49_receipt_replay_repair_failure_fails_and_keeps_ledger_truth(self):
        self._ingest_core()
        status, first = self._materialize(["core"], receipt="mat-replay-4")
        self.assertEqual(status, 200, first)
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()

        import audapack.bridge.server as server_module

        with mock.patch.object(server_module, "atomic_write", lambda path, content: None):
            status, data = self._materialize(["core"], receipt="mat-replay-4")
        self.assertEqual(status, 503, data)
        self.assertNotIn("files_intact", data)

        state = get_run_state(self.run_id)
        entry = state["materializations"]["mat-replay-4"]
        self.assertEqual(entry["response"].get("duplicate"), False, "prior success history must not be rewritten")
        self.assertTrue(entry.get("repair_failures"), "the repair failure must be recorded separately")
        self.assertFalse(latest.exists(), "no partial repair may be published")

        # The receipt remains the same logical operation: a repaired retry of
        # the SAME request still works after the injected failure is gone.
        status, data = self._materialize(["core"], receipt="mat-replay-4")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["files_intact"])
        self.assertTrue(latest.exists())

    # P0-1 / A3: an unsafe artifact is never overwritten for a replay.
    def test_50_receipt_replay_refuses_an_unsafe_artifact(self):
        self._ingest_core()
        status, _first = self._materialize(["core"], receipt="mat-replay-5")
        self.assertEqual(status, 200)
        latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        latest.unlink()
        latest.mkdir()

        status, data = self._materialize(["core"], receipt="mat-replay-5")
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "materialize_replay_unsafe")
        self.assertTrue(latest.is_dir(), "a directory is never replaced to satisfy a replay")
        self.assertFalse(data.get("ok", True))

    # P0-2 / B4: verify_only must include the final artifact in the set.
    def test_51_verify_only_reports_a_missing_all3(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()

        status, data = self._verify(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"])
        self.assertIn(str(all3), data["missing_files"])
        self.assertFalse(all3.exists(), "a verification never recreates the final artifact")

    def test_52_verify_only_reports_a_corrupted_all3_as_content_mismatch(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.write_text("CORRUPT HANDOFF", encoding="utf-8")

        status, data = self._verify(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"])
        self.assertIn(str(all3), data["mismatched_files"])
        final_by_path = {entry["path"]: entry for entry in data["final_artifacts"]}
        self.assertEqual(final_by_path[str(all3)]["verdict"], "CONTENT_MISMATCH")

    def test_53_verify_only_of_an_incomplete_campaign_does_not_require_all3(self):
        self._ingest_core()
        status, data = self._verify(["core"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["files_intact"])
        self.assertEqual(data["final_artifacts"], [], "1/3 runs have no required final artifact")

    def test_54_verify_only_reports_an_intact_final_artifact(self):
        self._ingest_all_three()
        status, data = self._verify(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertTrue(data["files_intact"])
        verdicts = {entry["verdict"] for entry in data["final_artifacts"]}
        self.assertEqual(verdicts, {"INTACT"})

    # P0-2 / B5: normal materialization repairs the final artifact too, and a
    # partial request on a COMPLETE campaign still proves the required set.
    def test_55_materialize_restores_a_corrupted_all3(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        original = all3.read_text(encoding="utf-8")
        all3.write_text("CORRUPT HANDOFF", encoding="utf-8")

        status, data = self._materialize(["core", "second", "performance"], receipt="mat-all3-fix")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["final_rebuilt"])
        self.assertEqual(
            _strip_generated_stamps(all3.read_text(encoding="utf-8")),
            _strip_generated_stamps(original),
        )

    def test_55b_partial_materialize_on_a_complete_campaign_rebuilds_all3(self):
        """B5: on a complete campaign the required set includes ALL_3.

        A subset request used to answer success while the campaign-final
        artifact stayed deleted -- the same false durability the receipt
        replay had.
        """
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()

        status, data = self._materialize(["core"], receipt="mat-partial-complete")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["final_rebuilt"])
        self.assertTrue(all3.exists())

    def test_56_injected_final_write_failure_rolls_back_and_refuses_success(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()
        core_latest = Path(get_run_state(self.run_id)["waves"]["core"]["latest_path"])

        import audapack.bridge.server as server_module

        real_atomic_write = server_module.atomic_write
        calls = {"n": 0}

        def flaky(path, content):
            calls["n"] += 1
            if "ALL_3" in Path(path).name:
                raise OSError("injected final-artifact write failure")
            return real_atomic_write(path, content)

        with mock.patch.object(server_module, "atomic_write", flaky):
            status, data = self._materialize(["core", "second", "performance"], receipt="mat-all3-fail")
        self.assertEqual(status, 503, data)
        self.assertFalse(all3.exists(), "no partial repair: ALL_3 stays absent")
        self.assertEqual(core_latest.read_text(encoding="utf-8"), CORE_CONTENT, "wave repair rolled back")

    # P0-2 / B6: a duplicate of the final wave must not green a campaign whose
    # final artifact set is broken.
    def test_57_duplicate_final_wave_repairs_a_missing_all3(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()

        status, data = self._post("/v1/audits", {
            "api_version": 2,
            "run_id": self.run_id,
            "project": "SAIPEN",
            "wave": "performance",
            "status": "complete",
            "receipt": "rcpt-perf",
            "content": PERF_CONTENT,
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data["duplicate"])
        self.assertTrue(data["campaign_ready"])
        self.assertTrue(all3.exists(), "a duplicate of the final wave must repair the final artifact set")

    def test_57b_duplicate_final_wave_fails_closed_when_all3_cannot_be_rebuilt(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()

        import audapack.bridge.server as server_module

        def broken_synth(*args, **kwargs):
            raise OSError("injected synthesizer failure")

        with mock.patch.object(server_module, "generate_canonical_campaign", broken_synth):
            status, data = self._post("/v1/audits", {
                "api_version": 2,
                "run_id": self.run_id,
                "project": "SAIPEN",
                "wave": "performance",
                "status": "complete",
                "receipt": "rcpt-perf",
                "content": PERF_CONTENT,
            })
        self.assertEqual(status, 503, data)
        self.assertIn(data["error"]["code"], {"finalization_failed", "campaign_final_files_unverified"})
        self.assertFalse(all3.exists())

    def test_57c_duplicate_final_wave_fails_closed_on_a_directory_at_the_final_path(self):
        self._ingest_all_three()
        all3 = self._final_all3()
        all3.unlink()
        all3.mkdir()

        status, data = self._post("/v1/audits", {
            "api_version": 2,
            "run_id": self.run_id,
            "project": "SAIPEN",
            "wave": "performance",
            "status": "complete",
            "receipt": "rcpt-perf",
            "content": PERF_CONTENT,
        })
        self.assertEqual(status, 503, data)
        self.assertEqual(data["error"]["code"], "campaign_final_files_unverified")
        self.assertTrue(all3.is_dir(), "a directory at the final path is never replaced")

    def test_58_legacy_campaign_without_final_digests_fails_closed_on_verify(self):
        self._ingest_all_three()
        state = get_run_state(self.run_id)
        state.pop("final_artifact_digests", None)
        from audapack.bridge.state import save_run_state
        save_run_state(self.run_id, state)

        status, data = self._verify(["core", "second", "performance"])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["files_intact"], "an unprovable final artifact is never durable")

    # -- W2-005 (T-188 / SRC-044): project placement integrity ------------
    #
    # A run records ABSOLUTE canonical paths from the placement it was
    # ingested in. If the project later moves (priority group / display name)
    # or the configured audit root changes, materialization must not mix the
    # stale recorded wave paths with a freshly resolved destination: one
    # campaign gets ONE physical placement, or a named fail-closed refusal
    # (project_placement_changed) BEFORE any write. verify_only reports the
    # same conflict without mutating anything.

    def _move_saipen(self, group: str) -> None:
        """Physically relocate the project identity the way the registry would."""
        for proj in self.config.projects:
            if proj.id == "saipen":
                proj.priority_group = group
        save_config(self.config, base_dir=self.temp_dir)

    def _temp_snapshot(self) -> dict:
        """rel-path -> sha256 of every file under the temp base (locks excluded)."""
        snap = {}
        for base, _dirs, files in os.walk(self.temp_dir):
            for name in files:
                if name.endswith(".lock"):
                    continue
                p = Path(base) / name
                snap[str(p.relative_to(self.temp_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    def _run_state_json(self) -> str:
        return json.dumps(get_run_state(self.run_id), sort_keys=True, default=str)

    def _warm_config_normalization(self) -> None:
        """One throwaway authenticated request.

        check_auth -> load_config() rewrites (normalizes) config.json on its
        first load after an external config change -- a config-loader behavior
        that runs for EVERY endpoint before routing, not part of the
        materialize surface under test. Firing one request before the
        snapshot means the assertions below measure THIS operation's own
        mutation footprint, not the loader's.
        """
        self._verify(["core"])

    def test_59_verify_only_reports_placement_drift_without_mutation(self):
        self._ingest_core()
        self._move_saipen("SIDE1")
        self._warm_config_normalization()
        before_files = self._temp_snapshot()
        before_state = self._run_state_json()

        status, data = self._verify(["core"])

        self.assertEqual(status, 409, data)
        self.assertFalse(data.get("ok"), data)
        self.assertEqual(data["error"]["code"], "project_placement_changed")
        self.assertFalse(data["error"].get("retriable"), data)
        self.assertEqual(self._temp_snapshot(), before_files, "verify_only mutated the filesystem on placement drift")
        self.assertEqual(self._run_state_json(), before_state, "verify_only rewrote run state on placement drift")
        stale = data["error"].get("stale_paths")
        self.assertTrue(stale, "the response must name the stale recorded paths")
        self.assertTrue(any("MAIN0" in entry.get("placement", "") for entry in stale), data)

    def test_60_materialize_after_move_fails_closed_and_writes_nothing(self):
        self._ingest_core()
        old_latest = self.project_dir / "SAIPEN__01_AUDIT_CORE.md"
        old_latest.unlink()
        self._move_saipen("SIDE1")
        self._warm_config_normalization()
        before_files = self._temp_snapshot()
        before_state = self._run_state_json()

        status, data = self._materialize(["core"])

        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "project_placement_changed")
        self.assertFalse((self.audit_root / "SIDE1" / "SAIPEN").exists(), "no SIDE1 project dir may be created")
        self.assertFalse((self.audit_root / "SIDE1" / "SAIPEN" / "campaign.json").exists())
        self.assertFalse(old_latest.exists(), "the stale MAIN0 wave must NOT be recreated")
        self.assertEqual(self._run_state_json(), before_state, "run state (incl. receipt ledger) changed on refusal")
        self.assertEqual(self._temp_snapshot(), before_files)

    def test_61_output_root_move_fails_closed_without_mixing_roots(self):
        self._ingest_core()
        root_b = Path(self.temp_dir) / "AUDITING_ROOT_B"
        root_b.mkdir()
        self.config.audits.root = str(root_b)
        self._warm_config_normalization()
        before_state = self._run_state_json()

        status_v, data_v = self._verify(["core"])
        self.assertEqual(status_v, 409, data_v)
        self.assertEqual(data_v["error"]["code"], "project_placement_changed")

        status_m, data_m = self._materialize(["core"])
        self.assertEqual(status_m, 409, data_m)
        self.assertEqual(data_m["error"]["code"], "project_placement_changed")

        self.assertEqual(list(root_b.rglob("*")), [], "the new output root must stay empty")
        self.assertEqual(self._run_state_json(), before_state)
        self.assertTrue((self.audit_root / "MAIN0" / "SAIPEN" / "SAIPEN__01_AUDIT_CORE.md").exists())

    def test_62_unchanged_placement_still_materializes_and_verifies(self):
        self._ingest_core()
        status_m, data_m = self._materialize(["core"])
        self.assertEqual(status_m, 200, data_m)
        self.assertTrue(data_m["ok"])
        status_v, data_v = self._verify(["core"])
        self.assertEqual(status_v, 200, data_v)
        self.assertTrue(data_v["files_intact"], data_v)

    def test_63_alias_identity_change_is_not_placement_drift(self):
        self._ingest_core()
        moved = False
        for proj in self.config.projects:
            if proj.id == "saipen":
                proj.audit_project_name = "SAIPEN_AUDIT_ALIAS"
                moved = True
        self.assertTrue(moved)
        save_config(self.config, base_dir=self.temp_dir)

        status_m, data_m = self._materialize(["core"], project_name="SAIPEN_AUDIT_ALIAS")
        self.assertEqual(status_m, 200, data_m)
        self.assertTrue(data_m["ok"])
        status_v, data_v = self._verify(["core"], project_name="SAIPEN_AUDIT_ALIAS")
        self.assertEqual(status_v, 200, data_v)
        self.assertTrue(data_v["files_intact"], data_v)

    def test_64_receipt_replay_cannot_resurrect_a_stale_placement_success(self):
        self._ingest_core()
        status, data = self._materialize(["core"], receipt="mat-move")
        self.assertEqual(status, 200, data)
        self._move_saipen("SIDE1")
        self._warm_config_normalization()
        before_files = self._temp_snapshot()
        before_state = self._run_state_json()

        status_r, data_r = self._materialize(["core"], receipt="mat-move")

        self.assertEqual(status_r, 409, data_r)
        self.assertEqual(data_r["error"]["code"], "project_placement_changed")
        self.assertFalse(data_r.get("duplicate"), data_r)
        self.assertFalse((self.audit_root / "SIDE1" / "SAIPEN").exists())
        self.assertTrue((self.project_dir / "SAIPEN__01_AUDIT_CORE.md").exists())
        self.assertEqual(self._run_state_json(), before_state)
        self.assertEqual(self._temp_snapshot(), before_files)


def load_config_registries(base_dir):
    from audapack.config import load_config
    return load_config(Path(base_dir) if not isinstance(base_dir, Path) else base_dir).projects


if __name__ == "__main__":
    unittest.main()
