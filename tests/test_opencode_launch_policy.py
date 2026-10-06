import json
import os
import subprocess
from pathlib import Path

import pytest

from audapack.instances import LaunchRecord
from audapack.opencode_launch import (
    LaunchAdmissionError,
    OpenCodeLaunchPolicy,
    admit_with_fallback,
)
from audapack.saipen_transport import SaipenTransportError


def _fleet_payload(root: Path, **overrides) -> dict:
    """The canonical shape saipen fleet preflight --json actually emits."""
    payload = {
        "classification": "BOUND_VALID",
        "root": str(root),
        "project_identity": os.path.normcase(os.path.realpath(root)),
        "project_lineage": "test-lineage",
        "canonical_actor": "buffy",
        "provenance": "state",
        "reason_code": "CLEAN",
        "reason": "canonical protocol state is valid",
        "read_only": True,
    }
    payload.update(overrides)
    return payload


def _managed_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    contract = root / ".saipen"
    contract.mkdir()
    home = root / "bound-home"
    entry = home / "tools" / "saipen.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# bound SAIPEN CLI\n", encoding="utf-8")
    (contract / "STATE.md").write_text(
        f"---\nsaipen_home: {home}\n---\n", encoding="utf-8"
    )
    (contract / "IDENTITY.md").write_text(
        "---\nproject_lineage: test-lineage\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("audapack.opencode_launch.bound_python", lambda: Path(__file__))
    monkeypatch.setattr("audapack.opencode_launch.shutil.which", lambda _name: "opencode.cmd")
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root)), ""
        ),
    )
    return root


def test_managed_project_selects_canonical_bound_launch(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    policy = OpenCodeLaunchPolicy()

    admission = policy.admit(root, custom_template=None)

    assert admission.managed is True
    assert admission.cwd == root.resolve()
    assert admission.command == (
        str(Path(__file__)),
        str(root / "bound-home" / "tools" / "saipen.py"),
        "--agent",
        "buffy",
        "--project-root",
        str(root.resolve()),
        "launch",
        "opencode",
        "--",
        ".",
        "--auto",
    )
    assert admission.recovery_diagnostic is None  # BOUND_VALID has no recovery diagnostic


def test_ordinary_project_preserves_legacy_launch(tmp_path: Path) -> None:
    policy = OpenCodeLaunchPolicy()

    admission = policy.admit(tmp_path, custom_template=None)

    assert admission.managed is False
    assert admission.cwd == tmp_path.resolve()
    assert admission.command is None


def test_managed_project_without_saipen_launcher_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    (root / "bound-home" / "tools" / "saipen.py").unlink()
    policy = OpenCodeLaunchPolicy()

    with pytest.raises(LaunchAdmissionError, match="SAIPEN launcher unavailable"):
        policy.admit(root, custom_template=None)


def test_managed_custom_template_cannot_bypass_saipen(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    policy = OpenCodeLaunchPolicy()

    with pytest.raises(LaunchAdmissionError, match="custom OpenCode template"):
        policy.admit(root, custom_template="opencode.cmd {path}")


def test_actor_identity_comes_from_fleet_and_is_not_host_derived(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    policy = OpenCodeLaunchPolicy()

    admission = policy.admit(root, custom_template=None)

    assert admission.command[2:4] == ("--agent", "buffy")
    assert "provider" not in " ".join(admission.command).lower()
    assert admission.binding is not None
    assert admission.binding["actor"] == "buffy"
    assert "opencode" not in admission.command[3]


def test_missing_or_invalid_canonical_actor_fails_before_host_spawn(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    for actor in (None, "", "bad seat"):
        monkeypatch.setattr(
            "audapack.opencode_launch.run_hidden",
            lambda *args, _actor=actor, **kwargs: subprocess.CompletedProcess(
                args[0], 0, json.dumps(_fleet_payload(root, canonical_actor=_actor)), ""
            ),
        )
        with pytest.raises(LaunchAdmissionError, match="canonical_actor"):
            OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_host_identity_actor_admits_generic_bound_launch(tmp_path: Path, monkeypatch) -> None:
    """When canonical_actor is a host identity ('opencode'), SAIPEN forbids explicit
    '--agent opencode' launch; OpenCodeLaunchPolicy must admit generic launch with
    OpenCode directly, preserving managed binding, title guard, and liveness."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root, canonical_actor="opencode")), ""
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.cwd == root.resolve()
    assert admission.command == ("opencode.cmd", ".", "--auto")
    assert admission.binding is not None
    assert admission.binding["actor"] == "opencode"
    assert admission.binding["project_identity"] == os.path.normcase(os.path.realpath(root))


def test_missing_required_fleet_binding_facts_fail_closed(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    for field in ("project_identity", "project_lineage", "canonical_actor"):
        payload = _fleet_payload(root)
        payload.pop(field)
        monkeypatch.setattr(
            "audapack.opencode_launch.run_hidden",
            lambda *args, _payload=payload, **kwargs: subprocess.CompletedProcess(
                args[0], 0, json.dumps(_payload), ""
            ),
        )
        with pytest.raises(LaunchAdmissionError, match="project_identity|project_lineage|canonical_actor"):
            OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_fleet_recoverable_blocked_admits_with_diagnostic(tmp_path: Path, monkeypatch) -> None:
    """BOUND_RECOVERY_REQUIRED_BLOCKED is a recovery/readiness state, not a host-admission denial.
    A bound host can still launch for diagnosis/recovery; the SAIPEN guard controls
    consequential operations once the host is up (Target A, Target C)."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(_fleet_payload(root, classification="BOUND_RECOVERY_REQUIRED_BLOCKED", reason_code="RECOVERY_REQUIRED", reason="pending journal", canonical_next_command="saipen recover")), ""),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.recovery_diagnostic is not None
    assert admission.recovery_diagnostic["classification"] == "BOUND_RECOVERY_REQUIRED_BLOCKED"
    assert admission.recovery_diagnostic["reason_code"] == "RECOVERY_REQUIRED"
    assert admission.recovery_diagnostic["canonical_next_command"] == "saipen recover"


def test_wrong_project_preflight_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root, project_identity="another-project")), ""
        ),
    )
    with pytest.raises(LaunchAdmissionError, match="SAIPEN Fleet preflight refused"):
        OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_fleet_command_is_preflight_with_require_binding_and_host_root(tmp_path: Path, monkeypatch) -> None:
    """Admission must be the read-only Fleet classifier, never `saipen status`.

    SRC-048: status can be ok while Fleet classifies
    BOUND_RECOVERY_REQUIRED_BLOCKED -- a known protocol blocker that would burn
    model budget for zero consequential capability.
    """
    root = _managed_project(tmp_path, monkeypatch)
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append([str(arg) for arg in args])
        return subprocess.CompletedProcess(args, 0, json.dumps(_fleet_payload(root)), "")

    monkeypatch.setattr("audapack.opencode_launch.run_hidden", fake_run)
    OpenCodeLaunchPolicy().admit(root, custom_template=None)

    argv = " ".join(captured[0])
    assert "status" not in argv.split("--json")[0].replace("--json", "")
    assert "fleet preflight" in argv
    assert "--require-binding" in argv
    assert f"--host-root {root}" in argv
    assert "prepare" not in argv  # a click must stay read-only
    assert "--json" in argv


