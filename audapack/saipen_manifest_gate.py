"""SAIPEN audit-manifest authority gate (precondition enforcement).

WHY THIS EXISTS
---------------
The 2026-09-15 15:11:04 SAIPENVIEW audit archive reported
``saipen_snapshot.status = PROTOCOL_MANIFEST_ABSENT`` with
``authoritative_state = false`` while the pack itself "succeeded":
``pack_single`` froze the inventory, computed the truthful SAIPEN verdict --
and shipped the non-authoritative archive anyway. Nothing in the canonical
pack path ever invoked ``saipen audit manifest --write``, so every pack of a
fresh SAIPEN project silently reproduced the same lie a reviewer cannot see
from the outside: a green package over a red snapshot.

THE CONTRACT
------------
``saipen_evidence`` already fails closed INSIDE the verdict. This module
closes the remaining gap -- the verdict arriving at the manifest writer at
all -- by making the normal pack path guarantee one of:

1. a current ``.saipen/MANIFEST.json`` exists immediately before the
   inventory freeze (generated through the REAL protocol CLI, never
   synthesized here); or
2. the pack fails closed with a precise, actionable precondition error
   (``FAILED_INVENTORY``), so no authoritative-looking archive is produced.

``gate`` is deliberately separated from ``prepare`` so the stale/missing
regressions can call ``gate`` directly against a prepared tree.

WHAT IS NOT DONE HERE
---------------------
* No manifest content is ever fabricated: generation is delegated to the
  SAIPEN protocol CLI (``saipen audit manifest --write``). If the CLI cannot
  be found or fails, the defect is reported, not papered over.
* The SAIPEN contract stays the sole authority on evidence; this module
  only proves the contract document exists and is structurally the
  published audit manifest before packing proceeds.
* Non-SAIPEN projects (no ``.saipen/`` directory) are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from audapack import saipen_evidence
from audapack.procutil import run_hidden

#: Shape marker of the published contract (see ``saipen_evidence``).
CONTRACT_KIND = "saipen_audit_manifest"

#: The manifest document is small; a file this large is malformed, not
#: generous. Kept in one place alongside the consumer's own cap so the two
#: cannot drift apart silently.
CONTRACT_MAX_BYTES = 256 * 1024

#: Candidate invocations for the SAIPEN protocol CLI, tried in order.
#: ``saipen.cmd`` is the Windows shim shipped next to the POSIX entrypoint.
SAIPEN_CLI_CANDIDATES = (
    ("saipen",),
    ("saipen.cmd",),
)

#: Host-session carriers that bind an AGENT SESSION to ITS project. The gate
#: runs the real protocol CLI against the TARGET project (cwd = project root),
#: so an ambient binding naming the consumer's own project is not the target's
#: context and must not leak into that subprocess: the target CLI then refuses
#: with PROJECT_BINDING_AMBIGUOUS / PROJECT_LINEAGE_MISMATCH and a stale
#: manifest can never be regenerated from a session that is itself bound --
#: measured live on a bound agent session packing a different registered
#: project, where the precondition failed with exactly that ambiguity.
SESSION_BINDING_ENV = ("SAIPEN_PROJECT_ROOT", "SAIPEN_PROJECT_LINEAGE", "SAIPEN_AGENT")


def _unbound_env() -> dict:
    """``os.environ`` without the consumer session's project binding."""
    env = dict(os.environ)
    for key in SESSION_BINDING_ENV:
        env.pop(key, None)
    return env

#: Fallback discovery: a SAIPEN project's ``.saipen/STATE.md`` names its own
#: protocol home under the ``saipen_home`` key.
SAIPEN_HOME_STATE_KEYS = ("saipen_home",)


@dataclass
class ManifestGateOutcome:
    """Terminal result of the pre-pack authority gate."""

    #: True only when a structurally valid contract document exists on disk.
    ok: bool
    #: Machine-readable terminal state (mirrors the snapshot vocabulary plus
    #: the generation-failure states this gate owns).
    code: str
    #: Operator-actionable message; never load-bearing for parsing.
    detail: str = ""
    #: True when this gate (re)generated the manifest through the protocol CLI.
    generated: bool = False
    #: The CLI argv that produced the manifest, when it was generated here.
    generated_by: Optional[list[str]] = None
    #: True when the protocol CLI RAN and answered with a non-zero exit. That is
    #: an authoritative refusal, not a missing launcher: trying another CLI would
    #: silently bypass the authority this gate exists to enforce.
    refused: bool = False


