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
The SAIPEN audit manifest is DECLARATIVE (it defines evidence rules, not a
snapshot/file listing). Therefore ordinary lifecycle writes (STATE.md,
BOARD.md, LOG.md) never stale MANIFEST.json. Evidence presence and completeness
are evaluated downstream by ``saipen_evidence``, not by this gate.

This module ensures that:
1. A supported, structurally valid ``.saipen/MANIFEST.json`` contract exists
   before inventory freeze.
2. If absent on a real SAIPEN checkpoint, it is generated through the REAL
   protocol CLI (``saipen audit manifest --write``).
3. If malformed or unsupported, packing fails closed with a precise,
   actionable precondition error (``FAILED_INVENTORY``).

WHAT IS NOT DONE HERE
---------------------
* No manifest content is fabricated: generation delegates to the protocol CLI.
* No evidence mtimes or disk mandatory sets are used as false staleness:
  SAIPEN owns the meaning of SAIPEN's declarative manifest.
* Non-SAIPEN projects (no ``.saipen/`` directory or no STATE.md checkpoint)
  are untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
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

    #: True only for a valid document with an implemented native contract.
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
    #: Parsing, implementation support and admission are separate diagnostics;
    #: evidence completeness is assessed later against inventory/archive content.
    manifest_parse_valid: bool = False
    contract_supported: bool = False
    admission: str = ""


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


def _verified_generation(project_root: Path, outcome: ManifestGateOutcome) -> ManifestGateOutcome:
    """A successful process is not proof; the resulting authority must be structurally valid."""
    verified = validate_contract_document(project_root)
    if not verified.ok:
        return replace(
            verified,
            detail=(
                "SAIPEN CLI reported success but manifest validation failed "
                f"({verified.code}): {verified.detail}"
            ),
        )
    return replace(
        verified,
        generated=True,
        generated_by=outcome.generated_by,
    )


def generate_manifest(
    project_root: Path,
    *,
    cancel_event=None,
    timeout_seconds: int = 120,
    log_callback=None,
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
        if after is None:
            failures.append("SAIPEN CLI reported success but did not create .saipen/MANIFEST.json")
            continue
        if log_callback is not None:
            try:
                log_callback(f"saipen audit manifest --write -> {outcome.detail[:200]}")
            except Exception:  # logging must never gate a pack
                pass
        return _verified_generation(project_root, outcome)
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
    diagnostics = {
        "manifest_parse_valid": contract.manifest_parse_valid,
        "contract_supported": contract.contract_supported,
        "admission": contract.admission,
    }
    if contract.status == saipen_evidence.STATUS_NOT_SAIPEN:
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN, **diagnostics)
    if contract.status == saipen_evidence.STATUS_MANIFEST_ABSENT:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_ABSENT,
            detail=contract.detail,
            **diagnostics,
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
            **diagnostics,
        )
    if not diagnostics["contract_supported"] or contract.admission != saipen_evidence.ManifestAdmission.NATIVE:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_CONTRACT_UNKNOWN,
            detail=contract.detail or saipen_evidence.contract_unknown_detail(contract.contract_version),
            **diagnostics,
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
            **{**diagnostics, "manifest_parse_valid": False},
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
            **{**diagnostics, "manifest_parse_valid": False},
        )
    path = project_root.joinpath(*saipen_evidence.MANIFEST_REL.split("/"))
    try:
        if path.stat().st_size > CONTRACT_MAX_BYTES:
            return ManifestGateOutcome(
                ok=False,
                code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
                detail=f".saipen/MANIFEST.json exceeds {CONTRACT_MAX_BYTES} bytes",
                **{**diagnostics, "manifest_parse_valid": False},
            )
        json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=f".saipen/MANIFEST.json unreadable: {exc}",
            **{**diagnostics, "manifest_parse_valid": False},
        )
    except ValueError as exc:
        return ManifestGateOutcome(
            ok=False,
            code=saipen_evidence.STATUS_MANIFEST_MALFORMED,
            detail=f".saipen/MANIFEST.json is not valid JSON: {exc}",
            **{**diagnostics, "manifest_parse_valid": False},
        )
    return ManifestGateOutcome(
        ok=True,
        code=saipen_evidence.STATUS_COMPLETE,
        detail=f"contract_version={contract.contract_version} generator={contract.generator}",
        **diagnostics,
    )


def prepare(
    project_root: Path,
    *,
    cancel_event=None,
    timeout_seconds: int = 120,
    log_callback=None,
) -> ManifestGateOutcome:
    """The pre-pack precondition: ensure a valid contract manifest exists.

    Order of authority:

    1. A structurally valid contract document exists and is supported
       with native implementation support. The protocol CLI is NEVER invoked
       for a valid manifest; ordinary checkpoint writes (STATE/BOARD/LOG)
       never stale a declarative manifest contract.
    2. Non-SAIPEN projects (or a .saipen/ directory without STATE.md) pass through.
    3. Malformed contract or unknown version, regardless of structural compatibility,
       fails closed without implicit overwrite or regeneration.
    4. A missing manifest on a real SAIPEN checkpoint is generated through the
       real `saipen audit manifest --write` (cwd = project root) and validated.
    """
    root = Path(project_root)
    existing = validate_contract_document(root)
    if existing.ok:
        return existing

    if existing.code == saipen_evidence.STATUS_NOT_SAIPEN:
        return existing

    if cancel_event is not None and cancel_event.is_set():
        return ManifestGateOutcome(ok=False, code="PACK_CANCELLED")

    if existing.code == saipen_evidence.STATUS_CONTRACT_UNKNOWN:
        # The manifest EXISTS and may well be current; it is the contract
        # VERSION this collector cannot read. Prescribing `saipen audit manifest --write`
        # here would be false advice, because the installed protocol writes that
        # same newer contract -- regeneration cannot downgrade it.
        return replace(
            existing,
            detail=(
                f"{existing.detail} Regeneration is deliberately NOT attempted: "
                "`saipen audit manifest --write` writes the contract version the "
                "installed protocol implements, so it cannot downgrade a newer "
                "manifest."
            ),
        )

    if existing.code == saipen_evidence.STATUS_MANIFEST_MALFORMED:
        # A present-but-unusable contract is a project defect the protocol
        # owner must fix; regenerating on top of it would silently rewrite
        # evidence the operator may be relying on.
        return replace(
            existing,
            detail=f"precondition failed; not regenerating: {existing.detail}",
        )

    if not _is_saipen_checkpoint(root / saipen_evidence.MEMORY_ROOT):
        # A lookalike directory with no declaration has no protocol checkpoint.
        # A PRESENT unsupported/malformed declaration was refused above: missing
        # STATE.md cannot waive its producer's compatibility policy.
        return ManifestGateOutcome(ok=False, code=saipen_evidence.STATUS_NOT_SAIPEN)

    # Manifest absent on a real checkpoint: generate it
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
        return replace(
            verified,
            detail=(
                "saipen audit manifest --write reported success but "
                f"{verified.code}: {verified.detail}"
            ),
        )
    return replace(
        verified,
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
