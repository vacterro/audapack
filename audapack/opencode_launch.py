"""Admission boundary for OpenCode launched from an AUDAPACK project row."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audapack.procutil import run_hidden
from audapack.saipen_transport import (
    SaipenTransportError,
    bound_entrypoint,
    bound_python,
    is_managed,
)

#: Fleet classifications that may produce a correctly bound OpenCode host process.
#: Admission here is about host-binding safety only: "can AUDAPACK safely start a
#: correctly bound OpenCode host for this project?" It is NOT a statement that the
#: entire SAIPEN protocol state is BOUND_VALID and ready for unrestricted work.
#:
#: BOUND_RECOVERY_REQUIRED_SAFE and BOUND_RECOVERY_REQUIRED_BLOCKED are recovery /
#: readiness states, not binding failures: root, lineage and actor are already
#: mechanically verified. Starting the bound host is not itself a canonical-state
#: mutation. The SAIPEN guard (not this launcher) still refuses protected / invalid
#: consequential operations once the host is up, and recovery remains owned by
#: SAIPEN and the acting agent/operator (SRC-048).
ADMITS_LAUNCH = frozenset({
    "BOUND_VALID",
    "BOUND_RECOVERY_REQUIRED_SAFE",
    "BOUND_RECOVERY_REQUIRED_BLOCKED",
})
_SAIPEN_SEAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_HOST_IDENTITIES = frozenset({"opencode"})


class LaunchAdmissionError(RuntimeError):
    """A managed launch cannot establish its canonical binding."""


class RecoverableLaunchError(RuntimeError):
    """A managed project binding is degraded but physical launch-safety is known."""

    def __init__(self, reason: str, *, degraded_root: Path) -> None:
        super().__init__(reason)
        self.degraded_root = degraded_root


@dataclass(frozen=True)
class OpenCodeAdmission:
    managed: bool
    cwd: Path
    command: tuple[str, ...] | None = None
    # Durable compatibility evidence, not authentication. SAIPEN itself owns
    # actor/root/lineage validation when the host process starts.
    binding: dict[str, str] | None = None
    # Bounded diagnostic facts for an admitted recovery-state launch so the UI can
    # surface a non-blocking status without re-running Fleet. Only populated for
    # BOUND_RECOVERY_REQUIRED_SAFE / BOUND_RECOVERY_REQUIRED_BLOCKED; None for a
    # normal BOUND_VALID launch.
    recovery_diagnostic: dict[str, str] | None = None
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def kind(self) -> str:
        if not self.managed and self.degraded:
            return "DEGRADED"
        if self.degraded and self.recovery_diagnostic is not None:
            return "DEGRADED"
        if self.managed:
            return "MANAGED"
        return "DIRECT"


def new_correlation_token() -> str:
    """One durable launch correlation token per bound host spawn.

    The launcher embeds it in the console title and the same value is stored on
    the LaunchRecord, so the window is attributable to its record mechanically,
    even when its visible PID differs from the parent PID and even when several
    bound instances of one project are live at once.
    """
    return f"OC-{secrets.token_hex(6)}"


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _diagnostic(payload: dict[str, Any]) -> str:
    """One bounded actionable line from the Fleet refusal, never raw JSON."""
    parts = [
        payload.get("classification", "INVALID_FLEET_OUTPUT"),
        _bounded(payload.get("reason_code", ""), 80),
        _bounded(payload.get("reason", ""), 200),
    ]
    command = payload.get("canonical_next_command")
    detail = " | ".join(part for part in parts if part)
    return f"{detail} (Fleet suggests: {command})" if command else detail


DEGRADED_SAFE_REASON_RE = re.compile(r"(?i)(invalid project saipen binding|invalid \\escape|SAIPEN launcher unavailable|SAIPEN binding|launcher metadata|escape)")
RECOVERY_SAFE_CLASSIFICATIONS = frozenset({
    "BOUND_RECOVERY_REQUIRED_SAFE",
    "BOUND_RECOVERY_REQUIRED_BLOCKED",
})


def _physical_launch_safe(root: Path) -> bool:
    return root.is_dir() and bool(shutil.which("opencode"))


def admit_with_fallback(
    project_root: Path | str,
    *,
    custom_template: str | None,
    known_display_name: str | None = None,
) -> OpenCodeAdmission:
    """Try the managed path; on recoverable SAIPEN-metadata failure, admit DEGRADED.

    The degraded admission is truthful: it reuses the canonical project root from
    Project Room, verifies physical safety facts (directory + executable), retains
    the managed failure reason, and never claims managed binding. Physical facts
    that cannot be verified remain fail-closed via LaunchAdmissionError.
    """
    try:
        return OpenCodeLaunchPolicy().admit(project_root, custom_template=custom_template)
    except (LaunchAdmissionError, SaipenTransportError) as exc:
        detail = str(exc)
        if not DEGRADED_SAFE_REASON_RE.search(detail):
            raise
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise LaunchAdmissionError(f"Project source directory unavailable: {root}") from exc
        if not shutil.which("opencode"):
            raise LaunchAdmissionError("OpenCode executable unavailable on PATH; install opencode before launch") from exc
        opencode_bin = shutil.which("opencode") or "opencode.cmd"
        command = (str(opencode_bin), ".", "--auto")
        diagnostic: dict[str, str] = {
            "classification": "DEGRADED_DIRECT_SAFE",
            "reason": _bounded(detail, 400),
        }
        if known_display_name:
            diagnostic["project_name"] = _bounded(known_display_name, 80)
        return OpenCodeAdmission(
            True, root, command, binding=None,
            recovery_diagnostic=diagnostic,
            degraded=True, degraded_reason=detail,
        )


class OpenCodeLaunchPolicy:
    """Select the bound host-launch command or preserve the legacy path."""

    def admit(self, project_root: Path | str, *, custom_template: str | None) -> OpenCodeAdmission:
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise LaunchAdmissionError(f"Project source directory unavailable: {root}")
        if not is_managed(root):
            return OpenCodeAdmission(False, root)
        if custom_template and custom_template.strip():
            raise LaunchAdmissionError(
                "Managed project custom OpenCode template cannot bypass SAIPEN; "
                "remove the template in Settings > Launchers to use the bound launcher."
            )
        if not (root / ".saipen" / "IDENTITY.md").is_file():
            raise LaunchAdmissionError("Managed project has no .saipen/IDENTITY.md contract")
        try:
            entry = bound_entrypoint(root)
        except SaipenTransportError as exc:
            raise LaunchAdmissionError(f"SAIPEN launcher unavailable: {exc}") from exc
        python = bound_python()
        if not python.is_file():
            raise LaunchAdmissionError(f"SAIPEN launcher unavailable: Python missing: {python}")
        if not shutil.which("opencode"):
            raise LaunchAdmissionError("OpenCode executable unavailable on PATH; install opencode before launch")

        # Fleet preflight determines binding and host-admission facts. BOUND_VALID
        # and valid bound-recovery classifications may start the canonical host.
        # This launcher remains read-only and never performs recovery; the SAIPEN
        # guard controls consequential operations after host startup.
        preflight = self._fleet_preflight(python, entry, root)
        classification = preflight.get("classification")
        if classification not in ADMITS_LAUNCH:
            raise LaunchAdmissionError(f"SAIPEN Fleet preflight refused this project: {_diagnostic(preflight)}")

        # The preflight already verified root, lineage, and actor through
        # SAIPEN's own engine; AUDAPACK consumes the result, it does not invent
        # any of those facts. An explicit host launch cannot proceed from a
        # partial projection: missing actor would recreate the host-as-seat
        # failure this admission boundary exists to prevent.
        fleet_root = _bounded(preflight.get("project_identity"), 1024)
        fleet_lineage = _bounded(preflight.get("project_lineage"), 128)
        raw_actor = preflight.get("canonical_actor")
        actor = raw_actor.strip() if isinstance(raw_actor, str) else ""
        if not fleet_root or not fleet_lineage or not _SAIPEN_SEAT_RE.fullmatch(actor):
            raise LaunchAdmissionError(
                f"SAIPEN Fleet {classification} output lacks usable "
                "project_identity, project_lineage, or canonical_actor; "
                "refusing explicit host launch"
            )
        if fleet_root != os.path.normcase(os.path.realpath(root)):
            raise LaunchAdmissionError(f"SAIPEN Fleet preflight refused this project: {_diagnostic(preflight)}")

        if actor.casefold() in _HOST_IDENTITIES:
            # Routine generic host launch: when canonical_actor is a host identity
            # (e.g. 'opencode'), SAIPEN's explicit envelope launcher refuses '--agent <host>'
            # ("a host identity is not a protocol seat; launch the host generically (no --agent)
            # for canonical attribution"). Core's protocol snapshot inherits STATE.agent.
            opencode_bin = shutil.which("opencode") or "opencode.cmd"
            command = (str(opencode_bin), ".", "--auto")
        else:
            command = (
                str(python), str(entry), "--agent", actor, "--project-root", str(root),
                "launch", "opencode", "--", ".", "--auto",
            )
        binding = {
            "kind": "saipen-opencode-v1",
            "project_root": str(root),
            "entrypoint": str(entry.resolve()),
            # Canonical machine facts from the verified Fleet result. The raw
            # IDENTITY.md body is deliberately excluded: harmless comment or
            # formatting edits must never invalidate window reuse, and only
            # SAIPEN owns lineage verification. project_lineage is portable
            # durable evidence; project_identity is the machine-local root.
            "project_identity": fleet_root,
            "project_lineage": fleet_lineage,
            "actor": actor,
        }
        # For an admitted recovery-state classification, preserve the bounded
        # Fleet diagnostic facts so the UI can show a non-blocking status without
        # re-running Fleet. This is read-only evidence, never a canonical
        # mutation, and recovery remains owned by SAIPEN / the operator.
        recovery_diagnostic = None
        if classification in ("BOUND_RECOVERY_REQUIRED_SAFE", "BOUND_RECOVERY_REQUIRED_BLOCKED"):
            recovery_diagnostic = {
                "classification": _bounded(classification, 64),
                "reason_code": _bounded(preflight.get("reason_code", ""), 80),
                "reason": _bounded(preflight.get("reason", ""), 200),
                "canonical_next_command": _bounded(
                    preflight.get("canonical_next_command", ""), 200,
                ),
            }
            if not recovery_diagnostic["canonical_next_command"]:
                recovery_diagnostic.pop("canonical_next_command")
        return OpenCodeAdmission(True, root, command, binding, recovery_diagnostic)

    def _fleet_preflight(self, python: Path, entry: Path, root: Path) -> dict[str, Any]:
        # T-210 TARGET G: a machine-readable preflight owns its own stdio. The
        # child is non-interactive, so it must never depend on AUDAPACK's
        # INHERITED stdin handle: an inherited handle that has gone stale (the
        # escaped T-209 regression) surfaced right here as
        # ``[WinError 6] The handle is invalid`` at process creation. This is
        # defense in depth only -- the parent no longer mutates its console
        # state at all -- and it keeps the preflight independent of the host's
        # stdio history either way.
        try:
            result = run_hidden(
                [
                    str(python), str(entry), "fleet", "preflight",
                    "--cwd", str(root),
                    "--require-binding",
                    "--host-root", str(root),
                    "--json",
                ],
                cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                timeout=30, stdin=subprocess.DEVNULL,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise LaunchAdmissionError(f"SAIPEN Fleet preflight failed: {exc}") from exc
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise LaunchAdmissionError(f"SAIPEN Fleet preflight returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("classification"):
            raise LaunchAdmissionError("SAIPEN Fleet preflight returned invalid output")
        # read_only is asserted by the CLI itself for every preflight; if a
        # future bound CLI stops promising it, refuse rather than mutate.
        if payload.get("read_only") is not True:
            raise LaunchAdmissionError(
                "SAIPEN Fleet preflight did not promise a read-only result; refusing to launch"
            )
        return payload