def _ordered_cli_candidates() -> tuple[tuple[str, ...], ...]:
    """Native launcher names first; keep deterministic order on every OS."""
    candidates = tuple(SAIPEN_CLI_CANDIDATES)
    if os.name == "nt":
        candidates = tuple(
            sorted(
                candidates,
                key=lambda args: (
                    not str(args[0]).lower().endswith((".bat", ".cmd", ".exe")),
                    str(args[0]).casefold(),
                ),
            )
        )
    return candidates


def _state_home_candidates(memory_root: Path) -> list[list[str]]:
    """CLI candidates derived from STATE.md's own ``saipen_home`` declaration."""
    out: list[list[str]] = []
    state = memory_root / "STATE.md"
    try:
        text = state.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        for key in SAIPEN_HOME_STATE_KEYS:
            prefix = key + ":"
            if not stripped.startswith(prefix):
                continue
            home = stripped[len(prefix):].strip().strip("\"'")
            if not home:
                continue
            home_dir = Path(home)
            for candidate in _ordered_cli_candidates():
                resolved = home_dir / "bin" / candidate[0]
                if resolved.is_file():
                    out.append([str(resolved)])
    return out


def _well_known_install_candidates() -> list[list[str]]:
    """Well-known OS install locations for the SAIPEN launcher.

    When STATE.md carries a ``saipen_home`` from another machine (e.g. a
    Linux path written by a remote agent session), the resolved path does
    not exist on the local host and the gate falls through to PATH — which
    may also miss the launcher when it was installed outside of PATH.

    This step covers the standard per-user install location on Windows
    (``%LOCALAPPDATA%\\saipen\\scheduled-source\\bin\\``) so a stale or
    foreign ``saipen_home`` never blocks packing.
    """
    out: list[list[str]] = []
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for candidate in _ordered_cli_candidates():
                p = Path(local_app) / "saipen" / "scheduled-source" / "bin" / candidate[0]
                if p.is_file():
                    out.append([str(p)])
    return out


def _cli_candidates(project_root: Path, memory_root: Path) -> list[list[str]]:
    """Ordered invocation attempts for the real SAIPEN CLI."""
    candidates: list[list[str]] = []
    ordered = _ordered_cli_candidates()
    # 1. A SAIPEN binary shipped inside the project itself (bin/saipen).
    for candidate in ordered:
        local = project_root / "bin" / candidate[0]
        if local.is_file():
            candidates.append([str(local)])
    # 2. Whatever the project's own STATE.md declares as its protocol home.
    candidates.extend(_state_home_candidates(memory_root))
    # 3. Well-known OS install locations (survives a foreign saipen_home).
    candidates.extend(_well_known_install_candidates())
    # 4. Resolve PATH to an absolute executable. No shell, no bare-name
    # subprocess fallback, and no dependence on a caller-specific PATHEXT.
    for candidate in ordered:
        resolved = shutil.which(candidate[0])
        if resolved:
            candidates.append([str(Path(resolved).resolve())])
    # De-duplicate identical argv while preserving order.
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for argv in candidates:
        key = tuple(argv)
        if key in seen:
            continue
        seen.add(key)
        unique.append(argv)
    return unique