def test_fleet_preflight_owns_its_stdin(tmp_path: Path, monkeypatch) -> None:
    """T-210 TARGET G: the machine-readable preflight owns its stdio.

    The escaped T-209 regression stranded AUDAPACK's inherited standard handles,
    and this exact child was where it surfaced as
    ``[WinError 6] The handle is invalid``. A JSON preflight is never
    interactive, so it must never depend on an inherited stdin handle.
    """
    root = _managed_project(tmp_path, monkeypatch)
    captured: list[dict] = []

    def fake_run(args, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps(_fleet_payload(root)), "")

    monkeypatch.setattr("audapack.opencode_launch.run_hidden", fake_run)
    OpenCodeLaunchPolicy().admit(root, custom_template=None)

    assert captured, "the preflight never ran"
    assert captured[0].get("stdin") == subprocess.DEVNULL
    assert captured[0].get("capture_output") is True


def test_fleet_recoverable_safe_admits_with_diagnostic(tmp_path: Path, monkeypatch) -> None:
    """BOUND_RECOVERY_REQUIRED_SAFE is a recovery/readiness state, not a host-admission denial.
    A bound host can launch; SAIPEN can auto-repair but a launcher click must NOT
    (Target B -- no canonical mutation on launch). Recovery remains owned by SAIPEN
    and the acting agent/operator (SRC-048)."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0,
            json.dumps(_fleet_payload(
                root, classification="BOUND_RECOVERY_REQUIRED_SAFE", reason_code="BOARD_RECORD_OVERSIZE",
                reason="legacy oversized BOARD record(s) require canonical compaction",
                canonical_next_command="saipen ticket compact T-158",
            )),
            "",
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.recovery_diagnostic is not None
    assert admission.recovery_diagnostic["classification"] == "BOUND_RECOVERY_REQUIRED_SAFE"
    assert admission.recovery_diagnostic["reason_code"] == "BOARD_RECORD_OVERSIZE"
    assert admission.recovery_diagnostic["canonical_next_command"] == "saipen ticket compact T-158"


def test_board_record_oversize_does_not_block_host_launch(tmp_path: Path, monkeypatch) -> None:
    """BOARD_RECORD_OVERSIZE is an inner reason, not a classification.
    A BOUND_RECOVERY_REQUIRED_SAFE project with BOARD_RECORD_OVERSIZE must launch
    the bound host -- the recovery command remains reachable from inside OpenCode
    (Target E clause 3)."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0,
            json.dumps(_fleet_payload(
                root,
                classification="BOUND_RECOVERY_REQUIRED_SAFE",
                reason_code="BOARD_RECORD_OVERSIZE",
                reason="legacy oversized BOARD record(s) require canonical compaction: T-158, T-012",
                canonical_next_command="saipen ticket compact T-158",
            )),
            "",
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.binding is not None
    assert admission.recovery_diagnostic["reason_code"] == "BOARD_RECORD_OVERSIZE"


