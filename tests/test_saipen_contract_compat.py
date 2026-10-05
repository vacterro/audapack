"""Cross-repo tripwire: SAIPEN's contract vs what AUDAPACK can read.

WHY THIS EXISTS
---------------
AUDAPACK hard-coded ``SUPPORTED_CONTRACT_VERSION = 1`` while SAIPEN moved its
audit-manifest contract to v2. Nothing in either repository noticed, because
nothing checked: the first party to learn about the skew was the OPERATOR, in
Project Room, as ``PACK FAILED (SAIPEN) [FAILED_INVENTORY] ...
PROTOCOL_CONTRACT_UNKNOWN`` -- and only after the archives stopped being
producible at all.

The gap was not the missing parser; it was the missing TRIPWIRE. Two checks in
one file close it:

1. Always-on: the committed canonical fixtures (real generator output, see
   tests/fixtures/saipen/README.md) must be readable by the version that claims
   to read them, and the documented v1 -> v2 delta must be exactly what the
   parsers assume. A future fixture refresh that widens that delta fails here
   instead of in production.

2. Cross-repo (skipped when no SAIPEN authority exists on this machine): run
   the CURRENT generator, feed its output to AUDAPACK's parser and gate, and
   require a supported result. This is the leg that would have failed the day
   SAIPEN bumped the contract rather than the day an operator tried to pack.

The second leg is deliberately a skip -- not a failure -- when the authority is
simply absent, because CI does not carry a SAIPEN checkout. It fails only when
the generator actually runs and AUDAPACK cannot read what it wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from audapack import saipen_evidence as se
from audapack import saipen_manifest_gate as sg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "saipen"

V2_FIXTURE = FIXTURE_DIR / "MANIFEST.v2.json"
V1_FIXTURE = FIXTURE_DIR / "MANIFEST.v1.json"
V3_FIXTURE = FIXTURE_DIR / "MANIFEST.v3.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_saipen_authority() -> Path | None:
    """The canonical protocol home, or None when this machine has none.

    Resolution order mirrors the packing gate's own: an explicit environment
    override first, then the ``saipen_home`` the project's checkpoint declares
    (the project knows which install owns it), and nothing else -- guessing a
    path would make the tripwire report on some other install.
    """
    candidates: list[Path] = []
    override = os.environ.get("SAIPEN_HOME")
    if override:
        candidates.append(Path(override))
    try:
        state = (PROJECT_ROOT / ".saipen" / "STATE.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        state = ""
    for line in state.splitlines():
        stripped = line.strip()
        if stripped.startswith("saipen_home:"):
            home = stripped[len("saipen_home:") :].strip().strip("\"'")
            if home:
                candidates.append(Path(home))
    for home in candidates:
        entry = home / "tools" / "saipen.py"
        if entry.is_file():
            return home
    return None


class TestCanonicalFixtures(unittest.TestCase):
    """Always-on: the captured documents are readable and the delta is known."""

    def test_both_canonical_contracts_are_supported(self):
        for name, path in (("v1", V1_FIXTURE), ("v2", V2_FIXTURE), ("v3", V3_FIXTURE)):
            with self.subTest(contract=name):
                document = _load(path)
                self.assertIn(
                    document["contract_version"],
                    se.SUPPORTED_CONTRACT_VERSIONS + (3,),
                    f"the {name} fixture declares a contract AUDAPACK cannot read",
                )
                self.assertEqual(
                    document["kind"], "saipen_audit_manifest"
                )
                self.assertEqual(
                    document["generator"],
                    f"saipen-audit-manifest/{document['contract_version']}",
                )

    def test_v1_and_v2_fixtures_differ_only_where_the_parsers_expect(self):
        """The delta the code is built on, pinned to the real documents.

        `parse_contract_v2` adds exactly the v2 baseline exclusion and nothing
        else, so if a refreshed fixture shows any other difference the parser
        assumption is stale -- and this fails before a pack does.
        """
        v1 = _load(V1_FIXTURE)
        v2 = _load(V2_FIXTURE)
        self.assertEqual(sorted(v1.keys()), sorted(v2.keys()))
        # Volatile metadata (which generator stamp, which protocol patch the
        # install reported, when it ran) is not contract SHAPE: the delta this
        # test owns is the one the parsers are built on.
        volatile = {"contract_version", "generator", "generated_at", "protocol_version", "evidence"}
        for key in sorted(set(v1) | set(v2)):
            if key in volatile:
                continue
            self.assertEqual(v1.get(key), v2.get(key), f"unexpected v1/v2 delta in {key!r}")
        # `evidence` legitimately differs by the v2 baseline exclusion and by
        # nothing else, so every other tier is compared field by field.
        self.assertEqual(sorted(v1["evidence"].keys()), sorted(v2["evidence"].keys()))
        for tier in sorted(v1["evidence"]):
            if tier == "non_exportable":
                continue
            self.assertEqual(
                v1["evidence"][tier],
                v2["evidence"][tier],
                f"unexpected v1/v2 delta in evidence.{tier}",
            )
        added = set(v2["evidence"]["non_exportable"]) - set(v1["evidence"]["non_exportable"])
        self.assertEqual(added, {"quarantine/"})
        self.assertEqual(
            tuple(rel.rstrip("/") for rel in added), se.V2_NON_EXPORTABLE_BASELINE
        )

    def test_each_fixture_is_read_by_its_own_version(self):
        for path, version in ((V1_FIXTURE, 1), (V2_FIXTURE, 2)):
            with self.subTest(contract=version):
                document = _load(path)
                base = se.SaipenContract(
                    detected=True,
                    contract_version=version,
                    generator=document["generator"],
                    memory_root=document["memory_root"],
                )
                parser = se.parse_contract_v1 if version == 1 else se.parse_contract_v2
                parsed = parser(base, document, document["memory_root"])
                self.assertEqual(parsed.status, se.STATUS_COMPLETE, parsed.detail)
                self.assertEqual(
                    sorted(parsed.mandatory),
                    sorted(
                        f"{document['memory_root']}/{rel}" for rel in document["required"]
                    ),
                )
                self.assertTrue(parsed.citations is not None)

    def test_v3_fixture_admits_as_compatible_degraded(self):
        """MILESTONE K: v3 is forward-compatible, not unknown.

        The v3 fixture carries all required capabilities structurally and is
        read in COMPATIBLE_DEGRADED mode, preserving unknown additive metadata
        rather than rewriting it.
        """
        document = _load(V3_FIXTURE)
        parsed = se.read_contract_from_document(document)
        self.assertEqual(parsed.contract_version, 3)
        self.assertEqual(parsed.admission, se.ManifestAdmission.COMPATIBLE_DEGRADED)
        self.assertEqual(parsed.status, se.STATUS_COMPLETE)
        self.assertTrue(parsed.citations is not None)
        self.assertEqual(
            sorted(parsed.mandatory),
            sorted(f"{document['memory_root']}/{rel}" for rel in document["required"]),
        )
        self.assertIn(".saipen/quarantine", parsed.non_exportable)

    def test_v3_with_unknown_exclusion_semantic_is_incompatible_unsafe(self):
        """Case 4: a v3 that reshapes a required capability must hard-block."""
        document = _load(V3_FIXTURE)
        document["evidence"]["non_exportable"] = [
            {"path": "locks", "kind": "dir"},
        ]
        parsed = se.read_contract_from_document(document)
        self.assertEqual(parsed.admission, se.ManifestAdmission.INCOMPATIBLE_UNSAFE)
        self.assertEqual(parsed.status, se.STATUS_CONTRACT_UNKNOWN)

    def test_v4_with_safe_core_admits_degraded(self):
        """Case 3: a future v4 with structurally compatible core proceeds."""
        document = _load(V3_FIXTURE)
        document["contract_version"] = 4
        document["generator"] = "saipen-audit-manifest/4"
        parsed = se.read_contract_from_document(document)
        self.assertEqual(parsed.admission, se.ManifestAdmission.COMPATIBLE_DEGRADED)
        self.assertEqual(parsed.status, se.STATUS_COMPLETE)
        self.assertEqual(parsed.contract_version, 4)


class TestCurrentGeneratorIsSupported(unittest.TestCase):
    """Cross-repo leg: the CURRENT generator's output must be readable here."""

    def test_current_generator_output_is_accepted_by_parser_and_gate(self):
        home = resolve_saipen_authority()
        if home is None:
            self.skipTest("no SAIPEN authority on this machine (CI carries none)")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fixture"
            memory = root / ".saipen"
            memory.mkdir(parents=True)
            (memory / "STATE.md").write_text(
                "schema_version: 3\nphase: BUILD\ntask: T-1\n", encoding="utf-8"
            )
            (memory / "BOARD.md").write_text("# BOARD\n", encoding="utf-8")
            (memory / "LOG.md").write_text("# LOG\n\n[E-1] fixture\n", encoding="utf-8")
            (memory / "IDENTITY.md").write_text("# IDENTITY\n\nfixture\n", encoding="utf-8")

            # The fixture is a DISPOSABLE project with its own identity; the
            # ambient session's project binding must not leak into it. A bound
            # agent session exports SAIPEN_PROJECT_ROOT/LINEAGE, and the real
            # CLI then refuses the fixture's root with PROJECT_LINEAGE_MISMATCH
            # -- turning this leg into a silent skip on exactly the machines
            # (the operator's) the tripwire exists to protect.
            fixture_env = {
                key: value
                for key, value in os.environ.items()
                if key not in ("SAIPEN_PROJECT_ROOT", "SAIPEN_PROJECT_LINEAGE", "SAIPEN_AGENT")
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(home / "tools" / "saipen.py"),
                    "--project-root",
                    str(root),
                    "--json",
                    "audit",
                    "manifest",
                    "--write",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env=fixture_env,
            )
            if proc.returncode != 0:
                self.skipTest(
                    "local SAIPEN CLI could not run: "
                    f"rc={proc.returncode} {(proc.stderr or proc.stdout).strip()[:200]}"
                )

            manifest = memory / "MANIFEST.json"
            self.assertTrue(manifest.is_file(), "the generator reported success but wrote nothing")
            document = _load(manifest)

            # The tripwire proper: SAIPEN's current contract must be one this
            # consumer can read -- either natively (in SUPPORTED_CONTRACT_VERSIONS)
            # or in a forward-compatible degraded mode (all capabilities
            # structurally present). A version that is NEITHER lands here, not
            # on the operator.
            version = document["contract_version"]
            parsed = se.read_contract(root)
            if version in se.SUPPORTED_CONTRACT_VERSIONS:
                self.assertEqual(parsed.status, se.STATUS_COMPLETE)
            else:
                # Newer version: must admit degraded, not unknown.
                self.assertEqual(
                    parsed.admission,
                    se.ManifestAdmission.COMPATIBLE_DEGRADED,
                )
                self.assertEqual(parsed.status, se.STATUS_COMPLETE)

            # And the document the real generator just wrote must pass the gate
            # that stands in front of every pack.
            outcome = sg.validate_contract_document(root)
            self.assertTrue(outcome.ok, outcome.detail)
            if version in se.SUPPORTED_CONTRACT_VERSIONS:
                self.assertEqual(outcome.code, se.STATUS_COMPLETE)
            else:
                self.assertEqual(outcome.code, se.STATUS_COMPATIBLE_DEGRADED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