def _is_saipen_checkpoint(memory_root: Path) -> bool:
    """True when the memory root carries the protocol's core documents.

    STATE.md alone is the protocol's own minimum for a loadable checkpoint
    (audit_manifest.LAYOUT_CORE_FILES = STATE/BOARD/LOG; STATE is the one
    every lifecycle call reads first). A `.saipen/` directory WITHOUT any
    STATE.md is not a lifecycle carrier at all -- it is ordinary project
    content (a synthetic tree, a half-copied fixture, a retired directory)
    and a missing CLI candidate must not block packing it.
    """
    try:
        state = memory_root / "STATE.md"
        if state.is_symlink() or not state.is_file():
            return False
        return bool(state.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _manifest_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _invoke_manifest_cli(
    argv: list[str],
    project_root: Path,
    *,
    timeout_seconds: int,
    command: list[str] | None = None,
) -> ManifestGateOutcome:
    command = list(command or [*argv, "audit", "manifest", "--write"])
    try:
        proc = run_hidden(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=_unbound_env(),
            # The child owns a closed stdin: no inherited console input and no
            # session binding from the consumer project.
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_GENERATION_FAILED",
            detail=f"launcher discovery failed for {argv[0]}: executable not found",
        )
    except subprocess.TimeoutExpired:
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_GENERATION_FAILED",
            detail=(
                f"launcher timeout: {' '.join(command)} timed out after "
                f"{timeout_seconds}s"
            ),
        )
    except OSError as exc:
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_GENERATION_FAILED",
            detail=f"launcher execution failed for {argv[0]}: {exc}",
        )
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").strip()[:1000]
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_GENERATION_FAILED",
            detail=(
                f"protocol refusal: {' '.join(command)} failed "
                f"(rc={proc.returncode}): {output or 'no output captured'}"
            ),
            refused=True,
        )
    output = (proc.stdout or proc.stderr or "SAIPEN CLI exited 0").strip()
    return ManifestGateOutcome(
        ok=True,
        code="AUDIT_MANIFEST_GENERATED",
        detail=output[:1000],
        generated_by=command,
    )


def _restore_manifest_backup(manifest_path: Path, backup_path: Path) -> str:
    """Restore the exact stale document after a failed forced refresh."""
    try:
        if manifest_path.is_symlink() or manifest_path.exists():
            if manifest_path.is_dir():
                return f"cannot restore: {manifest_path} is a directory; stale copy preserved at {backup_path}"
            manifest_path.unlink()
        if backup_path.exists():
            backup_path.replace(manifest_path)
        return ""
    except OSError as exc:
        return f"rollback failed: {exc}; stale copy preserved at {backup_path}"


def _restore_manifest_bytes(manifest_path: Path, data: bytes) -> str:
    """Atomically put the exact pre-generation document back after a bad write."""
    restore = manifest_path.with_name(
        f".{manifest_path.name}.audapack-restore-{uuid.uuid4().hex}.tmp"
    )
    try:
        restore.write_bytes(data)
        restore.replace(manifest_path)
        return ""
    except OSError as exc:
        try:
            restore.unlink(missing_ok=True)
        except OSError:
            pass
        return f"rollback failed: {exc}"


def _verified_generation(project_root: Path, outcome: ManifestGateOutcome) -> ManifestGateOutcome:
    """A successful process is not proof; the resulting authority must be fresh."""
    verified = validate_contract_document(project_root)
    if not verified.ok:
        return ManifestGateOutcome(
            ok=False,
            code=verified.code,
            detail=(
                "SAIPEN CLI reported success but manifest re-validation failed "
                f"({verified.code}): {verified.detail}"
            ),
        )
    contract = saipen_evidence.read_contract(project_root)
    why = _contract_staleness(project_root, contract)
    if why:
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_STALE_REGENERATION_FAILED",
            detail=f"SAIPEN CLI reported success but manifest remains stale: {why}",
        )
    return ManifestGateOutcome(
        ok=True,
        code=verified.code,
        detail=verified.detail,
        generated=True,
        generated_by=outcome.generated_by,
    )