def test_fleet_binding_conflict_and_unbound_are_refused(tmp_path: Path, monkeypatch) -> None:
    """BINDING_CONFLICT and UNBOUND remain fail-closed. BOUND_RECOVERY_REQUIRED_BLOCKED
    is NOT a binding failure and is admitted (see test_fleet_recoverable_blocked_admits_with_diagnostic)."""
    root = _managed_project(tmp_path, monkeypatch)
    for classification in ("BINDING_CONFLICT", "UNBOUND"):
        def fake_run(*args, _c=classification, **kwargs):
            return subprocess.CompletedProcess(
                args[0], 0,
                json.dumps(_fleet_payload(
                    root, classification=_c,
                    reason_code="PROJECT_BINDING_INVALID",
                    reason="synthetic",
                    canonical_next_command="saipen recover" if _c != "UNBOUND" else None,
                )),
                "",
            )
        monkeypatch.setattr("audapack.opencode_launch.run_hidden", fake_run)
        with pytest.raises(LaunchAdmissionError, match=classification):
            OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_fleet_suggested_command_is_surfaced_in_diagnostic(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0,
            json.dumps(_fleet_payload(
                root, classification="BOUND_RECOVERY_REQUIRED_BLOCKED", reason_code="RECOVERY_REQUIRED",
                reason="pending journal", canonical_next_command="saipen recover",
            )),
            "",
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.recovery_diagnostic is not None
    assert admission.recovery_diagnostic["canonical_next_command"] == "saipen recover"
    assert len(str(admission.recovery_diagnostic)) < 600  # bounded, actionable -- not raw JSON


def test_malformed_fleet_output_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    for stdout in ("", "not json", '{"ok": true}'):
        def fake_run(*args, _out=stdout, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, _out, "")
        monkeypatch.setattr(
            "audapack.opencode_launch.run_hidden",
            fake_run,
        )
        with pytest.raises(LaunchAdmissionError, match="Fleet preflight"):
            OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_mutating_fleet_result_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A preflight that no longer promises read-only must fail the launch."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root, read_only=False)), ""
        ),
    )
    with pytest.raises(LaunchAdmissionError, match="read-only"):
        OpenCodeLaunchPolicy().admit(root, custom_template=None)


def test_fleet_timeout_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="saipen", timeout=30)

    monkeypatch.setattr("audapack.opencode_launch.run_hidden", slow)
    with pytest.raises(LaunchAdmissionError, match="Fleet preflight failed"):
        OpenCodeLaunchPolicy().admit(root, custom_template=None)


# ---------------------------------------------------------------------------
# TARGET B -- canonical binding evidence
# ---------------------------------------------------------------------------

def test_binding_evidence_is_canonical_facts_not_raw_identity_body(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)

    binding = admission.binding
    assert binding is not None
    assert set(binding) == {
        "kind", "project_root", "entrypoint", "project_identity", "project_lineage", "actor",
    }
    assert binding["actor"] == "buffy"
    assert binding["project_lineage"] == "test-lineage"
    assert binding["project_identity"] == os.path.normcase(os.path.realpath(root))


