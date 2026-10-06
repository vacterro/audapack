"""SAIPEN audit-manifest authority gate: the T-850/SRC-007 regression matrix.

THE DEFECT THIS PINS
--------------------
The 2026-09-15 15:11:04 SAIPENVIEW audit archive shipped a green package over
a red snapshot: ``saipen_snapshot.status = PROTOCOL_MANIFEST_ABSENT`` with
``authoritative_state = false`` while the pack itself succeeded. The packer
computed the truthful SAIPEN verdict and wrote it into the manifest -- but
nothing in the canonical pack path ever ran ``saipen audit manifest
--write``, so the defect silently reproduced on every fresh pack.

The gate fixes the WORKFLOW ORCHESTRATION (outcome A): the normal pack path
now guarantees a current `.saipen/MANIFEST.json` before the inventory freeze
-- generated through the REAL protocol CLI when missing, regenerated when
stale -- or fails closed with a precise precondition error. Manifest bytes
are never synthesized by Audapack: tests inject a stub protocol CLI
(``bin/saipen`` / ``bin/saipen.cmd`` + one writer) the same way the real
CLI would be resolved from the project's own ``saipen_home`` STATE key.

Every test below is one of the audit brief's required regressions and is
named for it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from audapack import saipen_evidence as se
from audapack import saipen_manifest_gate as sg
from audapack.config import PackingConfig
from audapack.packing import (
    MANIFEST_FILENAME,
    PACK_STATUS_FAILED_INVENTORY,
    pack_single,
)
from audapack.procutil import hidden_spawn_kwargs

#: Canonical contract fixtures captured from the REAL generator; see
#: tests/fixtures/saipen/README.md. The gate must accept the document SAIPEN
#: writes TODAY, which is what these tests prove it does.
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "saipen"


def contract_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


CONTRACT_V2 = contract_fixture("MANIFEST.v2.json")
CONTRACT_V1_CANONICAL = contract_fixture("MANIFEST.v1.json")

# The contract shape the protocol CLI publishes (mirrors the real
# saipen-audit-manifest/1 document; see test_saipen_evidence.CONTRACT).
CONTRACT = {
    "schema_version": 1,
    "kind": "saipen_audit_manifest",
    "contract_version": 1,
    "protocol_version": "8.0.1",
    "generated_at": "",
    "generator": "saipen-audit-manifest/1",
    "memory_root": ".saipen",
    "required": ["STATE.md", "BOARD.md", "LOG.md", "IDENTITY.md"],
    "evidence": {
        "mandatory": [
            {"path": "STATE.md", "kind": "file"},
            {"path": "BOARD.md", "kind": "file"},
            {"path": "LOG.md", "kind": "file"},
            {"path": "IDENTITY.md", "kind": "file"},
        ],
        "conditional": [
            {"path": "logs", "kind": "dir", "recursive": True, "max_files": 4000},
        ],
        "optional": [],
        "non_exportable": ["locks/", "recovery/", "LOCAL_STATE.json"],
    },
}

_WRITER_SOURCE = '''"""Stub SAIPEN protocol CLI writer (test double)."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    leaked = [key for key in ("SAIPEN_PROJECT_ROOT", "SAIPEN_PROJECT_LINEAGE", "SAIPEN_AGENT") if key in os.environ]
    if leaked:
        print("consumer binding leaked: " + ",".join(leaked), file=sys.stderr)
        return 91
    home = Path(__file__).resolve().parent
    behavior_path = home / "saipen_behavior.json"
    behavior = behavior_path.read_text(encoding="utf-8").strip() if behavior_path.exists() else "write"
    if behavior in ("current_once", "refuse_after_current"):
        marker = home / "saipen_current_once.used"
        if not marker.exists():
            marker.write_text("used", encoding="utf-8")
            print("code: AUDIT_MANIFEST_CURRENT")
            return 0
        if behavior == "refuse_after_current":
            print("code: CAPABILITY_REFUSED", file=sys.stderr)
            return 7
    if behavior == "current":
        print("code: AUDIT_MANIFEST_CURRENT")
        return 0
    if behavior == "refuse":
        print("code: CAPABILITY_REFUSED", file=sys.stderr)
        return 7
    sp = Path.cwd() / ".saipen"
    sp.mkdir(parents=True, exist_ok=True)
    contract_path = Path(__file__).resolve().parent / "saipen_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if behavior == "stale_contract":
        contract["evidence"]["mandatory"].append({"path": "MISSING.md", "kind": "file"})
    contract["generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    (sp / "MANIFEST.json").write_text(
        json.dumps(contract, indent=1), encoding="utf-8"
    )
    print("code: AUDIT_MANIFEST_WRITTEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_POSIX_SHIM = '#!/bin/sh\nexec "{python}" "{writer}" "$@"\n'
_WIN_SHIM = '@echo off\r\n"{python}" "{writer}" %*\r\n'


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


class SaipenGateCase(unittest.TestCase):
    """Fixture: a Git SAIPEN project plus a resolvable stub protocol CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        self.out = self.root / "out"
        self.out.mkdir()
        (self.repo / "app.py").write_text("print('x')\n", encoding="utf-8")
        self.write_saipen()
        self.build_stub_cli()
        self.packing = PackingConfig()

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixture helpers ---------------------------------------------------

    def write_saipen(self, *, manifest: bool = False, contract=None):
        sp = self.repo / ".saipen"
        sp.mkdir(exist_ok=True)
        (sp / "STATE.md").write_text("phase: DONE\n", encoding="utf-8")
        (sp / "BOARD.md").write_text("# Board\n", encoding="utf-8")
        (sp / "LOG.md").write_text("- E-1 start\n", encoding="utf-8")
        (sp / "IDENTITY.md").write_text("project_lineage: lineage-x\n", encoding="utf-8")
        (sp / "logs").mkdir(exist_ok=True)
        (sp / "logs" / "LOG-001.md").write_text("- E-0 sealed\n", encoding="utf-8")
        # Runtime surfaces the contract marks non-exportable, plus an
        # undeclared cache dir a careless export would leak.
        (sp / "locks").mkdir(exist_ok=True)
        (sp / "locks" / "core.lock").write_text("lock\n", encoding="utf-8")
        (sp / "recovery").mkdir(exist_ok=True)
        (sp / "recovery" / "op.json").write_text("{}\n", encoding="utf-8")
        (sp / "LOCAL_STATE.json").write_text("{}\n", encoding="utf-8")
        (sp / "cache").mkdir(exist_ok=True)
        (sp / "cache" / "runtime.bin").write_text("raw\n", encoding="utf-8")
        if manifest:
            document = dict(CONTRACT if contract is None else contract)
            document["generated_at"] = (
                datetime.now(timezone.utc).isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            (sp / "MANIFEST.json").write_text(
                json.dumps(document, indent=1), encoding="utf-8"
            )
        return sp

    def install_contract(self, document):
        """Install one contract document, LAST, as the gate's precondition.

        Writes the manifest alone rather than going through ``write_saipen``:
        that helper rewrites STATE.md and would drop the ``saipen_home`` line
        the fixture's stub protocol CLI is resolved through.
        """
        (self.repo / ".saipen" / "MANIFEST.json").write_text(
            json.dumps(document, indent=1), encoding="utf-8"
        )

    def stub_writes(self, document):
        """Point the stub protocol CLI at the contract it should generate.

        A current protocol install writes the CURRENT contract, which is how a
        stale v1 manifest is legitimately upgraded to v2 by regeneration.
        """
        (self._saipen_home / "bin" / "saipen_contract.json").write_text(
            json.dumps(document, indent=1), encoding="utf-8"
        )

    def stub_behavior(self, behavior: str) -> None:
        home = self._saipen_home / "bin"
        (home / "saipen_behavior.json").write_text(behavior, encoding="utf-8")
        marker = home / "saipen_current_once.used"
        if behavior != "current_once":
            marker.unlink(missing_ok=True)

    def age_manifest(self):
        """Make the installed manifest look stale without sleeping.

        Mandatory evidence newer than the document is exactly what a real
        project looks like after a canonical write, and it is what forces the
        gate to regenerate through the protocol CLI.
        """
        state = self.repo / ".saipen" / "STATE.md"
        st = state.stat()
        os.utime(state, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))

    def build_stub_cli(self):
        """A stub protocol CLI reachable through the project's own saipen_home.

        The gate resolves candidates exactly like production: the STATE key
        is tried before PATH, so the stub is authoritative for the fixture
        regardless of the machine's real PATH content.
        """
        home = self.root / "fakehome"
        self._saipen_home = home
        (home / "bin").mkdir(parents=True)
        writer = home / "bin" / "saipen_writer.py"
        writer.write_text(_WRITER_SOURCE, encoding="utf-8")
        (home / "bin" / "saipen_contract.json").write_text(
            json.dumps(CONTRACT, indent=1), encoding="utf-8"
        )
        python = sys.executable.replace("\\", "/")
        shim_args = {"python": python, "writer": str(writer).replace("\\", "/")}
        (home / "bin" / "saipen").write_text(_POSIX_SHIM.format(**shim_args), encoding="utf-8")
        (home / "bin" / "saipen.cmd").write_text(_WIN_SHIM.format(**shim_args), encoding="utf-8")
        if os.name != "nt":
            p = home / "bin" / "saipen"
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        state = self.repo / ".saipen" / "STATE.md"
        state.write_text(
            f"phase: DONE\nsaipen_home: {home.as_posix()}\n", encoding="utf-8"
        )

    def ignore(self, *lines: str):
        (self.repo / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def commit(self):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "w")

    def pack(self, *, manifest_meta=None, **overrides):
        return pack_single(
            source_path=self.repo,
            output_dir=self.out,
            archive_stem="proj",
            excludes=set(self.packing.excludes),
            delete_old=True,
            include_timestamp=False,
            manifest_meta=(
                manifest_meta if manifest_meta is not None else {"project_name": "proj"}
            ),
            packing=self.packing,
            **overrides,
        )

    def archive(self, name: str = "proj.zip"):
        with zipfile.ZipFile(self.out / name) as zf:
            return zf

    def legacy_manifest(self):
        with zipfile.ZipFile(self.out / "proj.zip") as zf:
            return json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))

    def source_manifest_from_archive(self):
        with zipfile.ZipFile(self.out / "proj.zip") as zf:
            return json.loads(zf.read(".saipen/MANIFEST.json").decode("utf-8"))


class TestMissingManifestRegression(SaipenGateCase):
    """Brief: pack a valid manifest-less SAIPEN project via the canonical path.
    The repair must auto-create the contract and produce an AUTHORITATIVE
    archive -- never a silent PROTOCOL_MANIFEST_ABSENT package."""

    def test_pack_auto_generates_manifest_and_archive_is_authoritative(self):
        self.assertFalse((self.repo / ".saipen" / "MANIFEST.json").exists())
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        # The canonical workflow created the contract artifact itself.
        manifest_path = self.repo / ".saipen" / "MANIFEST.json"
        self.assertTrue(manifest_path.exists())
        contract = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["kind"], "saipen_audit_manifest")
        # The archive carries both the contract and its evidence.
        with self.archive() as zf:
            names = set(zf.namelist())
        self.assertIn(".saipen/MANIFEST.json", names)
        for rel in (
            ".saipen/STATE.md",
            ".saipen/BOARD.md",
            ".saipen/LOG.md",
            ".saipen/IDENTITY.md",
            ".saipen/logs/LOG-001.md",
        ):
            self.assertIn(rel, names)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertEqual(snapshot["status"], "COMPLETE")
        self.assertTrue(snapshot["authoritative_state"])
        self.assertFalse(snapshot["required_evidence_omitted"])
        self.assertEqual(snapshot["mandatory_missing"], [])
        self.assertEqual(snapshot["omitted_required"], [])
        self.assertEqual(snapshot["verdict_basis"], "final_archive_content")

    def test_pack_fails_closed_when_no_protocol_cli_is_reachable(self):
        with mock.patch.object(
            sg, "SAIPEN_CLI_CANDIDATES", (("no-such-saipen-binary-xyz",),)
        ):
            res = self.pack()
        self.assertFalse(res.success)
        self.assertEqual(res.status, PACK_STATUS_FAILED_INVENTORY)
        self.assertIn("audit-manifest precondition failed", res.error_message)
        self.assertIn("saipen audit manifest --write", res.error_message)
        # Fail closed BEFORE any archive byte exists: no authoritative-looking
        # package, no staging residue.
        self.assertFalse((self.out / "proj.zip").exists())
        self.assertEqual([p.name for p in self.out.iterdir() if p.suffix == ".zip"], [])


class TestStaleManifestRegression(SaipenGateCase):
    """Brief: generate -> mutate authoritative evidence -> package again ->
    prove the old manifest is not blindly trusted (regenerate or reject)."""

    def test_stale_manifest_with_missing_cli_fails_closed_with_discovery_reason(self):
        self.install_contract(CONTRACT)
        self.age_manifest()
        with mock.patch.object(
            sg, "SAIPEN_CLI_CANDIDATES", (("no-such-saipen-binary-xyz",),)
        ):
            result = self.pack()

        self.assertFalse(result.success)
        self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
        self.assertIn("AUDIT_MANIFEST_STALE_REGENERATION_FAILED", result.error_message)
        self.assertIn("launcher discovery", result.error_message)
        self.assertIn("no-such-saipen-binary-xyz", result.error_message)
        self.assertFalse((self.out / "proj.zip").exists())

    def test_pack_after_evidence_mutation_regenerates_not_blind_trusts(self):
        first = self.pack()
        self.assertTrue(first.success, first.error_message)
        first_generated_at = self.source_manifest_from_archive()["generated_at"]
        self.assertTrue(first_generated_at)

        state = self.repo / ".saipen" / "STATE.md"
        # The mutation must keep the fixture's real saipen_home line: it is
        # the CLI discovery path the gate needs when it regenerates the
        # stale contract below. (The literal "..." destroyed it and made the
        # regeneration fail with an unrelated executable-not-found error.)
        state.write_text(
            f"phase: SCOUT\nsaipen_home: {self._saipen_home.as_posix()}\n",
            encoding="utf-8",
        )
        # Coarse filesystems quantize mtime; force the mutation to be strictly
        # newer than the existing contract without any wall-clock sleeping.
        st = state.stat()
        os.utime(state, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        # Gate level (airtight): the stale contract is REGENERATED.
        gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertTrue(gate.generated, gate.detail)

        second = self.pack()
        self.assertTrue(second.success, second.error_message)
        second_contract = self.source_manifest_from_archive()
        # The regenerated contract is a different document from the one the
        # first archive trusted.
        self.assertNotEqual(second_contract["generated_at"], first_generated_at)
        with self.archive() as zf:
            names = set(zf.namelist())
        self.assertIn(".saipen/MANIFEST.json", names)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertTrue(snapshot["authoritative_state"])

    def test_stale_current_noop_is_forced_to_recreate_and_verify_freshness(self):
        self.install_contract(CONTRACT)
        before = (self.repo / ".saipen" / "MANIFEST.json").read_bytes()
        self.age_manifest()
        self.stub_behavior("current_once")
        logs = []

        gate = sg.prepare(self.repo, log_callback=logs.append)

        self.assertTrue(gate.ok, gate.detail)
        self.assertTrue(gate.generated, gate.detail)
        self.assertEqual(gate.generated_by[-3:], ["audit", "manifest", "--write"])
        after_path = self.repo / ".saipen" / "MANIFEST.json"
        self.assertNotEqual(after_path.read_bytes(), before)
        contract = se.read_contract(self.repo)
        self.assertEqual(sg._contract_staleness(self.repo, contract), "")
        self.assertTrue(any("manifest bytes unchanged" in line for line in logs), logs)
        self.assertGreaterEqual(
            sum("saipen audit manifest --write ->" in line for line in logs), 2, logs
        )
        self.assertEqual(list((self.repo / ".saipen").glob("*.audapack-*.bak")), [])

    def test_stale_regeneration_refusal_preserves_old_manifest_and_fails_closed(self):
        self.install_contract(CONTRACT)
        before = (self.repo / ".saipen" / "MANIFEST.json").read_bytes()
        self.age_manifest()
        self.stub_behavior("refuse_after_current")

        gate = sg.prepare(self.repo)

        self.assertFalse(gate.ok)
        self.assertEqual(gate.code, "AUDIT_MANIFEST_STALE_REGENERATION_FAILED")
        self.assertIn("protocol refusal", gate.detail)
        self.assertIn("previous stale manifest restored", gate.detail)
        self.assertEqual((self.repo / ".saipen" / "MANIFEST.json").read_bytes(), before)
        self.assertEqual(list((self.repo / ".saipen").glob("*.audapack-*.bak")), [])

    def test_pack_exposes_protocol_refusal_reason(self):
        self.install_contract(CONTRACT)
        self.age_manifest()
        self.stub_behavior("refuse")

        logs = []
        result = self.pack(log_callback=logs.append)

        self.assertFalse(result.success)
        self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
        self.assertIn("AUDIT_MANIFEST_STALE_REGENERATION_FAILED", result.error_message)
        self.assertIn("protocol refusal", result.error_message)
        self.assertIn("CAPABILITY_REFUSED", result.error_message)
        self.assertTrue(any(result.error_message in line for line in logs), logs)
        self.assertFalse((self.out / "proj.zip").exists())

    def test_successful_but_invalid_rewrite_restores_authority_and_fails_closed(self):
        self.install_contract(CONTRACT)
        before = (self.repo / ".saipen" / "MANIFEST.json").read_bytes()
        self.age_manifest()
        self.stub_behavior("stale_contract")

        gate = sg.prepare(self.repo)

        self.assertFalse(gate.ok)
        self.assertEqual(gate.code, "AUDIT_MANIFEST_STALE_REGENERATION_FAILED")
        self.assertIn("manifest remains stale", gate.detail)
        self.assertIn("previous stale manifest restored", gate.detail)
        self.assertEqual((self.repo / ".saipen" / "MANIFEST.json").read_bytes(), before)

    def test_stale_regeneration_timeout_is_exact_and_actionable(self):
        self.install_contract(CONTRACT)
        self.age_manifest()
        timeout = subprocess.TimeoutExpired(["saipen", "audit", "manifest", "--write"], 7)

        with mock.patch.object(sg, "run_hidden", side_effect=timeout):
            gate = sg.prepare(self.repo, timeout_seconds=7)

        self.assertFalse(gate.ok)
        self.assertEqual(gate.code, "AUDIT_MANIFEST_STALE_REGENERATION_FAILED")
        self.assertIn("launcher timeout", gate.detail)
        self.assertIn("timed out after 7s", gate.detail)

    def test_cli_discovery_order_and_absolute_path_resolution(self):
        local_bin = self.repo / "bin"
        local_bin.mkdir()
        local = None
        for candidate in sg._ordered_cli_candidates():
            local = local_bin / candidate[0]
            local.write_text("local", encoding="utf-8")
            break
        candidates = sg._cli_candidates(
            self.repo, self.repo / ".saipen"
        )
        self.assertEqual(candidates[0], [str(local)])

        empty_memory = self.root / "empty-memory"
        empty_memory.mkdir()
        path_project = self.root / "path-project"
        path_project.mkdir()
        path_launcher = self.root / "path-bin" / "saipen.exe"
        path_launcher.parent.mkdir()
        path_launcher.write_text("stub", encoding="utf-8")
        # The PATH step is what this asserts, so the earlier steps are held out.
        # _well_known_install_candidates() finds a REAL install under
        # %LOCALAPPDATA% on any machine that has one, and it legitimately
        # outranks PATH (that is how a foreign saipen_home stops blocking a pack),
        # so without this the assertion only holds on a machine without SAIPEN.
        with (
            mock.patch.object(sg.shutil, "which", return_value=str(path_launcher)),
            mock.patch.object(sg, "_well_known_install_candidates", return_value=[]),
        ):
            resolved = sg._cli_candidates(path_project, empty_memory)
        self.assertEqual(resolved[0], [str(path_launcher.resolve())])

    def test_cli_runs_hidden_noninteractive_and_unbound_in_target_root(self):
        self.install_contract(CONTRACT)
        self.age_manifest()
        real_runner = sg.run_hidden
        calls = []

        def capture(command, **kwargs):
            calls.append((command, kwargs))
            return real_runner(command, **kwargs)

        binding = {
            "SAIPEN_PROJECT_ROOT": str(self.root / "consumer"),
            "SAIPEN_PROJECT_LINEAGE": "consumer-lineage",
            "SAIPEN_AGENT": "consumer-agent",
        }
        with mock.patch.dict(os.environ, binding, clear=False):
            with mock.patch.object(sg, "run_hidden", side_effect=capture):
                gate = sg.prepare(self.repo)

        self.assertTrue(gate.ok, gate.detail)
        self.assertEqual(len(calls), 1, calls)
        command, kwargs = calls[0]
        self.assertEqual(command[-3:], ["audit", "manifest", "--write"])
        self.assertEqual(Path(kwargs["cwd"]).resolve(), self.repo.resolve())
        self.assertTrue(kwargs["capture_output"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        for key in sg.SESSION_BINDING_ENV:
            self.assertNotIn(key, kwargs["env"])
        if os.name == "nt":
            hidden = hidden_spawn_kwargs(**kwargs)
            self.assertTrue(
                hidden["creationflags"] & subprocess.CREATE_NO_WINDOW,
                hidden,
            )

    def test_current_contract_is_left_alone(self):
        self.pack()
        before = (self.repo / ".saipen" / "MANIFEST.json").read_bytes()
        gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok)
        self.assertFalse(gate.generated)
        self.assertEqual(
            before, (self.repo / ".saipen" / "MANIFEST.json").read_bytes()
        )


class TestIgnoredManifestRegression(SaipenGateCase):
    """Brief: the manifest is deliberately Git-ignored -> it is still collected
    for audit authority, while unrelated ignored `.saipen` runtime state and
    non-exportable surfaces stay excluded; `.gitignore` is not broadened."""

    def test_ignored_contract_and_evidence_still_authoritative(self):
        self.ignore(".saipen/*", ".venv/")
        self.commit()
        # Git really is hiding the memory root: that is the precondition.
        listed = subprocess.run(
            ["git", "ls-files", ".saipen"], cwd=str(self.repo), capture_output=True
        )
        self.assertEqual(listed.stdout.strip(), b"")
        # Unrelated ignored runtime state that must NOT leak.
        venv = self.repo / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("stub\n", encoding="utf-8")

        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        with self.archive() as zf:
            names = set(zf.namelist())
        # The contract and every declared tier survive Git invisibility.
        for rel in (
            ".saipen/MANIFEST.json",
            ".saipen/STATE.md",
            ".saipen/BOARD.md",
            ".saipen/LOG.md",
            ".saipen/IDENTITY.md",
            ".saipen/logs/LOG-001.md",
        ):
            self.assertIn(rel, names)
        # Non-exportable + undeclared runtime state stays excluded.
        for banned in (
            ".saipen/locks/core.lock",
            ".saipen/recovery/op.json",
            ".saipen/LOCAL_STATE.json",
            ".saipen/cache/runtime.bin",
            ".venv/pyvenv.cfg",
        ):
            self.assertNotIn(banned, names)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertTrue(snapshot["authoritative_state"])
        # The verdict reports the contract's own ban list exactly as the
        # document declares it (the consumer may not rewrite protocol paths).
        self.assertEqual(
            snapshot["non_exportable"],
            [
                ".saipen/locks",
                ".saipen/recovery",
                ".saipen/LOCAL_STATE.json",
            ],
        )


class TestNonSaipenUnaffected(SaipenGateCase):
    """The precondition costs nothing for projects without `.saipen/`."""

    def test_plain_project_packs_without_contract_or_gate(self):
        import shutil

        shutil.rmtree(self.repo / ".saipen")
        self.commit()
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        manifest = self.legacy_manifest()
        self.assertNotIn("saipen_snapshot", manifest)
        with self.archive() as zf:
            names = set(zf.namelist())
        self.assertFalse(any(n.startswith(".saipen/") for n in names))
        self.assertFalse((self.repo / ".saipen").exists())


class TestContractVersionGate(SaipenGateCase):
    """TARGET F/G/H: accept what the protocol writes today, refuse only what
    cannot be honoured, and never send the operator in a circle.

    The escaped regression was exactly this: a manifest generated by the
    current protocol declared contract_version 2, this consumer implemented 1,
    and Project Room packing failed with PROTOCOL_CONTRACT_UNKNOWN -- with a
    remediation (``saipen audit manifest --write``) that could not repair
    anything, because the installed protocol writes that same newer contract.
    """

    def test_case_v2_contract_passes_the_gate_without_regeneration(self):
        self.install_contract(CONTRACT_V2)
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertFalse(gate.generated, gate.detail)
        self.assertEqual(gate.code, se.STATUS_COMPLETE)
        # A current manifest must never be rewritten, least of all by a CLI
        # whose contract version could differ from the document's.
        runner.assert_not_called()

    def test_case_v1_canonical_contract_still_passes_the_gate(self):
        """TARGET D: older projects are not forced to regenerate."""
        self.install_contract(CONTRACT_V1_CANONICAL)
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertFalse(gate.generated, gate.detail)
        runner.assert_not_called()

    def test_case_v2_pack_produces_an_authoritative_archive(self):
        self.install_contract(CONTRACT_V2)
        self.commit()
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        with self.archive() as zf:
            names = set(zf.namelist())
        self.assertIn(".saipen/MANIFEST.json", names)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertEqual(snapshot["status"], se.STATUS_COMPLETE)
        self.assertTrue(snapshot["authoritative_state"])
        self.assertEqual(snapshot["contract_version"], 2)
        self.assertEqual(snapshot["omitted_required"], [])
        # v2's added exclusion is enforced, not merely recorded.
        self.assertNotIn(".saipen/LOCAL_STATE.json", names)

    def test_case_stale_v1_regenerated_as_v2_is_accepted(self):
        """Item 13: the CLI upgrades v1 -> v2; the result must be accepted.

        A stale v1 manifest is regenerated by the CURRENT protocol install,
        which writes v2. Refusing the regenerated document would leave such a
        project permanently un-packable, which is the defect that escaped.
        """
        self.install_contract(CONTRACT)
        self.stub_writes(CONTRACT_V2)
        self.age_manifest()
        self.commit()
        gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertTrue(gate.generated, gate.detail)
        on_disk = json.loads(
            (self.repo / ".saipen" / "MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["contract_version"], 2)
        self.assertEqual(on_disk["generator"], "saipen-audit-manifest/2")
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertEqual(snapshot["contract_version"], 2)
        self.assertTrue(snapshot["authoritative_state"])

    def test_case_v3_forward_compatible_passes_degraded(self):
        """MILESTONE K Case 2/3: newer contract with all required capabilities.

        A v3 manifest is newer than implemented (v2) but structurally carries
        every capability AUDAPACK depends on -- the same `required`, `mandatory`,
        `conditional`, `optional`, `non_exportable`, `references` shape, plus
        additive metadata. PACK must proceed.
        """
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        self.install_contract(future)
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertFalse(gate.generated, gate.detail)
        self.assertEqual(gate.code, se.STATUS_COMPATIBLE_DEGRADED)
        # A current manifest must never be rewritten.
        runner.assert_not_called()

    def test_case_v3_pack_produces_authoritative_archive(self):
        """Case 3: safe degraded PACK succeeds even for a newer contract."""
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        self.install_contract(future)
        self.commit()
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        with self.archive() as zf:
            names = set(zf.namelist())
        self.assertIn(".saipen/MANIFEST.json", names)
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertTrue(snapshot["authoritative_state"])
        self.assertEqual(snapshot["contract_version"], 3)
        # The v3 document is preserved on disk and in the archive unchanged.
        on_disk = json.loads(
            (self.repo / ".saipen" / "MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["contract_version"], 3)
        self.assertEqual(on_disk["generator"], "saipen-audit-manifest/3")
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertEqual(snapshot["admission"], "COMPATIBLE_DEGRADED")

    def test_case_v3_non_exportable_ban_enforced(self):
        """Case 2: v3's non_exportable surfaces are still refused."""
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        self.install_contract(future)
        self.commit()
        res = self.pack()
        self.assertTrue(res.success, res.error_message)
        with self.archive() as zf:
            names = set(zf.namelist())
        # Quarantine is banned in v2 baseline and carries to degraded v3.
        self.assertNotIn(".saipen/LOCAL_STATE.json", names)

    def test_case_v3_with_unknown_security_semantic_fails_closed(self):
        """Case 4: newer contract with unknown security semantics.

        A v3 document that moves ``non_exportable`` from a list of strings to
        objects (a shape this consumer cannot interpret) is INCOMPATIBLE_UNSAFE
        and must hard-block, with the exact missing capability reported.
        """
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        future["evidence"]["non_exportable"] = [
            {"path": "locks", "kind": "dir"},
        ]
        self.install_contract(future)
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.code, se.STATUS_CONTRACT_UNKNOWN)
        self.assertIn("credential_protection_rules_understood", gate.detail)
        self.assertIn("INCOMPATIBLE_UNSAFE", gate.detail)
        runner.assert_not_called()

    def test_case_v3_future_optional_metadata_passthrough(self):
        """Case 6: future optional fields do not cause regression."""
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        # Additive unknown fields that v3 introduced.
        future["evidence"]["nested_instance_files"] = ["STATE.md", "BOARD.md"]
        future["evidence"]["non_exportable_filenames"] = ["LOCAL_STATE.json"]
        future["evidence"]["non_exportable_segments"] = [".cache"]
        future["evidence"]["non_exportable_suffixes"] = [".tmp"]
        future["evidence"]["precedence"] = ["declared_durable_path"]
        future["schema_version"] = 1
        self.install_contract(future)
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertTrue(gate.ok, gate.detail)
        self.assertEqual(gate.code, se.STATUS_COMPATIBLE_DEGRADED)
        runner.assert_not_called()

    def test_case_pack_degraded_v3_message_distinguishes_skew_from_unsafe(self):
        """MILESTONE K UI: distinguish version skew from genuine incompatibility."""
        future = json.loads(json.dumps(CONTRACT_V2))
        future["contract_version"] = 3
        future["generator"] = "saipen-audit-manifest/3"
        self.install_contract(future)
        self.commit()
        with mock.patch.object(sg, "run_hidden") as runner:
            res = self.pack()
        self.assertTrue(res.success, res.error_message)
        # The package succeeds; the degraded admission is recorded in the snapshot.
        snapshot = self.legacy_manifest()["saipen_snapshot"]
        self.assertEqual(snapshot["admission"], "COMPATIBLE_DEGRADED")
        self.assertTrue(snapshot["authoritative_state"])
        runner.assert_not_called()

    def test_case_missing_manifest_is_generated_through_the_real_cli(self):
        (self.repo / ".saipen" / "MANIFEST.json").write_text(
            "{not json\n", encoding="utf-8"
        )
        with mock.patch.object(sg, "run_hidden") as runner:
            gate = sg.prepare(self.repo)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.code, se.STATUS_MANIFEST_MALFORMED)
        self.assertIn("not regenerating", gate.detail)
        runner.assert_not_called()

    def test_case_generator_identity_must_match_the_declared_version(self):
        """The protocol writes contract and generator version together.

        A document claiming one shape's rules under another shape's provenance
        is not the published contract, so the gate refuses it rather than
        trusting whichever half it prefers.
        """
        for generator, expected in (
            ("saipen-audit-manifest/1", "does not match"),
            ("saipen-audit-manifest/5", "does not match"),
            ("not-a-saipen-generator/2", "is not a saipen-audit-manifest generator"),
        ):
            with self.subTest(generator=generator):
                document = json.loads(json.dumps(CONTRACT_V2))
                document["generator"] = generator
                self.install_contract(document)
                gate = sg.prepare(self.repo)
                self.assertFalse(gate.ok)
                self.assertEqual(gate.code, se.STATUS_MANIFEST_MALFORMED)
                self.assertIn(expected, gate.detail)


if __name__ == "__main__":
    unittest.main()
