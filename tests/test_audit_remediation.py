"""Regression tests for audit remediation issues:
- Browser user-data privacy exclusions (audit/AUDAPACK_20261011_0418.md, AUDAPACK_20261011_0729.md)
- Disposable clean-checkout scratch tree exclusions (audit/AUDAPACK_20261010_1411.md)
- Contract-v3 manifest support and evidence diagnostics separation (audit/AUDAPACK_20261011_0436.md)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from audapack import fidelity
from audapack import saipen_evidence as se
from audapack.config import DEFAULT_EXCLUDES, PackingConfig
from audapack.packing import MANIFEST_FILENAME, pack_single
from audapack.source_inventory import (
    build_git_inventory,
    source_review_reconciliation,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "saipen"
CONTRACT_V3 = json.loads((FIXTURE_DIR / "MANIFEST.v3.json").read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


class AuditRemediationCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        self.output = self.root / "out"
        self.output.mkdir()
        (self.repo / "app.py").write_text("print('base')\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, msg="commit"):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", msg, "--allow-empty")

    def ignore(self, *lines):
        with open(self.repo / ".gitignore", "a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line.strip() + "\n")

    def _pack(self, stem="Proj", always_include=None, always_exclude=None):
        return pack_single(
            source_path=self.repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem},
            packing=PackingConfig(
                always_include=list(always_include or []),
                always_exclude=list(always_exclude or []),
            ),
        )


class TestBrowserUserDataExclusions(AuditRemediationCase):
    """AUDAPACK_20261011_0418.md & AUDAPACK_20261011_0729.md:
    Unconditional browser user-data profile exclusion before discretionary budget scoring,
    preserving legitimate source files named Cookies.
    """

    def test_browser_user_data_unconditionally_excluded_and_source_cookies_kept(self):
        # 1. Legitimate source files
        src_dir = self.repo / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "Cookies").write_text("# HTTP cookie parsing library\nclass Cookies: pass\n", encoding="utf-8")
        (src_dir / "cookies_helper.py").write_text("# helper\n", encoding="utf-8")

        # 2. Browser profile data
        webview_dir = self.repo / "gui.WebView2" / "EBWebView" / "Default"
        webview_dir.mkdir(parents=True)
        (webview_dir / "Cookies").write_bytes(b"SQLITE_BROWSER_COOKIE_DATA")
        (webview_dir / "Login Data").write_bytes(b"SQLITE_BROWSER_LOGIN_DATA")
        (webview_dir / "Web Data").write_bytes(b"SQLITE_BROWSER_WEB_DATA")

        gpu_dir = webview_dir / "GPUCache"
        gpu_dir.mkdir(parents=True)
        (gpu_dir / "data_0").write_bytes(b"CACHE_BYTES")

        crash_dir = self.repo / "gui.WebView2" / "EBWebView" / "Crashpad"
        crash_dir.mkdir(parents=True)
        (crash_dir / "metadata.db").write_bytes(b"TELEMETRY_DB")

        self.commit()

        inv = build_git_inventory(self.repo, excludes=set(DEFAULT_EXCLUDES))
        included = {e.rel for e in inv.included_entries()}
        excluded = {e.rel: e for e in inv.excluded_entries()}

        # Legitimate source files must be included
        self.assertIn("src/Cookies", included)
        self.assertIn("src/cookies_helper.py", included)

        # Browser data must be excluded under privacy_policy
        banned_paths = [
            "gui.WebView2/EBWebView/Default/Cookies",
            "gui.WebView2/EBWebView/Default/Login Data",
            "gui.WebView2/EBWebView/Default/Web Data",
            "gui.WebView2/EBWebView/Default/GPUCache/data_0",
            "gui.WebView2/EBWebView/Crashpad/metadata.db",
        ]
        for path in banned_paths:
            self.assertNotIn(path, included, f"{path} must not be included")
            self.assertIn(path, excluded, f"{path} must be in excluded entries")
            self.assertEqual(excluded[path].reason, fidelity.REASON_PRIVACY_POLICY)

        # Verify in final pack
        result = self._pack("BrowserTest")
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            namelist = set(zf.namelist())
        self.assertIn("src/Cookies", namelist)
        for path in banned_paths:
            self.assertNotIn(path, namelist)

    def test_always_include_cannot_override_browser_privacy_exclusion(self):
        webview_dir = self.repo / "gui.WebView2" / "EBWebView" / "Default"
        webview_dir.mkdir(parents=True)
        (webview_dir / "Cookies").write_bytes(b"BROWSER_COOKIES")
        (webview_dir / "Login Data").write_bytes(b"BROWSER_LOGINS")
        self.commit()

        result = self._pack(
            "PrivacyOverrideTest",
            always_include=[
                "gui.WebView2/EBWebView/Default/Cookies",
                "gui.WebView2/EBWebView/Default/Login Data",
            ],
        )
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            namelist = set(zf.namelist())
        self.assertNotIn("gui.WebView2/EBWebView/Default/Cookies", namelist)
        self.assertNotIn("gui.WebView2/EBWebView/Default/Login Data", namelist)


class TestCleanCheckoutScratchExclusions(AuditRemediationCase):
    """AUDAPACK_20261010_1411.md:
    Exclude disposable clean-checkout scratch trees (sai-clean-repro-*) without
    blanket exclusions of general TEMP directories.
    """

    def test_clean_repro_scratch_excluded_and_general_temp_handled_normally(self):
        repro_dir = self.repo / "sai-clean-repro-a1b2c3"
        repro_dir.mkdir(parents=True)
        (repro_dir / "repro_script.py").write_text("repro\n", encoding="utf-8")
        (repro_dir / "test.log").write_text("log\n", encoding="utf-8")

        docs_dir = self.repo / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "notes.txt").write_text("legitimate notes\n", encoding="utf-8")

        self.commit()

        inv = build_git_inventory(self.repo, excludes=set(DEFAULT_EXCLUDES))
        included = {e.rel for e in inv.included_entries()}
        excluded = {e.rel: e for e in inv.excluded_entries()}

        self.assertIn("docs/notes.txt", included)
        self.assertNotIn("sai-clean-repro-a1b2c3/repro_script.py", included)
        self.assertEqual(
            excluded["sai-clean-repro-a1b2c3/repro_script.py"].reason,
            "generated_output",
        )

        result = self._pack("ReproScratchTest")
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            namelist = set(zf.namelist())
        self.assertIn("docs/notes.txt", namelist)
        self.assertNotIn("sai-clean-repro-a1b2c3/repro_script.py", namelist)


class TestContractV3DiagnosticsSeparation(AuditRemediationCase):
    """AUDAPACK_20261011_0436.md:
    Contract-v3 manifest support and separation of evidence diagnostics into:
    - Included required evidence
    - Missing required evidence
    - Intentionally non-exportable references
    - Nested-instance exclusions
    - Security exclusions
    - Missing-on-disk citations
    - Inventory or compatibility failures
    """

    def _setup_v3_repo(self):
        sp = self.repo / ".saipen"
        sp.mkdir(parents=True)
        (sp / "STATE.md").write_text("phase: DONE\n", encoding="utf-8")
        (sp / "BOARD.md").write_text("# Board\n", encoding="utf-8")
        (sp / "IDENTITY.md").write_text("project_lineage: lin-1\n", encoding="utf-8")

        # Evidence dir
        (sp / "evidence").mkdir(parents=True)
        (sp / "evidence" / "bench.md").write_text("# benchmark proof\n", encoding="utf-8")

        # Creds dir with secret token
        (sp / "creds").mkdir(parents=True)
        (sp / "creds" / "token.txt").write_text("SECRET_TOKEN\n", encoding="utf-8")

        # Nested instance state (must be excluded under nested_instance_state)
        (sp / "sub").mkdir(parents=True)
        (sp / "sub" / "STATE.md").write_text("nested state\n", encoding="utf-8")

        # Non-exportable surface
        (sp / "recovery").mkdir(parents=True)
        (sp / "recovery" / "backup.json").write_text('{"backup": true}\n', encoding="utf-8")

        # Carriers citing evidence
        log_content = (
            "- E-1 start\n"
            "- E-2 gate PASS, proof at .saipen/evidence/bench.md\n"
            "- E-3 nested instance at .saipen/sub/STATE.md\n"
            "- E-4 recovery recorded at .saipen/recovery/backup.json\n"
            "- E-5 token reference at .saipen/creds/token.txt\n"
            "- E-6 missing proof at .saipen/evidence/missing_proof.md\n"
        )
        (sp / "LOG.md").write_text(log_content, encoding="utf-8")

        # Seal manifest last with surfaces declaring evidence, recovery, sub, and creds
        manifest_path = sp / "MANIFEST.json"
        doc = json.loads(json.dumps(CONTRACT_V3))
        doc["references"]["surfaces"] = ["evidence", "recovery", "sub", "creds"]
        manifest_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        self.ignore(".saipen/*")
        self.commit()

    def test_diagnostics_separated_and_honest_missing_status(self):
        self._setup_v3_repo()

        result = self._pack("V3DiagTest")
        self.assertTrue(result.success, result.error_message)

        with zipfile.ZipFile(result.output_path) as zf:
            namelist = set(zf.namelist())
            snapshot = json.loads(zf.read(MANIFEST_FILENAME))["saipen_snapshot"]

        # 1. Eligible required evidence is included
        self.assertIn(".saipen/evidence/bench.md", namelist)
        self.assertIn(".saipen/evidence/bench.md", snapshot["included_required"])

        # 2. Excluded nested instance remains excluded
        self.assertNotIn(".saipen/sub/STATE.md", namelist)
        self.assertIn(".saipen/sub/STATE.md", snapshot["cited_nested_instance"])

        # 3. Non-exportable reference remains excluded
        self.assertNotIn(".saipen/recovery/backup.json", namelist)
        self.assertIn(".saipen/recovery/backup.json", snapshot["cited_non_exportable"])

        # 4. Security exclusion remains excluded
        self.assertNotIn(".saipen/creds/token.txt", namelist)
        self.assertIn(".saipen/creds/token.txt", snapshot["cited_security_excluded"])

        # 5. Missing on disk citation honestly reported
        self.assertIn(".saipen/evidence/missing_proof.md", snapshot["cited_missing_on_disk"])
        self.assertIn(".saipen/evidence/missing_proof.md", snapshot["missing_required"])

        # 6. Honest non-authoritative status due to genuinely missing proof
        self.assertEqual(snapshot["status"], se.STATUS_PROTOCOL_INCOMPLETE)
        self.assertFalse(snapshot["authoritative_state"])

        # 7. Protected nested and non-exportables do NOT inflate omitted_required
        omitted_paths = [item["path"] for item in snapshot["omitted_required"]]
        self.assertNotIn(".saipen/sub/STATE.md", omitted_paths)
        self.assertNotIn(".saipen/recovery/backup.json", omitted_paths)
        self.assertNotIn(".saipen/creds/token.txt", omitted_paths)
        self.assertIn(".saipen/evidence/missing_proof.md", omitted_paths)

    def test_complete_v3_fixture_achieves_authoritative_status(self):
        self._setup_v3_repo()

        # Supply the missing proof and re-seal
        sp = self.repo / ".saipen"
        (sp / "evidence" / "missing_proof.md").write_text("# proof present\n", encoding="utf-8")
        manifest_path = sp / "MANIFEST.json"
        doc = json.loads(json.dumps(CONTRACT_V3))
        doc["references"]["surfaces"] = ["evidence", "recovery", "sub", "creds"]
        manifest_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")

        result = self._pack("V3CompleteTest")
        self.assertTrue(result.success, result.error_message)

        with zipfile.ZipFile(result.output_path) as zf:
            snapshot = json.loads(zf.read(MANIFEST_FILENAME))["saipen_snapshot"]

        self.assertEqual(snapshot["missing_required"], [])
        self.assertEqual(snapshot["omitted_required"], [])
        self.assertEqual(snapshot["status"], se.STATUS_COMPLETE)
        self.assertEqual(snapshot["admission"], se.ManifestAdmission.NATIVE)
        self.assertTrue(snapshot["manifest_parse_valid"])
        self.assertTrue(snapshot["contract_supported"])
        self.assertTrue(snapshot["evidence_complete"])
        self.assertTrue(snapshot["authoritative_state"])

    def test_regression_c_secret_remains_excluded_even_with_always_include(self):
        self._setup_v3_repo()
        result = self._pack(
            "SecretOverrideTest",
            always_include=[".saipen/creds/token.txt"],
        )
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            namelist = set(zf.namelist())
            snapshot = json.loads(zf.read(MANIFEST_FILENAME))["saipen_snapshot"]
        self.assertNotIn(".saipen/creds/token.txt", namelist)
        self.assertIn(".saipen/creds/token.txt", snapshot["cited_security_excluded"])

    def test_regression_d_durable_exception_outranks_prefix(self):
        doc = json.loads(json.dumps(CONTRACT_V3))
        # Add a conditional rule under recovery/ (which is in non_exportable)
        doc["evidence"]["conditional"].append(
            {"path": "recovery/board-compaction", "kind": "dir", "recursive": True, "max_files": 100}
        )
        sp = self.repo / ".saipen"
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "STATE.md").write_text("phase: DONE\n", encoding="utf-8")
        (sp / "BOARD.md").write_text("# Board\n", encoding="utf-8")
        (sp / "IDENTITY.md").write_text("project_lineage: lin-1\n", encoding="utf-8")
        (sp / "LOG.md").write_text("- E-1 done\n", encoding="utf-8")

        compaction_dir = sp / "recovery" / "board-compaction"
        compaction_dir.mkdir(parents=True)
        (compaction_dir / "compact.json").write_text('{"compact": true}\n', encoding="utf-8")

        manifest_path = sp / "MANIFEST.json"
        manifest_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        self.ignore(".saipen/*")
        self.commit()

        contract = se.read_contract(self.repo)
        self.assertTrue(contract.durable_precedence)
        exportable, rule = se.export_decision(".saipen/recovery/board-compaction/compact.json", contract)
        self.assertTrue(exportable)
        self.assertEqual(rule, se.EXPORT_RULE_DURABLE)

    def test_strict_refusal_preserves_independent_source_review(self):
        self._setup_v3_repo()
        document = json.loads(json.dumps(CONTRACT_V3))
        document["contract_version"] = 4
        document["generator"] = "saipen-audit-manifest/4"
        (self.repo / ".saipen" / "MANIFEST.json").write_text(json.dumps(document), encoding="utf-8")
        inventory = build_git_inventory(self.repo, set())
        review = source_review_reconciliation(inventory)
        self.assertTrue(review["source_review_ready"], review)
        self.assertEqual(inventory.saipen["status"], se.STATUS_CONTRACT_UNKNOWN)
        self.assertFalse(inventory.saipen["authoritative_state"])
        result = self._pack("UnsupportedV4")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, se.STATUS_CONTRACT_UNKNOWN)
        self.assertIsNone(result.output_path)

    def test_regression_g_unsupported_future_version_refused(self):
        doc = json.loads(json.dumps(CONTRACT_V3))
        doc["contract_version"] = 4
        doc["generator"] = "saipen-audit-manifest/4"
        # Every known field remains valid; implementation support is absent.
        contract = se.read_contract_from_document(doc)
        self.assertEqual(contract.status, se.STATUS_CONTRACT_UNKNOWN)
        self.assertEqual(contract.admission, se.ManifestAdmission.INCOMPATIBLE_UNSAFE)
        verdict = se.evaluate(se.collect(self.repo, contract))
        self.assertTrue(verdict["manifest_parse_valid"])
        self.assertEqual(verdict["manifest_parse_scope"], "envelope_only")
        self.assertFalse(verdict["contract_supported"])
        self.assertIsNone(verdict["evidence_complete"])
        self.assertFalse(verdict["authoritative_state"])

    def test_regression_i_source_review_readiness_independent_of_protocol_completeness(self):
        self._setup_v3_repo()
        result = self._pack("SourceReviewTest")
        # Pack succeeded and source review is ready even though protocol was incomplete
        self.assertTrue(result.success)
        self.assertIsNotNone(result.output_path)
        with zipfile.ZipFile(result.output_path) as zf:
            manifest = json.loads(zf.read(MANIFEST_FILENAME))
            snapshot = manifest["saipen_snapshot"]
        self.assertTrue(manifest["source_review_ready"])
        self.assertTrue(snapshot["manifest_parse_valid"])
        self.assertTrue(snapshot["contract_supported"])
        self.assertFalse(snapshot["evidence_complete"])
        self.assertFalse(snapshot["authoritative_state"])
        self.assertEqual(snapshot["status"], se.STATUS_PROTOCOL_INCOMPLETE)