def test_identity_comment_edits_do_not_invalidate_reuse(tmp_path: Path, monkeypatch) -> None:
    """Harmless IDENTITY.md formatting/comment changes must not break reuse."""
    root = _managed_project(tmp_path, monkeypatch)
    before = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert before is not None

    identity = root / ".saipen" / "IDENTITY.md"
    identity.write_text(
        "---\nproject_lineage: test-lineage\n---\n"
        "# Reformatted header\n<!-- a brand-new comment nobody needs -->\n",
        encoding="utf-8",
    )

    after = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert after is not None
    # Same canonical lineage -> same compatibility evidence -> reuse works.
    assert {k: v for k, v in after.items() if k != "project_root"} == \
        {k: v for k, v in before.items() if k != "project_root"}


def test_changed_lineage_is_incompatible_for_reuse(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    before = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert before is not None

    identity = root / ".saipen" / "IDENTITY.md"
    identity.write_text("---\nproject_lineage: a-different-lineage\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root, project_lineage="a-different-lineage")), ""
        ),
    )
    after = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert after is not None
    assert after["project_lineage"] != before["project_lineage"]
    assert after != before  # an existing window under the old lineage cannot satisfy reuse


def test_changed_fleet_actor_is_incompatible_for_reuse(tmp_path: Path, monkeypatch) -> None:
    root = _managed_project(tmp_path, monkeypatch)
    before = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert before is not None
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root, canonical_actor="another-seat")), ""
        ),
    )
    after = OpenCodeLaunchPolicy().admit(root, custom_template=None).binding
    assert after is not None
    assert after["actor"] == "another-seat"
    assert after != before


def test_legacy_launch_record_deserializes_without_binding() -> None:
    record = LaunchRecord.from_dict({
        "pid": 42, "launcher_id": "opencode", "project_id": "p", "project_name": "P",
        "project_path": "V:/p", "started_at": "2026-09-14T00:00:00Z",
    })
    assert record is not None
    assert record.saipen_binding is None


# ---------------------------------------------------------------------------
# TARGET E -- additional regression coverage
# ---------------------------------------------------------------------------


def test_launcher_click_invokes_preflight_not_prepare(tmp_path: Path, monkeypatch) -> None:
    """A click must be read-only: fleet preflight, never fleet prepare.
    TARGET B: no canonical mutation on launch (SRC-048)."""
    root = _managed_project(tmp_path, monkeypatch)
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append([str(arg) for arg in args])
        return subprocess.CompletedProcess(args, 0, json.dumps(_fleet_payload(root)), "")

    monkeypatch.setattr("audapack.opencode_launch.run_hidden", fake_run)
    OpenCodeLaunchPolicy().admit(root, custom_template=None)

    argv = " ".join(captured[0])
    assert "fleet preflight" in argv
    assert "prepare" not in argv  # a click must stay read-only, never mutate


def test_recovery_admission_preserves_command_and_binding(tmp_path: Path, monkeypatch) -> None:
    """For an admitted recovery-state launch, the command and binding are identical
    to a normal BOUND_VALID launch -- only the diagnostic differs (Target A/C)."""
    root = _managed_project(tmp_path, monkeypatch)
    expected_cmd = (
        str(Path(__file__)),
        str(root / "bound-home" / "tools" / "saipen.py"),
        "--agent", "buffy",
        "--project-root", str(root.resolve()),
        "launch", "opencode", "--", ".", "--auto",
    )

    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0,
            json.dumps(_fleet_payload(
                root, classification="BOUND_RECOVERY_REQUIRED_SAFE",
                reason_code="BOARD_RECORD_OVERSIZE",
                reason="compact required",
                canonical_next_command="saipen ticket compact T-158",
            )), "",
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.command == expected_cmd
    assert admission.binding is not None
    assert admission.binding["actor"] == "buffy"
    assert admission.binding["project_lineage"] == "test-lineage"
    assert admission.recovery_diagnostic is not None