def _refresh_current_manifest(
    project_root: Path,
    outcome: ManifestGateOutcome,
    *,
    timeout_seconds: int,
    log_callback=None,
) -> ManifestGateOutcome:
    """Make a stale-but-CURRENT declaration converge through the real CLI.

    Current SAIPEN compares contract shape, not evidence mtimes, so a stale
    declaration can legitimately answer ``AUDIT_MANIFEST_CURRENT``. Retire the
    exact old document atomically, run the same canonical command again, then
    validate and prove freshness before deleting the rollback copy.
    """
    manifest_path = project_root / saipen_evidence.MANIFEST_REL
    if not manifest_path.is_file():
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_STALE_REGENERATION_FAILED",
            detail="cannot refresh current manifest: .saipen/MANIFEST.json disappeared",
        )
    backup_path = manifest_path.with_name(
        f".{manifest_path.name}.audapack-{uuid.uuid4().hex}.bak"
    )
    try:
        manifest_path.replace(backup_path)
    except OSError as exc:
        return ManifestGateOutcome(
            ok=False,
            code="AUDIT_MANIFEST_STALE_REGENERATION_FAILED",
            detail=f"manifest refresh failed while preserving the stale copy: {exc}",
        )

    regenerated = _invoke_manifest_cli(
        outcome.generated_by or [],
        project_root,
        timeout_seconds=timeout_seconds,
        command=outcome.generated_by,
    )
    if not regenerated.ok:
        rollback_error = _restore_manifest_backup(manifest_path, backup_path)
        detail = regenerated.detail
        if rollback_error:
            detail = f"{detail}; {rollback_error}"
        else:
            detail = f"{detail}; previous stale manifest restored"
        return ManifestGateOutcome(ok=False, code=regenerated.code, detail=detail)

    verified = _verified_generation(project_root, regenerated)
    if not verified.ok:
        rollback_error = _restore_manifest_backup(manifest_path, backup_path)
        detail = verified.detail
        if rollback_error:
            detail = f"{detail}; {rollback_error}"
        else:
            detail = f"{detail}; previous stale manifest restored"
        return ManifestGateOutcome(ok=False, code=verified.code, detail=detail)

    if log_callback is not None:
        try:
            log_callback(f"saipen audit manifest --write -> {regenerated.detail[:200]}")
        except Exception:
            pass
    try:
        backup_path.unlink()
    except OSError as exc:
        if log_callback is not None:
            try:
                log_callback(f"saipen manifest refreshed; rollback copy cleanup failed: {exc}")
            except Exception:
                pass
    return verified


def generate_manifest(
    project_root: Path,
    *,
    cancel_event=None,
    timeout_seconds: int = 120,
    log_callback=None,
    refresh_current: bool = False,
) -> ManifestGateOutcome:
    """Run the REAL ``saipen audit manifest --write`` protocol command.

    This is a delegation, not an emulation: the SAIPEN runtime owns the
    contract and writes its own document. Audapack never authors manifest
    bytes. The command is invoked with the project root as cwd exactly like a
    human operator would.

    A ``.saipen/`` directory that is not a checkpoint (no STATE.md) is NOT
    SAIPEN for gate purposes: there is no lifecycle for a contract to
    describe, so the outcome is the ordinary non-SAIPEN pass-through instead
    of a generation failure. A real project -- STATE.md present -- still
    fails closed when no protocol CLI can be reached.
    """
    memory_root = project_root / saipen_evidence.MEMORY_ROOT
    if not saipen_evidence.detect(project_root):
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN)
    if not _is_saipen_checkpoint(memory_root):
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN)
    manifest_path = project_root / saipen_evidence.MANIFEST_REL
    failures: list[str] = []
    for argv in _cli_candidates(project_root, memory_root):
        if cancel_event is not None and cancel_event.is_set():
            return ManifestGateOutcome(ok=False, code="PACK_CANCELLED")
        try:
            before = _manifest_snapshot(manifest_path)
        except OSError as exc:
            return ManifestGateOutcome(
                ok=False,
                code="AUDIT_MANIFEST_GENERATION_FAILED",
                detail=f"cannot snapshot existing manifest before regeneration: {exc}",
            )
        outcome = _invoke_manifest_cli(
            argv,
            project_root,
            timeout_seconds=timeout_seconds,
        )
        if not outcome.ok:
            failures.append(outcome.detail)
            # A launcher that could not be STARTED is a discovery miss, and the
            # next candidate is a legitimate try. A launcher that RAN and
            # refused is the protocol's own terminal answer -- continuing would
            # hand the pack to whatever CLI is installed next, which is exactly
            # how a CAPABILITY_REFUSED manifest gets packed anyway.
            if outcome.refused:
                break
            continue
        try:
            after = _manifest_snapshot(manifest_path)
        except OSError as exc:
            failures.append(f"cannot verify manifest after SAIPEN CLI success: {exc}")
            continue
        changed = after != before
        if log_callback is not None:
            try:
                suffix = "" if changed else "; manifest bytes unchanged"
                log_callback(f"saipen audit manifest --write -> {outcome.detail[:200]}{suffix}")
            except Exception:  # logging must never gate a pack
                pass
        if refresh_current and not changed:
            contract = saipen_evidence.read_contract(project_root)
            if not _contract_staleness(project_root, contract):
                return _verified_generation(project_root, outcome)
            return _refresh_current_manifest(
                project_root,
                outcome,
                timeout_seconds=timeout_seconds,
                log_callback=log_callback,
            )
        if not changed and after is None:
            failures.append("SAIPEN CLI reported success but did not create .saipen/MANIFEST.json")
            continue
        verified = _verified_generation(project_root, outcome)
        if not verified.ok and refresh_current and before is not None:
            rollback_error = _restore_manifest_bytes(manifest_path, before)
            detail = verified.detail
            detail = (
                f"{detail}; {rollback_error or 'previous stale manifest restored'}"
            )
            return ManifestGateOutcome(ok=False, code=verified.code, detail=detail)
        return verified
    tried = ", ".join(sorted({str(candidate[0]) for candidate in _ordered_cli_candidates()}))
    discovery_detail = "launcher discovery found no executable in project bin, STATE saipen_home, or PATH"
    if tried:
        discovery_detail += f" (tried: {tried})"
    return ManifestGateOutcome(
        ok=False,
        code="AUDIT_MANIFEST_GENERATION_FAILED",
        detail="; ".join(failures) or discovery_detail,
    )