def test_safe_recovery_diagnostic_can_omit_next_command(tmp_path: Path, monkeypatch) -> None:
    """When Fleet provides no canonical_next_command, recovery_diagnostic still
    surfaces classification/reason_code/reason without crashing."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(
                root, classification="BOUND_RECOVERY_REQUIRED_BLOCKED",
                reason_code="PENDING_DECISION", reason="awaiting operator input",
                canonical_next_command=None,
            )), "",
        ),
    )
    admission = OpenCodeLaunchPolicy().admit(root, custom_template=None)
    assert admission.managed is True
    assert admission.recovery_diagnostic is not None
    assert "canonical_next_command" not in admission.recovery_diagnostic
    assert admission.recovery_diagnostic["classification"] == "BOUND_RECOVERY_REQUIRED_BLOCKED"


# ---------------------------------------------------------------------------
# TARGET B/B (SRC-082) -- MANAGED_UNAVAILABLE_BUT_DIRECT_SAFE fail-open
# ---------------------------------------------------------------------------


def _broken_managed_project(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed project whose Fleet preflight raises a recoverable binding error."""
    contract = root / ".saipen"
    contract.mkdir()
    home = root / "bound-home"
    entry = home / "tools" / "saipen.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# bound SAIPEN CLI\n", encoding="utf-8")
    (contract / "STATE.md").write_text(
        f"---\nsaipen_home: {home}\n---\n", encoding="utf-8"
    )
    (contract / "IDENTITY.md").write_text(
        "---\nproject_lineage: test-lineage\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr("audapack.opencode_launch.bound_python", lambda: Path(__file__))
    monkeypatch.setattr("audapack.opencode_launch.shutil.which", lambda _name: "opencode.cmd")
    return root


def test_malformed_binding_with_valid_root_admits_degraded(tmp_path, monkeypatch):
    """A malformed SAIPEN metadata file alone is NOT a hard block when the
    physical project root and OpenCode executable are both resolvable."""
    root = _broken_managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(SaipenTransportError("Invalid \\escape in binding")),
    )
    admission = admit_with_fallback(root, custom_template=None, known_display_name="Proj A")
    assert admission.managed is True
    assert admission.degraded is True
    assert admission.command == ("opencode.cmd", ".", "--auto")
    assert admission.binding is None
    assert admission.recovery_diagnostic is not None
    assert admission.recovery_diagnostic["classification"] == "DEGRADED_DIRECT_SAFE"
    assert "escape" in admission.recovery_diagnostic["reason"].lower()
    assert admission.recovery_diagnostic["project_name"] == "Proj A"


def test_malformed_binding_missing_opencode_executable_fails_closed(tmp_path, monkeypatch):
    """Physical fact: opencode must be resolvable. No executable -> hard block."""
    root = _broken_managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr("audapack.opencode_launch.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(SaipenTransportError("Invalid binding")),
    )
    with pytest.raises(LaunchAdmissionError, match="OpenCode executable unavailable"):
        admit_with_fallback(root, custom_template=None)


def test_malformed_binding_missing_root_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Physical fact: project root must exist. Missing dir -> hard block."""
    root = _broken_managed_project(tmp_path, monkeypatch)
    sub = root / "does-not-exist"
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(SaipenTransportError("Invalid binding")),
    )
    with pytest.raises(LaunchAdmissionError, match="Project source directory unavailable"):
        admit_with_fallback(sub, custom_template=None)


def test_non_recoverable_fleet_refusal_stays_fail_closed(tmp_path, monkeypatch):
    """A BINDING_CONFLICT is not a recoverable metadata defect -> hard block."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0,
            json.dumps(_fleet_payload(
                root, classification="BINDING_CONFLICT",
                reason_code="PROJECT_BINDING_INVALID", reason="synthetic",
            )),
            "",
        ),
    )
    with pytest.raises(LaunchAdmissionError, match="BINDING_CONFLICT"):
        admit_with_fallback(root, custom_template=None, known_display_name="Proj")


def test_degraded_still_uses_canonical_project_root(tmp_path, monkeypatch):
    """Degraded launch must cwd into the canonical project root, never guess."""
    root = tmp_path
    (root / ".saipen").mkdir()
    (root / ".saipen" / "IDENTITY.md").write_text("---\nproject_lineage: test\n---\n", encoding="utf-8")
    monkeypatch.setattr("audapack.opencode_launch.bound_python", lambda: Path(__file__))
    monkeypatch.setattr("audapack.opencode_launch.shutil.which", lambda _name: "opencode.cmd")
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(SaipenTransportError("Invalid \\escape")),
    )
    admission = admit_with_fallback(root, custom_template=None)
    assert admission.cwd == root.resolve()


def test_managed_launch_still_admits_after_repair(tmp_path, monkeypatch):
    """Second launch with a healed binding returns to MANAGED, not DEGRADED."""
    root = _managed_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audapack.opencode_launch.run_hidden",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(_fleet_payload(root)), ""
        ),
    )
    admission = admit_with_fallback(root, custom_template=None)
    assert admission.managed is True
    assert admission.degraded is False
    assert admission.binding is not None