def validate_contract_document(project_root: Path) -> ManifestGateOutcome:
    """Prove ``.saipen/MANIFEST.json`` exists and is the published contract.

    Structural only: the SAIPEN runtime owns the content contract.
    ``saipen_evidence.read_contract`` already refuses absolute/escaping paths,
    oversized documents, unknown contract versions and wrong kinds -- the
    gate reuses exactly that authority instead of a second drifting parser.
    """
    contract = saipen_evidence.read_contract(project_root)
    if contract.status == saipen_evidence.STATUS_NOT_SAIPEN:
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN)
    if contract.status == saipen_evidence.STATUS_MANIFEST_ABSENT:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_ABSENT,
            detail=contract.detail,
        )
    if contract.status in (
        saipen_evidence.STATUS_MANIFEST_MALFORMED,
        saipen_evidence.STATUS_CONTRACT_UNKNOWN,
    ):
        # For CONTRACT_UNKNOWN the consumer's detail IS the operator
        # remediation (see `saipen_evidence.check_capabilities_detail`): a newer
        # contract that lacks required capabilities is a consumer defect, and the
        # instruction must not be "regenerate", which cannot downgrade what the
        # installed protocol writes.
        return ManifestGateOutcome(
            ok=False,
            code=contract.status,
            detail=contract.detail,
        )
    # STATUS_COMPATIBLE_DEGRADED: forward-compat admitted; allow but mark.
    if contract.admission == saipen_evidence.ManifestAdmission.COMPATIBLE_DEGRADED:
        return ManifestGateOutcome(
            ok=True,
            code=saipen_evidence.STATUS_COMPATIBLE_DEGRADED,
            detail=contract.detail,
        )
    # STATUS_COMPLETE (provisional) from read_contract: the document parsed,
    # declared mandatory evidence and announced a supported version. The gate
    # adds the one check the consumer cannot do at collection time: the
    # contract must have been generated by the real protocol generator -- and
    # by the generator OF THE VERSION IT DECLARES, so a document cannot claim
    # one shape's rules under another shape's provenance.
    generator = contract.generator or ""
    prefix = "saipen-audit-manifest/"
    if not generator.startswith(prefix):
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=(
                f".saipen/MANIFEST.json generator {generator!r} is not a "
                "saipen-audit-manifest generator; refusing to treat it as "
                "protocol evidence"
            ),
        )
    declared_generator_version = generator[len(prefix):]
    if not declared_generator_version.isdigit() or int(declared_generator_version) != contract.contract_version:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=(
                f".saipen/MANIFEST.json generator {generator!r} does not match "
                f"the contract_version {contract.contract_version} it declares; "
                "the protocol writes both together, so this document is not "
                "the published contract"
            ),
        )
    path = project_root.joinpath(*saipen_evidence.MANIFEST_REL.split("/"))
    try:
        if path.stat().st_size > CONTRACT_MAX_BYTES:
            return ManifestGateOutcome(
                ok=False,
                code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
                detail=f".saipen/MANIFEST.json exceeds {CONTRACT_MAX_BYTES} bytes",
            )
        json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=f".saipen/MANIFEST.json unreadable: {exc}",
        )
    except ValueError as exc:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=f".saipen/MANIFEST.json is not valid JSON: {exc}",
        )
    return ManifestGateOutcome(
        ok=True,
        code=saipen_evidence.STATUS_COMPLETE,
        detail=f"contract_version={contract.contract_version} generator={contract.generator}",
    )


def _disk_mandatory_paths(project_root: Path, contract) -> set[str]:
    """Mandatory contract paths that genuinely exist as real files on disk.

    Mirrors ``saipen_evidence.collect``'s own file test: a symlink or a
    non-regular file is NOT evidence, so staleness is judged over what the
    protocol would actually pack.
    """
    found: set[str] = set()
    for rel in contract.mandatory:
        full = project_root.joinpath(*rel.split("/"))
        try:
            st = full.lstat()
        except OSError:
            continue
        if stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
            continue
        found.add(rel)
    return found


def _evidence_newer_than_manifest(project_root: Path, contract) -> list[str]:
    """Mandatory evidence files whose mtime postdates the manifest document.

    The manifest file's own mtime (not ``generated_at``) is the baseline: it
    is the same clock that stamps the evidence, so clock skew between the
    protocol stamp and the filesystem cannot produce a false verdict, and a
    hand-authored contract written after its fixtures reads as current.
    The comparison is deliberately STRICT with no skew tolerance: a false
    "stale" verdict costs one idempotent regeneration through the protocol
    CLI, while a false "current" verdict is exactly the blind trust this
    gate exists to prevent. Equality counts as current (a copied tree
    preserves it in any order).
    """
    manifest_path = project_root.joinpath(*saipen_evidence.MANIFEST_REL.split("/"))
    try:
        baseline_ns = manifest_path.stat().st_mtime_ns
    except OSError:
        return []
    stale: list[str] = []
    for rel in contract.mandatory:
        full = project_root.joinpath(*rel.split("/"))
        try:
            mtime_ns = full.lstat().st_mtime_ns
        except OSError:
            continue
        if mtime_ns > baseline_ns:
            stale.append(rel)
    return stale


def _contract_staleness(project_root: Path, contract) -> str:
    """Exact reason the installed contract is not current, or an empty string."""
    disk_mandatory = sorted(_disk_mandatory_paths(project_root, contract))
    declared_mandatory = sorted(contract.mandatory)
    if disk_mandatory != declared_mandatory:
        return (
            "declared mandatory evidence no longer matches disk: "
            f"declared={declared_mandatory} on_disk={disk_mandatory}"
        )
    stale_evidence = _evidence_newer_than_manifest(project_root, contract)
    if stale_evidence:
        return f"mandatory evidence mutated after the manifest was written: {stale_evidence}"
    return ""


def prepare(
    project_root: Path,
    *,
    cancel_event=None,
    timeout_seconds: int = 120,
    log_callback=None,
) -> ManifestGateOutcome:
    """The pre-pack precondition: ensure a CURRENT contract manifest exists.

    Order of authority:

    1. A structurally valid contract document that still names exactly the
       mandatory evidence present on disk wins -- the protocol CLI is never
       invoked for a current manifest, so a normal pack never mutates a
       project whose contract is up to date.
    2. A STALE contract (declared mandatory evidence no longer matches disk,
       e.g. STATE/BOARD/LOG mutated after the manifest was written) is
       regenerated through the real protocol command before the pack may
       trust it; regeneration failure fails closed.
    3. A missing manifest is generated through the real ``saipen audit
       manifest --write`` (cwd = the project root) and re-validated.
    4. Otherwise the outcome fails closed with a precise, actionable error;
       the caller must refuse to produce an authoritative-looking archive.
    """
    root = Path(project_root)
    existing = validate_contract_document(root)
    if existing.ok:
        contract = saipen_evidence.read_contract(root)
        why = _contract_staleness(root, contract)
        if not why:
            return existing
        # Stale-contract repair: authoritative evidence mutated after the
        # manifest was written. Current SAIPEN may answer CURRENT because its
        # declaration shape did not change; refresh_current retires that exact
        # stale document and runs the same canonical command again.
        regenerated = generate_manifest(
            root,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
            log_callback=log_callback,
            refresh_current=True,
        )
        if not regenerated.ok:
            return ManifestGateOutcome(
                ok=False,
                code="AUDIT_MANIFEST_STALE_REGENERATION_FAILED",
                detail=f"manifest is stale ({why}); regeneration failed: {regenerated.detail}",
            )
        return ManifestGateOutcome(
            ok=True,
            code=regenerated.code,
            detail=f"stale manifest regenerated: {regenerated.detail}",
            generated=True,
            generated_by=regenerated.generated_by,
        )
    if existing.code == saipen_evidence.STATUS_NOT_SAIPEN:
        return existing
    if not _is_saipen_checkpoint(root / saipen_evidence.MEMORY_ROOT):
        # Not a checkpoint (no STATE.md): there is no lifecycle a contract
        # could describe, so this is ordinary content, not a missing
        # manifest. Pass through like any non-SAIPEN project instead of
        # failing the pack over a precondition that cannot exist here.
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN)
    if cancel_event is not None and cancel_event.is_set():
        return ManifestGateOutcome(ok=False, code="PACK_CANCELLED")
    if existing.code == saipen_evidence.STATUS_CONTRACT_UNKNOWN:
        # TARGET G: the manifest EXISTS and may well be current; it is the
        # contract VERSION this collector cannot read. Prescribing
        # `saipen audit manifest --write` here would be false advice, because
        # the installed protocol writes exactly the newer contract that was
        # refused -- regeneration cannot downgrade it. Say what actually
        # repairs the failure: this consumer. `existing.detail` already names
        # that (saipen_evidence.contract_unknown_detail).
        return ManifestGateOutcome(
            ok=False,
            code=existing.code,
            detail=(
                f"{existing.detail} Regeneration is deliberately NOT attempted: "
                "`saipen audit manifest --write` writes the contract version the "
                "installed protocol implements, so it cannot downgrade a newer "
                "manifest."
            ),
        )
    if existing.code in (
        saipen_evidence.STATUS_MANIFEST_MALFORMED,
    ):
        # A present-but-unusable contract is a project defect the protocol
        # owner must fix; regenerating on top of it would silently rewrite
        # evidence the operator may be relying on.
        return ManifestGateOutcome(
            ok=False,
            code=existing.code,
            detail=f"precondition failed; not regenerating: {existing.detail}",
        )
    generated = generate_manifest(
        root,
        cancel_event=cancel_event,
        timeout_seconds=timeout_seconds,
        log_callback=log_callback,
    )
    if not generated.ok:
        return generated
    verified = validate_contract_document(root)
    if not verified.ok:
        return ManifestGateOutcome(
            ok=False,
            code=verified.code,
            detail=(
                "saipen audit manifest --write reported success but "
                f"{verified.code}: {verified.detail}"
            ),
        )
    return ManifestGateOutcome(
        ok=True,
        code=verified.code,
        detail=verified.detail,
        generated=True,
        generated_by=generated.generated_by,
    )


__all__ = [
    "CONTRACT_KIND",
    "CONTRACT_MAX_BYTES",
    "ManifestGateOutcome",
    "_is_saipen_checkpoint",
    "generate_manifest",
    "validate_contract_document",
    "prepare",
]
