"""Frozen source inventory for AUDAPACK packing (T-190, SRC-046).

The pre-T-190 packaging pipeline derived archive membership from filesystem
traversal plus exclusion/fidelity policy. In a Git repository that is not the
definition of the project: a file committed to Git is source-of-truth project
material even when its name matches an ordinary noise exclude, so a walk-based
archive can be syntactically valid and still silently omit tracked source.

This module owns the fix as ONE explicit discovery stage:

    DISCOVER -> VALIDATE INVENTORY -> FREEZE -> WRITE -> VERIFY -> COMMIT

For a Git worktree the inventory is exactly::

    tracked_existing  UNION  untracked_nonignored  UNION  protected_control_plane

resolved with a bounded number of Git commands (never one per file). Git
membership overrides ordinary noise/fidelity excludes; only the explicit
hard-safety policy may override a tracked path. The protected-control-plane
overlay (T-188 P0) re-enumerates the ignored-but-protected live audit
locations (``.saipen/intake/active/**``, ``audit/<numeric>.md``,
``.saipen/audit/<numeric>.md``) plus explicitly required paths (exact
``always_include`` patterns and manifest-declared required files) that Git
ignore rules hide from ``--exclude-standard`` enumeration -- it is bounded to
those semantic locations and never becomes a generic ignored-tree walk. Git
ignore alone is never an AUDAPACK exclusion for protected material; explicit
AUDAPACK policy (mandatory/hard-safety, always_exclude, configured) retains
its precedence. For a genuinely non-Git directory the existing bounded walk
fallback produces the same frozen-inventory shape, and every archive write
consumes only the frozen set.

Fail-closed contract: Git metadata that cannot be inventoried is an error,
never a silent fallback to ``os.walk``. Submodules, escaping symlinks, duplicate
or colliding paths, and tracked files whose absence Git cannot explain all fail
the pack before a single archive byte is written.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from audapack import saipen_evidence
from audapack.config import MANDATORY_EXCLUDES
from audapack.fidelity import (
    _build_matcher,
    _priority_for,
    exclusion_reason_for,
)

__all__ = [
    "SourceOrigin",
    "SourceInventoryEntry",
    "SourceInventory",
    "SourceInventoryError",
    "HARD_SAFETY_EXCLUDES",
    "RESERVED_ARCHIVE_NAMES",
    "GENERATED_CONTROL_MARKERS",
    "is_generated_archive_control",
    "REASON_TRACKED_DELETED",
    "REASON_SUPERSEDED_GENERATED_CONTROL",
    "REASON_UNTRACKED_GIT_DIR",
    "REASON_UNTRACKED_NON_REGULAR",
    "REASON_NESTED_GIT_DIFFERENT_ORIGIN",
    "REASON_COMPONENT_OPTIONAL",
    "CODE_NESTED_COMPONENT_INVENTORY",
    "CODE_GIT_UNAVAILABLE",
    "CODE_INVENTORY_INCONSISTENT",
    "CODE_TRACKED_HARD_DENY_CONFLICT",
    "CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT",
    "reserved_archive_conflict_message",
    "CODE_SUBMODULE_REQUIRES_EXPLICIT_POLICY",
    "CODE_SYMLINK_UNSAFE",
    "CODE_PATH_INVALID",
    "detect_git_worktree",
    "build_git_inventory",
    "NestedGitComponent",
    "normalize_origin_url",
    "discover_nested_git_components",
    "nested_component_manifest_section",
    "build_pack_inventory",
    "saipen_verdict",
    "evidence_omission_reason",
]


#: Error codes. They surface as ``[<code>] message`` in PackResult.error_message
#: so an operator sees one stable vocabulary in the UI, the bridge and the CLI.
CODE_GIT_UNAVAILABLE = "GIT_UNAVAILABLE"
CODE_INVENTORY_INCONSISTENT = "SOURCE_INVENTORY_INCONSISTENT"
CODE_TRACKED_HARD_DENY_CONFLICT = "TRACKED_HARD_DENY_CONFLICT"
#: A tracked path sits on an archive-control name AUDAPACK itself writes. It is
#: the SAME fail-closed refusal as hard safety -- the tracked file can be
#: neither omitted nor packaged -- but a different POLICY and a different
#: operator repair, so it must never be reported as generic secret denial: a
#: reserved-name collision is fixed in source control, not by a packer setting.
CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT = "TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT"

#: The same collision, seen from a source Git is not tracking. A project-owned
#: or malformed control artifact can arrive untracked and non-ignored (the
#: ordinary state after any pack whose control artifact nobody committed), and
#: reporting that as ``TRACKED_...`` told the operator to look in source control
#: for a file that was never in it -- advice that cannot end the conflict. The
#: refusal itself is unchanged and equally fail-closed; only the classification
#: stops lying about where the file came from.
CODE_RESERVED_ARCHIVE_NAME_CONFLICT = "RESERVED_ARCHIVE_NAME_CONFLICT"
CODE_SUBMODULE_REQUIRES_EXPLICIT_POLICY = "SUBMODULE_REQUIRES_EXPLICIT_POLICY"
CODE_SYMLINK_UNSAFE = "SYMLINK_UNSAFE"
CODE_PATH_INVALID = "PATH_INVALID"
#: A detected same-product component could not be inventoried. Distinct from
#: CODE_INVENTORY_INCONSISTENT because the operator's repair is different:
#: this one names a specific nested directory whose index or worktree cannot be
#: read, and the pack must fail rather than ship an archive that is quietly
#: missing a first-party component.
CODE_NESTED_COMPONENT_INVENTORY = "NESTED_GIT_COMPONENT_INVENTORY_FAILED"


#: Exclusion reasons used for inventory entries that are not written. The
#: tracked-deletion category is new: a worktree deletion Git records is an
#: intentional state, not an unexplained inventory hole.
REASON_TRACKED_DELETED = "tracked_deleted"

#: Git reported an untracked directory it did not recurse into (the directory
#: is itself a Git worktree/clone). Git cannot offer its per-file content, so
#: the entry is an explicit, named omission -- the same philosophy as
#: ``tracked_deleted``: an intentional state, never a silent inventory hole.
REASON_UNTRACKED_GIT_DIR = "untracked_git_directory"

#: An untracked path Git listed that is neither a regular file, a symlink, nor a
#: directory (a FIFO, socket, device node, or a path whose type changed under
#: the enumeration). Such an object carries no packageable file content and one
#: transient special file in the worktree must never fail the WHOLE pack -- it
#: is recorded as an explicit, named omission, exactly like an untracked nested
#: Git worktree above. A TRACKED non-regular is still a hard failure (the index
#: promised content that is not there); only the UNTRACKED case is downgraded,
#: because Git never promised an untracked special file was project truth.
REASON_UNTRACKED_NON_REGULAR = "untracked_non_regular"

#: A Git-tracked file sitting on a reserved archive-control name whose bytes are
#: provably an artifact AUDAPACK generated on an earlier pack. The archive
#: carries a freshly generated control artifact under that name instead, so the
#: tracked stale copy is superseded -- recorded as an explicit, named omission
#: like ``tracked_deleted``, never a silent hole. A project-owned file on a
#: reserved name is NOT this: it keeps the fail-closed refusal.
REASON_SUPERSEDED_GENERATED_CONTROL = "superseded_generated_archive_control"

#: A direct child that is itself a Git worktree, but of a DIFFERENT product. The
#: omission is the same one it always was (``REASON_UNTRACKED_GIT_DIR``); this
#: reason travels on the component's own manifest record so the omission is
#: attributable to origin rather than looking like an unexplained gap.
REASON_NESTED_GIT_DIFFERENT_ORIGIN = "nested_git_different_origin"

#: One file inside an auxiliary component that the compact tier declined to make
#: mandatory -- media, an archive, a generated bundle, or a single oversized
#: payload. Named so the omission is auditable instead of silent.
REASON_COMPONENT_OPTIONAL = "nested_component_optional"

#: How far the nested-component scan reaches and what it refuses to carry.
#: Direct children only: a worktree two levels down is a checkout somebody made,
#: not a component this product ships. The cap is a bound on the scan, not a
#: byte budget for the archive.
MAX_NESTED_COMPONENTS = 64

#: Never archived from an auxiliary component, tracked or not: Git's own object
#: store, dependency trees, build output, generated bundles and tool homes.
#: These are reproducible from the sources that ARE archived.
COMPONENT_NEVER_ARCHIVE_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "out", "coverage", "target",
    "vendor", "__pycache__", ".venv", "venv", ".gradle", ".tox", ".mypy_cache",
    ".pytest_cache", ".turbo", ".cache", ".output",
})

#: Payloads an auxiliary component carries that must never become mandatory
#: merely because the upstream repository committed them. A tracked 38 MB WAV
#: corpus or a compressed vendor artifact is content, not the source code this
#: archive exists to carry; making it mandatory would let one committed asset
#: decide the weight of every pack. There is deliberately NO byte cap: a large
#: real source file stays mandatory, because the archive's job is to carry the
#: source and the manifest is where a weight delta is reported, not a policy
#: that quietly deletes mandatory code.
COMPONENT_OPTIONAL_SUFFIXES = frozenset({
    ".wav", ".mp3", ".mp4", ".mov", ".webm", ".avi", ".mkv", ".ogg", ".flac",
    ".psd", ".ai", ".sketch", ".fig", ".blend",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
    ".iso", ".dmg", ".jar", ".whl", ".crate",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".pdf",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".pdb", ".wasm", ".node",
    ".map", ".pyc", ".pyd", ".nupkg",
})

#: HARD SAFETY policy, separate from ordinary archive optimization. Only these
#: boundaries may override Git-tracked membership: actual secret/token material
#: and the reserved archive-control names. Ordinary noise (build output, media,
#: logs, recovery-looking names) deliberately does NOT appear here -- tracked
#: project truth beats noise heuristics, which is the entire point of T-190.
HARD_SAFETY_EXCLUDES = frozenset({
    "*.pre-redact",
    "*.secret",
    "*.secrets",
    "token.txt",
    "*.token",
    "secrets",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.ppk",
})

#: Archive-control paths no source file may own. A tracked file at one of these
#: names would collide with archive metadata written by the packer itself, so
#: it fails the pack as a NAMED reserved-name conflict
#: (``CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT``) -- never as a generic
#: hard-safety/secret refusal, and never by silently dropping the entry.
RESERVED_ARCHIVE_NAMES = frozenset({
    "_AUDAPACK_MANIFEST.json",
    ".audapack/manifest.json",
})

#: What proves a file on a reserved name is an artifact AUDAPACK itself wrote,
#: keyed by that name: the (key, value) pair its own generator always emits.
#: Names not listed here have no generated form, so nothing on them is ever a
#: supersession candidate.
GENERATED_CONTROL_MARKERS = {
    "_AUDAPACK_MANIFEST.json": ("product", "AUDAPACK"),
    ".audapack/manifest.json": ("kind", "audapack_source_inventory"),
}

#: A generated control artifact is metadata this packer writes, so it stays
#: small; the cap only stops a pathological file from being parsed whole.
_MAX_CONTROL_PROBE_BYTES = 64 * 1024 * 1024


def is_generated_archive_control(path: Path, rel: str) -> bool:
    """True when ``path`` holds an AUDAPACK-generated control artifact.

    Content, not name, is the proof: the file must parse as a JSON object AND
    carry the marker its own generator writes for that name. Anything else --
    unreadable, oversized, unparseable, or project-owned content that merely
    happens to sit on a reserved name -- returns False and keeps the
    fail-closed reserved-name refusal.
    """
    marker = GENERATED_CONTROL_MARKERS.get(rel)
    if marker is None:
        return False
    try:
        if path.stat().st_size > _MAX_CONTROL_PROBE_BYTES:
            return False
        # Regular files only: a tracked symlink on a reserved name would make
        # this read follow a link out of the source root, and nothing outside
        # the root is ever read to decide membership.
        if not stat_module.S_ISREG(path.lstat().st_mode):
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get(marker[0]) == marker[1]

#: Maximum parent directories searched for Git metadata before the source is
#: declared non-Git. Bounded by construction.
_GIT_METADATA_MAX_DEPTH = 64

#: Wall-clock bound for one Git subprocess. Inventory cost is O(few commands),
#: so the budget is generous but finite.
GIT_COMMAND_TIMEOUT_S = 120.0


class SourceInventoryError(Exception):
    """The source inventory cannot be established reliably; fail the pack.

    ``code`` is one of the ``CODE_*`` constants, ``rel`` (when known) is the
    first actionable source-relative path so an operator can go look.
    """

    def __init__(self, code: str, message: str, rel: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rel = rel

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.rel:
            return f"[{self.code}] {self.message} ({self.rel})"
        return f"[{self.code}] {self.message}"


def reserved_archive_conflict_message(rel: str, *, tracked: bool = True) -> str:
    """Operator message for a reserved archive-control collision.

    Names the conflicting path, what it collides with, and the two repairs that
    actually end the conflict. The classification says WHICH policy refused;
    this sentence says WHAT to do about it, so the Project Room can show both
    without the operator having to know AUDAPACK's internals.

    ``tracked`` selects the wording: a filesystem-mode source has no source
    control, and telling its operator to "remove it from source control" points
    them at a repository that is not involved.
    """
    origin = "Tracked source path" if tracked else "Source path"
    remedy = (
        "Remove it from source control or rename the project-owned file"
        if tracked
        else "Rename or delete the project-owned file"
    )
    return (
        f"{origin} '{rel}' conflicts with an AUDAPACK reserved "
        f"archive metadata path. {remedy} before packing."
    )


def _tracked_path_conflict(rel: str, *, tracked: bool = True) -> SourceInventoryError:
    """Classify a tracked path the packer may neither omit nor package.

    Two different policies land on this one predicate, and collapsing them
    into one diagnostic lied to the operator (measured on a real Project Room
    pack): ``_AUDAPACK_MANIFEST.json`` is a RESERVED_ARCHIVE_NAME -- a path
    AUDAPACK writes as generated archive metadata -- but it was reported as
    ``TRACKED_HARD_DENY_CONFLICT``/"hard-safety deny policy", i.e. as if the
    project were carrying a credential. That false positive costs real time:
    the operator goes looking for a secret instead of fixing a name collision.

    So the two are split:

    - a reserved archive-control name is a COLLISION, repaired in the project
      (remove the file from source control, or rename it) -- its own code and
      its own remediation sentence;
    - secret/token/private-key material keeps the hard-safety classification,
      unchanged, because that refusal is a disclosure boundary rather than a
      naming accident and "rename the file" would be terrible advice.

    Both stay fail-closed: the pack stops before any archive byte exists, and
    neither the source file nor the archive metadata is ever silently dropped.
    """
    if rel in RESERVED_ARCHIVE_NAMES:
        return SourceInventoryError(
            CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT if tracked else CODE_RESERVED_ARCHIVE_NAME_CONFLICT,
            reserved_archive_conflict_message(rel, tracked=tracked),
            rel=rel,
        )
    return SourceInventoryError(
        CODE_TRACKED_HARD_DENY_CONFLICT,
        "tracked path matches the hard-safety deny policy; refusing to "
        "silently omit or package it",
        rel=rel,
    )


class SourceOrigin:
    """Origin vocabulary for inventory entries (SRC-046 manifest contract)."""

    TRACKED = "tracked"
    UNTRACKED = "untracked"
    FILESYSTEM = "filesystem"


ORIGIN_TRACKED = SourceOrigin.TRACKED
ORIGIN_UNTRACKED = SourceOrigin.UNTRACKED
ORIGIN_FILESYSTEM = SourceOrigin.FILESYSTEM


# ---------------------------------------------------------------------------
# Bounded protected-control-plane discovery (T-188 P0)
# ---------------------------------------------------------------------------

#: Git ignore rules hide material the fidelity contract protects as priority-1
#: live audit control plane (this repository's own .gitignore carries
#: ``.saipen/intake/`` and ``audit/``). ``git ls-files --others
#: --exclude-standard`` never enumerates those paths, so ``_priority_for``
#: never sees them and they vanish from the archive with ``excluded=0``.
#:
#: The overlay re-enumerates ONLY the semantic protected locations below,
#: with one bounded ``scandir`` per location and no recursion beyond them.
#: Unrelated ignored trees (``.saipen/recovery/``, ``_RECOVERY_*/``, caches,
#: build output, media) are never touched.
PROTECTED_CONTROL_PLANE_LOCATIONS = (
    # (root_relative_dir, recursive)
    (".saipen/intake/active", True),
    ("audit", False),
    (".saipen/audit", False),
)


def _is_protected_control_plane_rel(rel: str) -> bool:
    """Semantic membership test for the bounded protected locations.

    Mirrors the priority-1 rules in ``_priority_for`` exactly:
    ``.saipen/intake/active/**``, ``audit/<numeric>.md`` and
    ``.saipen/audit/<numeric>.md``.
    """
    segs = rel.split("/")
    name_lower = segs[-1].lower()
    if len(segs) >= 4 and segs[0] == ".saipen" and segs[1] == "intake" and segs[2] == "active":
        return True
    if len(segs) == 2 and segs[0] == "audit" and _is_numeric_md_name(name_lower):
        return True
    if (
        len(segs) == 3
        and segs[0] == ".saipen"
        and segs[1] == "audit"
        and _is_numeric_md_name(name_lower)
    ):
        return True
    return False


def _manifest_required_candidates(manifest_path: Path, manifest_rel: str) -> list[str]:
    """Explicit required paths named by a project MANIFEST.json, bounded.

    Same conservative contract as the filesystem mode's
    ``_extract_manifest_required`` closure (MANIFEST.json only, JSON object,
    relative paths, no absolute/.. escape) but WITHOUT the "must already be
    in inventory" check -- because Git ignore may have hidden exactly those
    paths from enumeration. Only paths the manifest names are probed with
    one existence stat; nothing is walked.
    """
    import json as _json

    try:
        data = _json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("required")
    if not isinstance(raw, (list, tuple, set, dict, str)):
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    elif isinstance(raw, dict):
        items = list(raw.keys())
    else:
        items = [raw]
    manifest_dir = manifest_rel.rpartition("/")[0]
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            p_str = item
        elif isinstance(item, dict):
            p_str = item.get("path") or item.get("file") or item.get("rel") or item.get("name")
            if not isinstance(p_str, str):
                continue
        else:
            continue
        p_norm = p_str.strip().replace("\\", "/")
        if not p_norm:
            continue
        if p_norm.startswith("/") or len(p_norm) >= 2 and p_norm[1] == ":":
            continue
        parts = [seg for seg in p_norm.split("/") if seg]
        if not parts or any(seg in ("..", ".") for seg in parts):
            continue
        clean = "/".join(parts)
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        out.append(f"{manifest_dir}/{clean}" if manifest_dir else clean)
    return out


def _is_numeric_md_name(name_lower: str) -> bool:
    if not name_lower.endswith(".md"):
        return False
    return name_lower[:-3].isdigit()


def discover_protected_control_plane(source_resolved: Path, cancel_event=None) -> list[str]:
    """Boundedly enumerate ignored-but-protected live control-plane files.

    Returns source-relative POSIX paths that exist on disk inside the
    protected locations, ignoring symlinks (validated later like any other
    entry) and never descending past the declared locations. This is NOT a
    generic ignored-tree walk: three fixed locations, one scandir each.
    """
    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    found: list[str] = []
    for rel_dir, recursive in PROTECTED_CONTROL_PLANE_LOCATIONS:
        if _cancelled():
            raise SourceInventoryError(CODE_INVENTORY_INCONSISTENT, "inventory cancelled")
        base = source_resolved.joinpath(*rel_dir.split("/"))
        try:
            is_dir = base.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        if recursive:
            # Bounded: only below this one declared root, and symlinked
            # directories are never followed (same containment rule as the
            # walk fallback).
            stack = [base]
            while stack:
                if _cancelled():
                    raise SourceInventoryError(
                        CODE_INVENTORY_INCONSISTENT, "inventory cancelled"
                    )
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        entries = list(it)
                except OSError:
                    continue
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            found.append(
                                Path(entry.path).relative_to(source_resolved).as_posix()
                            )
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                    except OSError:
                        continue
                    found.append(
                        Path(entry.path).relative_to(source_resolved).as_posix()
                    )
        else:
            try:
                with os.scandir(base) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        found.append(
                            Path(entry.path).relative_to(source_resolved).as_posix()
                        )
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found.append(Path(entry.path).relative_to(source_resolved).as_posix())
    return found


# ---------------------------------------------------------------------------
# Nested same-product Git components
# ---------------------------------------------------------------------------


def normalize_origin_url(url: str) -> str:
    """One comparable identity for one Git remote.

    Git accepts several spellings of the same remote, and comparing them as raw
    strings would classify a product's own component as foreign because someone
    cloned it over SSH instead of HTTPS. Scheme, ``.git`` suffix, trailing
    slash, credentials and case all normalize away; what survives is
    ``host/path``, which is what "same product" actually means here.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    if raw.endswith(".git"):
        raw = raw[: -len(".git")]
    if "://" in raw:
        scheme, _, remainder = raw.partition("://")
        if scheme.lower() in {"http", "https", "ssh", "git"}:
            raw = remainder
        else:
            raw = remainder
    if "@" in raw.split("/", 1)[0]:
        # scp-style user@host:path -- drop the user, keep host:path.
        raw = raw.split("@", 1)[1]
    raw = raw.replace(":", "/", 1) if ":" in raw.split("/", 1)[0] else raw
    return raw.strip("/").lower()


@dataclass
class NestedGitComponent:
    """A direct child of the source root that is itself a Git worktree."""

    #: Path relative to the SOURCE root (e.g. ``zcode``), which is also the
    #: prefix every member entry keeps -- archive paths stay rooted under the
    #: real project layout and are never flattened.
    rel: str
    path: Path
    #: Normalized ``host/path`` identity, or "" when the child has no origin.
    origin: str = ""
    root_branch: str = ""
    head: str = ""
    included: bool = False
    reason: str = ""
    file_count: int = 0
    included_bytes: int = 0

    def manifest_record(self) -> dict:
        return {
            "path": self.rel,
            "origin": self.origin,
            "root_branch": self.root_branch,
            "head": self.head,
            "included": bool(self.included),
            "reason": self.reason,
            "files": int(self.file_count),
            "bytes": int(self.included_bytes),
        }


def _component_git_text(component: Path, args: Iterable[str], *, what: str) -> str:
    """One read-only Git query inside a component, fail-closed by name.

    A component we cannot interrogate is not a component we may omit: the
    failure names the directory so the operator knows which checkout is broken.
    """
    try:
        proc = _run_git(list(args), cwd=component)
    except SourceInventoryError as exc:
        raise SourceInventoryError(
            CODE_NESTED_COMPONENT_INVENTORY,
            f"nested component {component.name!r} could not be inventoried ({what}): {exc}",
            rel=component.name,
        ) from exc
    return _decode(proc.stdout, f"nested component {component.name} {what}").strip()


def _normalized_component_origin(worktree: Path) -> str:
    """Normalized origin identity of one worktree, or "" when it has no remote.

    "No such remote" is an answer, not a failure; a broken query is still
    fail-closed by name through :func:`_component_git_text`.
    """
    if "origin" not in _component_git_text(worktree, ["remote"], what="remotes").split():
        return ""
    return normalize_origin_url(
        _component_git_text(worktree, ["remote", "get-url", "origin"], what="origin")
    )


def _component_head_metadata(worktree: Path, args: Iterable[str], *, what: str) -> str:
    """Branch/HEAD of a component, empty when it has none yet.

    A freshly cloned-then-emptied component has an unborn HEAD, exactly like a
    freshly initialised root; that is an answer, not a broken checkout. Only the
    CONTENT queries a component is judged on stay fail-closed.
    """
    try:
        return _component_git_text(worktree, args, what=what)
    except SourceInventoryError:
        return ""


def discover_nested_git_components(source: Path, *, cancel_event=None) -> list[NestedGitComponent]:
    """Bounded scan of DIRECT children that are Git worktrees of their own.

    One level, one ``.git`` probe per child, and at most
    ``MAX_NESTED_COMPONENTS`` of them. No recursive walk of ignored trees and
    no reading of prose: a child is recognised solely by being a worktree, and
    its identity comes from Git, never from its name.
    """
    source_resolved = Path(source).resolve()
    found: list[NestedGitComponent] = []
    try:
        children = sorted(source_resolved.iterdir(), key=lambda p: p.name)
    except OSError:
        return found
    for child in children:
        if cancel_event is not None and cancel_event.is_set():
            break
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        if len(found) >= MAX_NESTED_COMPONENTS:
            break
        if not (child / ".git").exists():
            continue
        # A checkout with no origin has no identity to compare, and "probably
        # the same product" is not a proof: it keeps the safe omission. Only a
        # real query failure is fail-closed -- "no such remote" is an answer.
        origin = _normalized_component_origin(child)
        found.append(NestedGitComponent(
            rel=child.name,
            path=child,
            origin=origin,
            root_branch=_component_head_metadata(
                child, ["rev-parse", "--abbrev-ref", "HEAD"], what="branch"
            ),
            head=_component_head_metadata(child, ["rev-parse", "HEAD"], what="HEAD"),
        ))
    return found


def _component_rel_is_never_archived(inner: str) -> bool:
    """Dependency trees, build output and Git's own store, one segment deep."""
    for segment in inner.split("/"):
        lowered = segment.lower()
        if lowered in COMPONENT_NEVER_ARCHIVE_DIRS:
            return True
        if lowered.startswith(".next"):
            return True
    return False


def build_nested_component_entries(
    component: NestedGitComponent,
    excludes: set[str],
    *,
    cancel_event=None,
) -> dict[str, SourceInventoryEntry]:
    """Inventory one canonical component under its real nested paths.

    Deliberately NOT ``build_git_inventory`` called again: at the root, a
    tracked file is project truth and therefore mandatory, which inside an
    auxiliary component would make every committed asset mandatory too. Here a
    tracked file still faces a compact tier -- source and configuration stay
    mandatory, media/archives/generated bundles and a single oversized payload
    do not -- and every decline is a named exclusion, never a silent hole.
    """
    root = component.path
    entries: dict[str, SourceInventoryEntry] = {}
    hard = _build_matcher(set(HARD_SAFETY_EXCLUDES))
    configured = _build_matcher(set(excludes) | set(MANDATORY_EXCLUDES))
    lower_seen: dict[str, str] = {}

    def _claim_lower(rel: str) -> None:
        lower = rel.lower()
        other = lower_seen.get(lower)
        if other is not None and other != rel:
            raise SourceInventoryError(
                CODE_NESTED_COMPONENT_INVENTORY,
                f"case-collision inside nested component {component.rel!r}: "
                f"'{other}' and '{rel}' normalize to one path",
                rel=rel,
            )
        lower_seen[lower] = rel

    def _tier(inner: str, size: int) -> tuple[bool, Optional[str]]:
        """The compact component decision, applied to tracked and untracked alike."""
        name_lower = inner.rpartition("/")[2].lower()
        if hard(inner.lower()):
            # Hard safety is not a compact-tier opinion: a secret inside a
            # component fails the whole pack exactly as it does at the root.
            raise _tracked_path_conflict(f"{component.rel}/{inner}")
        if _component_rel_is_never_archived(inner):
            return False, "component_reproducible_tree"
        if name_lower.endswith(tuple(COMPONENT_OPTIONAL_SUFFIXES)):
            return False, REASON_COMPONENT_OPTIONAL
        if configured(inner.lower()):
            return False, exclusion_reason_for(inner.lower(), name_lower)
        return True, None

    # Four Git content commands per component, never one per file.
    try:
        tracked_records = _parse_ls_files_stage(
            _run_git(["ls-files", "--cached", "-s", "-z"], cwd=root)
        )
        status = _parse_status_porcelain(
            _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=no"], cwd=root)
        )
        untracked_paths = _parse_ls_files_plain(
            _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
        )
    except SourceInventoryError as exc:
        raise SourceInventoryError(
            CODE_NESTED_COMPONENT_INVENTORY,
            f"nested component {component.rel!r} could not be inventoried: {exc}",
            rel=component.rel,
        ) from exc
    deleted = {rel for rel, xy in status.items() if "D" in xy}

    if cancel_event is not None and cancel_event.is_set():
        raise SourceInventoryError(
            CODE_NESTED_COMPONENT_INVENTORY,
            "component inventory cancelled",
            rel=component.rel,
        )

    for mode, raw in tracked_records:
        if mode == "160000":
            raise SourceInventoryError(
                CODE_NESTED_COMPONENT_INVENTORY,
                f"nested component {component.rel!r} contains a submodule; "
                "an explicit preserved packaging policy is required",
                rel=f"{component.rel}/{raw}",
            )
        inner = validate_rel_path(raw)
        rel = f"{component.rel}/{inner}"
        if inner in deleted:
            _claim_lower(rel)
            entries[rel] = SourceInventoryEntry(
                rel=rel, origin=ORIGIN_TRACKED, size=0,
                include=False, reason=REASON_TRACKED_DELETED,
            )
            continue
        full = root.joinpath(*inner.split("/"))
        st = _stat_existing(full, inner)
        if mode == "120000" or stat_module.S_ISLNK(st.st_mode):
            entry = _resolve_symlink_entry(root, full, inner, ORIGIN_TRACKED)
            _claim_lower(rel)
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_TRACKED,
                size=entry.size,
                mtime_ns=entry.mtime_ns,
                st_ino=entry.st_ino,
                st_dev=entry.st_dev,
                include=True,
                priority=1,
                symlink=True,
            )
            continue
        if not stat_module.S_ISREG(st.st_mode):
            raise SourceInventoryError(
                CODE_NESTED_COMPONENT_INVENTORY,
                f"nested component {component.rel!r} tracks a path that is not a regular file",
                rel=rel,
            )
        include, reason = _tier(inner, st.st_size)
        _claim_lower(rel)
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_TRACKED,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=include,
            reason=reason,
            priority=1,
        )

    for raw in untracked_paths:
        inner = validate_rel_path(raw[:-1] if raw.endswith("/") else raw)
        full = root.joinpath(*inner.split("/"))
        try:
            st = _stat_existing(full, inner)
        except SourceInventoryError:
            continue
        if stat_module.S_ISDIR(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
            continue
        rel = f"{component.rel}/{inner}"
        include, reason = _tier(inner, st.st_size)
        _claim_lower(rel)
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_UNTRACKED,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=include,
            reason=reason,
            priority=1,
        )
    return entries


def nested_component_manifest_section(inventory: SourceInventory) -> list[dict]:
    """The ``nested_git_components`` manifest section, always well-formed.

    Backward compatible by construction: a project with no components reports
    an empty list rather than omitting the key, and a reader written before
    this feature ignores it.
    """
    return [item.manifest_record() for item in getattr(inventory, "nested_git_components", [])]


@dataclass(frozen=True)
class SourceInventoryEntry:
    """One immutable pre-write evidence record for a source file.

    ``rel`` is the normalized POSIX path relative to the source root (exact
    case). ``size``/``mtime_ns``/``st_ino``/``st_dev`` are the frozen stat
    identity the writer re-proves before reading; ``include``/``reason``
    carry the membership decision (tracked paths are ``include=True`` unless
    the pack failed earlier on hard safety).
    """

    rel: str
    origin: str
    size: int = 0
    mtime_ns: int = 0
    st_ino: int = 0
    st_dev: int = 0
    include: bool = True
    reason: Optional[str] = None
    priority: int = 1
    #: True when the entry is a symlink whose target was resolved INSIDE the
    #: source root at freeze time; the writer re-proves that resolution.
    symlink: bool = False


@dataclass
class SourceInventory:
    """The complete, validated, frozen source set for one pack."""

    mode: str                       # "git" | "filesystem"
    source: Path
    entries: dict[str, SourceInventoryEntry] = field(default_factory=dict)
    #: Tracked paths Git records as deleted in the worktree. Never restored,
    #: never silently dropped: they are excluded entries with
    #: ``reason=tracked_deleted`` and are reported in the archive manifest.
    tracked_deleted: list[str] = field(default_factory=list)
    #: Reserved archive-control paths that were tracked as AUDAPACK's own
    #: generated artifacts and are superseded by the freshly generated control
    #: artifact in this archive. Reported next to ``tracked_deleted`` so the
    #: omission is always visible, never a silent inventory hole.
    superseded_control: list[str] = field(default_factory=list)
    git_head: str = ""
    git_dirty: bool = False
    #: Same-product components found as DIRECT children of the root that are
    #: Git worktrees of their own. Each is either inventoried under its real
    #: nested prefix (same origin) or named in the manifest as an omission
    #: (different origin, or no origin to compare). Never silently dropped.
    nested_git_components: list[NestedGitComponent] = field(default_factory=list)
    #: Machine-readable SAIPEN snapshot verdict for this inventory, or None
    #: for a project that carries no protocol memory. Computed against what
    #: actually survived classification, never against what was merely found
    #: on disk -- an evidence file an operator exclude removed must still be
    #: reported missing.
    saipen: Optional[dict] = None

    @property
    def tracked_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.origin == ORIGIN_TRACKED)

    @property
    def untracked_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.origin == ORIGIN_UNTRACKED)

    @property
    def filesystem_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.origin == ORIGIN_FILESYSTEM)

    def included_entries(self) -> list[SourceInventoryEntry]:
        """Included entries in deterministic (path) order -- the write set."""
        return sorted(
            (e for e in self.entries.values() if e.include),
            key=lambda e: e.rel,
        )

    def excluded_entries(self) -> list[SourceInventoryEntry]:
        return sorted(
            (e for e in self.entries.values() if not e.include),
            key=lambda e: e.rel,
        )

    def git_summary(self) -> str:
        """Compact operator summary, e.g. ``Git: 312 tracked + 4 untracked, 2 deleted``."""
        return (
            f"Git: {self.tracked_count} tracked + {self.untracked_count} untracked, "
            f"{len(self.tracked_deleted)} deleted"
        )


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def validate_rel_path(raw: str) -> str:
    """Normalize and validate one Git/file relative path, fail-closed.

    Returns the normalized POSIX relative path. Rejects absolute paths, drive
    letters, root escape (``..``), empty segments and NUL bytes with
    ``PATH_INVALID``.
    """
    if not raw or "\0" in raw:
        raise SourceInventoryError(CODE_PATH_INVALID, "empty or NUL-bearing path", rel=raw)
    if raw.startswith("/") or raw.startswith("\\"):
        raise SourceInventoryError(CODE_PATH_INVALID, "absolute path rejected", rel=raw)
    if len(raw) >= 2 and raw[1] == ":":
        raise SourceInventoryError(CODE_PATH_INVALID, "drive-absolute path rejected", rel=raw)
    parts = raw.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise SourceInventoryError(
                CODE_PATH_INVALID,
                "path escapes the source root or is not normalized",
                rel=raw,
            )
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Bounded Git execution
# ---------------------------------------------------------------------------


def _decode(raw: bytes, context: str, rel: str = "") -> str:
    """Deterministic UTF-8 decoding; undecodable Git output fails closed."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceInventoryError(
            CODE_INVENTORY_INCONSISTENT,
            f"git {context} output is not valid UTF-8: {exc}",
            rel=rel,
        ) from exc


def _run_git(args: Iterable[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one bounded, captured, shell-free Git command with an explicit cwd.

    Spawns through :func:`audapack.procutil.run_hidden` so a Git-mode pack
    from the windowless GUI/Bridge never allocates a console: without it,
    the five-command inventory (2x rev-parse, status, 2x ls-files) flashes a
    black window per command and steals focus. procutil stays the single
    owner of hidden-spawn policy; no Windows flags are duplicated here.
    """
    from audapack.procutil import run_hidden

    argv = ["git", "-c", "core.quotepath=false", *args]
    try:
        proc = run_hidden(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=GIT_COMMAND_TIMEOUT_S,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise SourceInventoryError(
            CODE_GIT_UNAVAILABLE, f"git executable not found: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceInventoryError(
            CODE_GIT_UNAVAILABLE,
            f"git command timed out after {GIT_COMMAND_TIMEOUT_S:.0f}s: {args[0] if args else 'git'}",
        ) from exc
    except OSError as exc:
        raise SourceInventoryError(
            CODE_GIT_UNAVAILABLE, f"git command failed to run: {exc}"
        ) from exc
    if proc.returncode != 0:
        stderr = _decode(proc.stderr[:200], "stderr").strip()
        raise SourceInventoryError(
            CODE_GIT_UNAVAILABLE,
            f"git {' '.join(args[:2])} failed (rc={proc.returncode}): {stderr}",
        )
    return proc


def _find_git_metadata(source: Path) -> Optional[Path]:
    """Nearest ancestor (including ``source`` itself) containing ``.git``.

    ``.git`` may be a directory (normal repo) or a file (worktree/link), both
    count as Git metadata. Bounded upward walk.
    """
    probe = source if source.is_dir() else source.parent
    probe = probe.resolve()
    for _ in range(_GIT_METADATA_MAX_DEPTH):
        if (probe / ".git").exists() or (probe / ".git").is_file():
            return probe
        parent = probe.parent
        if parent == probe:
            return None
        probe = parent
    return None


def detect_git_worktree(source: Path) -> Optional[Path]:
    """The Git worktree root when ``source`` carries Git metadata, else None.

    Git metadata that exists but cannot answer ``rev-parse`` fails closed
    (``GIT_UNAVAILABLE``) -- it never silently downgrades to a filesystem walk.
    """
    meta_root = _find_git_metadata(source)
    if meta_root is None:
        return None
    proc = _run_git(["rev-parse", "--show-toplevel"], cwd=source)
    top = _decode(proc.stdout, "rev-parse").strip()
    if not top:
        raise SourceInventoryError(
            CODE_GIT_UNAVAILABLE,
            "git metadata exists but rev-parse --show-toplevel returned nothing",
        )
    return Path(top)


# ---------------------------------------------------------------------------
# Git inventory
# ---------------------------------------------------------------------------


def _parse_status_porcelain(proc: subprocess.CompletedProcess) -> dict[str, str]:
    """``rel -> XY`` for ``git status --porcelain=v1 -z --untracked-files=no``.

    Rename/copy records carry a second NUL-separated field (the original path)
    which is consumed and ignored here.
    """
    text = _decode(proc.stdout, "status")
    fields = text.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue
        xy = record[:2]
        path = record[2:].lstrip()
        if not path:
            continue
        if xy and xy[0] in ("R", "C"):
            i += 1  # original path follows as its own NUL field
        # ``-z`` records carry a leading space inside XY for unstaged worktree
        # states (e.g. b" D gone.py"); strip only the XY columns, never path
        # characters.
        result[path] = xy
    return result


def _parse_ls_files_stage(proc: subprocess.CompletedProcess) -> list[tuple[str, str]]:
    """``[(mode, path)]`` from ``git ls-files --cached -s -z``."""
    text = _decode(proc.stdout, "ls-files")
    records = []
    for record in text.split("\0"):
        if not record:
            continue
        meta, sep, path = record.partition("\t")
        if not sep or not path:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                f"unparsable git ls-files record: {record[:80]!r}",
            )
        mode = meta.split(" ")[0]
        records.append((mode, path))
    return records


def _parse_ls_files_plain(proc: subprocess.CompletedProcess) -> list[str]:
    """Paths from ``git ls-files --others --exclude-standard -z``."""
    text = _decode(proc.stdout, "ls-files")
    return [record for record in text.split("\0") if record]


def _stat_existing(full: Path, rel: str) -> os.stat_result:
    try:
        return os.lstat(full)
    except OSError as exc:
        raise SourceInventoryError(
            CODE_INVENTORY_INCONSISTENT,
            f"inventory path vanished or is unreadable: {exc}",
            rel=rel,
        ) from exc


def _resolve_symlink_entry(
    source_resolved: Path, full: Path, rel: str, origin: str
) -> SourceInventoryEntry:
    """Validate one symlink inventory member, fail-closed.

    A symlink that resolves outside the selected project root is refused
    (``SYMLINK_UNSAFE``). A link resolving to a regular file inside the root is
    inventoried with the TARGET's stat identity -- the archive carries the
    target's bytes, never a link target chosen at write time.
    """
    try:
        target = Path(os.path.realpath(full))
        root_real = Path(os.path.realpath(source_resolved))
        target_inside = target == root_real or root_real in target.parents
        if not target_inside or not target.is_file():
            raise SourceInventoryError(
                CODE_SYMLINK_UNSAFE,
                "symlink does not resolve to a regular file inside the project root",
                rel=rel,
            )
        st = os.stat(target)
    except OSError as exc:
        raise SourceInventoryError(
            CODE_SYMLINK_UNSAFE, f"symlink target could not be resolved: {exc}", rel=rel
        ) from exc
    return SourceInventoryEntry(
        rel=rel,
        origin=origin,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        st_ino=st.st_ino,
        st_dev=st.st_dev,
        include=True,
        priority=1,
        symlink=True,
    )


def evidence_omission_reason(entries: dict, rel: str) -> str:
    """WHY one required artifact did not survive into the archive.

    The reason is the inventory's OWN vocabulary (``configured_ignore`` for an
    explicit operator exclude, ``secret_policy`` for hard safety,
    ``tracked_deleted``, ``untracked_git_directory``, a fidelity reason ...),
    so a reviewer reads one language whichever stage removed the file. A file
    the contract required that never reached the inventory at all is named as
    such rather than being silently absent from the report.
    """
    entry = entries.get(rel)
    if entry is None:
        return saipen_evidence.REASON_EVIDENCE_NOT_INVENTORIED
    if getattr(entry, "include", False):
        return ""
    return getattr(entry, "reason", "") or saipen_evidence.REASON_EVIDENCE_UNSUPPORTED


def saipen_verdict(
    source_resolved: Path,
    entries: dict,
    *,
    collection=None,
) -> Optional[dict]:
    """The SAIPEN snapshot verdict for a FINISHED inventory.

    Judged on what SURVIVED into the archive, not on what was found on disk and
    not on what the collector discovered. Three sets have to be reconciled, and
    only the last one is the archive:

        mandatory evidence required by the contract
        + conditional evidence the contract makes required BECAUSE IT EXISTS
        - everything the packaging policy then removed

    An evidence file an operator's ``always_exclude`` removed, one hard safety
    refused, one a size/count policy dropped, one that could not be read, or
    one that was never inventoried at all: each is reported as an omission with
    its reason, so the package can never claim authority over state it dropped.
    Optional evidence that is absent is reported and changes nothing -- the
    contract already said its absence is honest.

    Returns None for a project that carries no protocol memory, leaving
    non-SAIPEN packaging untouched.
    """
    if collection is None:
        collection = saipen_evidence.collect_for_inventory(source_resolved)
    contract = collection.contract
    if not contract.detected:
        return None

    included = {rel for rel, entry in entries.items() if getattr(entry, "include", False)}
    return saipen_evidence.evaluate(
        collection,
        included=included,
        reason_for=lambda rel: evidence_omission_reason(entries, rel),
    )


def _classify_untracked(
    rel: str,
    size: int,
    *,
    mandatory,
    always_excl,
    always_incl,
    configured,
    saipen_declared: bool = False,
) -> tuple[bool, Optional[str], int]:
    """Untracked material passes the ordinary AUDAPACK noise/fidelity policy.

    Exact mirror of the non-media decision cascade in
    ``fidelity.build_fidelity_plan`` (safety -> always_exclude ->
    always_include -> configured -> include), so untracked files can never
    drift from the policy a non-Git pack would apply.

    ``saipen_declared`` marks a path the protocol's own manifest named as
    audit evidence. Such a path outranks the GENERIC configured excludes --
    without that, a default pattern like ``logs`` would silently swallow
    ``.saipen/logs/**``, the sealed LOG segments that make the event chain
    resolvable, and the package would again look healthy while missing the
    evidence. It does NOT outrank secret policy or an operator's explicit
    ``always_exclude``: those stay authoritative, and the snapshot verdict
    reports the resulting omission instead of hiding it.
    """
    rel_lower = rel.lower()
    name_lower = rel.rpartition("/")[2].lower()
    if mandatory(rel_lower):
        return False, "secret_policy", 3
    if always_excl(rel_lower):
        return False, "configured_ignore", 3
    if always_incl(rel_lower):
        return True, None, 1
    if saipen_declared:
        return True, None, 1
    if configured(rel_lower):
        # T-188 P0: Git ignore alone must never exclude protected control
        # plane -- the overlay enumerates those paths, so this cascade is the
        # SAME policy a filesystem-mode pack applies. Explicit AUDAPACK
        # configured excludes retain their existing precedence (requirement
        # 6); only hard safety may override a protected path.
        return False, exclusion_reason_for(rel_lower, name_lower), 3
    return True, None, _priority_for(rel_lower, name_lower, size)


def build_git_inventory(
    source: Path,
    excludes: set[str],
    *,
    always_include: Optional[list[str]] = None,
    always_exclude: Optional[list[str]] = None,
    supersedable_reserved: Optional[frozenset] = None,
    cancel_event=None,
) -> SourceInventory:
    """Frozen ``tracked_existing UNION untracked_nonignored`` inventory.

    Exactly four Git content commands plus one rev-parse, regardless of tree
    size -- never one subprocess per file. Git-ignored material is absent by
    construction: it is never enumerated, so an ignored recovery tree costs
    nothing and cannot re-enter the archive.

    ``supersedable_reserved`` names the reserved control paths THIS pack
    regenerates. A tracked file on one of them is superseded when its bytes are
    an AUDAPACK-generated artifact; on any other reserved name, or on a reserved
    name holding project-owned content, the pack still fails closed.
    """
    source_resolved = source.resolve()

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # 1. HEAD (empty on an unborn branch -- still a usable inventory).
    git_head = ""
    try:
        head_proc = _run_git(["rev-parse", "HEAD"], cwd=source)
        git_head = _decode(head_proc.stdout, "rev-parse HEAD").strip()
    except SourceInventoryError:
        git_head = ""

    # 2. Tracked worktree changes: dirty flag + the authoritative deletion set.
    status = _parse_status_porcelain(
        _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=no"], cwd=source)
    )
    git_dirty = any(xy != "??" for xy in status.values())
    git_deleted = {rel for rel, xy in status.items() if "D" in xy}

    if _cancelled():
        raise SourceInventoryError(CODE_INVENTORY_INCONSISTENT, "inventory cancelled")

    # 3. Tracked index entries with modes (gitlinks/symlinks visible).
    tracked_records = _parse_ls_files_stage(
        _run_git(["ls-files", "--cached", "-s", "-z"], cwd=source)
    )
    if _cancelled():
        raise SourceInventoryError(CODE_INVENTORY_INCONSISTENT, "inventory cancelled")

    # 4. Untracked non-ignored files. Git applies the ignore rules itself, so
    # ignored recovery/cache trees are never enumerated at all.
    untracked_paths = _parse_ls_files_plain(
        _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=source)
    )

    # 4b. Protected control-plane overlay (T-188 P0): one bounded semantic
    # discovery stage that re-enumerates ignored files inside the protected
    # live audit locations ONLY, so gitignore cannot erase them. Paths that
    # Git itself already reported are skipped; the rest join the same
    # validation and classification cascade as untracked material below.
    untracked_set = set(untracked_paths)
    overlay_paths = [
        p for p in discover_protected_control_plane(source_resolved, cancel_event)
        if p not in untracked_set
    ]

    # 4c. Same-product nested components. A DIRECT child that is a Git worktree
    # of its own is compared by ORIGIN IDENTITY, never by directory name: equal
    # normalized origin means it is this product's own component and its source
    # belongs in the archive; anything else keeps the safe omission below and is
    # named in the manifest instead of vanishing.
    try:
        root_origin = _normalized_component_origin(source_resolved)
    except SourceInventoryError:
        root_origin = ""
    nested_components = discover_nested_git_components(
        source_resolved, cancel_event=cancel_event
    )
    same_origin_components: dict[str, NestedGitComponent] = {}
    for component in nested_components:
        component.reason = (
            REASON_NESTED_GIT_DIFFERENT_ORIGIN
            if not root_origin or not component.origin or component.origin != root_origin
            else ""
        )
        if not component.reason:
            same_origin_components[component.rel] = component

    mandatory = _build_matcher(set(MANDATORY_EXCLUDES))
    hard = _build_matcher(set(HARD_SAFETY_EXCLUDES))
    always_excl = _build_matcher({p for p in (always_exclude or [])})
    always_incl = _build_matcher({p for p in (always_include or [])})
    configured = _build_matcher(set(excludes))

    entries: dict[str, SourceInventoryEntry] = {}
    lower_seen: dict[str, str] = {}
    tracked_deleted: list[str] = []
    superseded_control: list[str] = []

    def _supersede_generated_control(rel: str, origin: str, *, tracked: bool) -> bool:
        """Apply the ONE reserved-control verdict and record any supersession.

        Returns True when ``rel`` was a reserved control name handled here, so
        the caller can skip its ordinary classification. Narrow on purpose,
        because the alternative -- omitting a source path -- is the exact
        failure T-190 exists to prevent. Everything the verdict does not accept
        keeps the fail-closed reserved-name refusal.
        """
        entry = _reserved_control_verdict(
            rel,
            source_resolved,
            supersedable_reserved,
            origin=origin,
            tracked=tracked,
        )
        if entry is None:
            return False
        entries[rel] = entry
        if rel not in superseded_control:
            superseded_control.append(rel)
        return True

    def _claim_lower(rel: str) -> None:
        lower = rel.lower()
        other = lower_seen.get(lower)
        if other is not None and other != rel:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                f"case-collision ambiguity: '{other}' and '{rel}' normalize to one path",
                rel=rel,
            )
        lower_seen[lower] = rel

    for mode, raw_path in tracked_records:
        if mode == "160000":
            raise SourceInventoryError(
                CODE_SUBMODULE_REQUIRES_EXPLICIT_POLICY,
                "git submodule (gitlink) requires an explicit preserved packaging policy",
                rel=raw_path,
            )
        rel = validate_rel_path(raw_path)
        if rel in entries:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "duplicate normalized tracked path",
                rel=rel,
            )
        _claim_lower(rel)
        if _supersede_generated_control(rel, ORIGIN_TRACKED, tracked=True):
            continue
        if hard(rel):
            raise _tracked_path_conflict(rel)
        if rel in git_deleted:
            # A worktree deletion Git records is an explicit state, not an
            # inventory hole; the file is absent by definition, so do not
            # stat it here. The tracked_deleted classification happens in
            # the pass below.
            continue
        full = source_resolved.joinpath(*rel.split("/"))
        st = _stat_existing(full, rel)
        if stat_module.S_ISDIR(st.st_mode):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "tracked path is a directory in the worktree",
                rel=rel,
            )
        if mode == "120000" or stat_module.S_ISLNK(st.st_mode):
            entries[rel] = _resolve_symlink_entry(source_resolved, full, rel, ORIGIN_TRACKED)
            continue
        if not stat_module.S_ISREG(st.st_mode):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "tracked path is not a regular file",
                rel=rel,
            )
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_TRACKED,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=True,
            priority=1,
        )

    for raw_path in untracked_paths:
        # Git reports an untracked DIRECTORY (never recursed into because it
        # is itself another Git worktree/clone) as "dir/" with a trailing
        # slash and no per-file records. Refusing the trailing slash as
        # PATH_INVALID turned every repository carrying such a sandbox into a
        # failed pack (measured on the real SAIPEN tree). Git cannot enumerate
        # inside the nested worktree, so the content is not project truth this
        # inventory can represent: record the directory as an explicit
        # excluded entry with a named reason -- never a silent hole, never a
        # crash, never a recursive walk into the nested clone.
        if raw_path.endswith("/"):
            rel = validate_rel_path(raw_path.rstrip("/"))
            if rel in entries:
                continue
            if rel in same_origin_components:
                # Superseded by the component's own ingestion phase below --
                # never a second ingestion trigger. Outer Git policy may hide
                # or reveal this directory at will; membership was decided by
                # bounded direct-child discovery plus origin identity, so this
                # placeholder records nothing: the members arrive with their
                # real prefixes from that phase, and an
                # "untracked_git_directory" omission would contradict them.
                continue
            _claim_lower(rel)
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_UNTRACKED,
                include=False,
                reason=REASON_UNTRACKED_GIT_DIR,
            )
            continue
        rel = validate_rel_path(raw_path)
        if rel in entries:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "path reported as both tracked and untracked",
                rel=rel,
            )
        _claim_lower(rel)
        full = source_resolved.joinpath(*rel.split("/"))
        st = _stat_existing(full, rel)
        if stat_module.S_ISDIR(st.st_mode):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "untracked path is a directory",
                rel=rel,
            )
        # A reserved archive-control name is decided by the same verdict the
        # tracked leg uses, and BEFORE the symlink and non-regular branches:
        # a link or a special file on a control name is not generated content,
        # so it must keep the fail-closed refusal instead of being classified
        # as ordinary payload and colliding with the control writer.
        if _supersede_generated_control(rel, ORIGIN_UNTRACKED, tracked=False):
            continue
        if stat_module.S_ISLNK(st.st_mode):
            entries[rel] = _resolve_symlink_entry(source_resolved, full, rel, ORIGIN_UNTRACKED)
            continue
        if not stat_module.S_ISREG(st.st_mode):
            # A FIFO/socket/device/other special untracked object carries no
            # packageable content. Unlike a TRACKED non-regular (the index
            # promised content, so it fails closed above), Git never promised
            # this untracked special file was project truth: record it as an
            # explicit, named omission rather than failing the whole pack --
            # the same philosophy as the untracked-nested-worktree branch.
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_UNTRACKED,
                include=False,
                reason=REASON_UNTRACKED_NON_REGULAR,
            )
            continue
        include, reason, priority = _classify_untracked(
            rel,
            st.st_size,
            mandatory=mandatory,
            always_excl=always_excl,
            always_incl=always_incl,
            configured=configured,
        )
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_UNTRACKED,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=include,
            reason=reason,
            priority=priority,
        )

    # 4d. Canonical component ingestion (T-273): ONE dedicated phase, keyed by
    # bounded direct-child discovery and normalized origin identity ONLY --
    # never by whether the outer repository happens to enumerate the child.
    # The canonical product layout ignores its own component checkout in the
    # OUTER .gitignore, so coupling ingestion to `ls-files --others` dropped
    # first-party source from an otherwise successful pack. Every proven
    # same-origin component is ingested here exactly once, under its real
    # prefix; any failure to inventory it fails the pack closed by NAME
    # (build_nested_component_entries raises NESTED_GIT_COMPONENT_INVENTORY_FAILED).
    # Foreign components never reach this phase: they keep the named omission
    # recorded above and in the manifest.
    for component in same_origin_components.values():
        component_entries = build_nested_component_entries(
            component, set(excludes), cancel_event=cancel_event
        )
        for member_rel, member in component_entries.items():
            if member_rel in entries:
                raise SourceInventoryError(
                    CODE_INVENTORY_INCONSISTENT,
                    "path reported as both tracked and a nested component member",
                    rel=member_rel,
                )
            _claim_lower(member_rel)
            entries[member_rel] = member
        component.included = True
        component.file_count = sum(
            1 for m in component_entries.values() if m.include
        )
        component.included_bytes = sum(
            m.size for m in component_entries.values() if m.include
        )
        component.reason = "nested_git_component"

    # 5. Explicit-required overlay (T-188 P0, bounded to explicit references):
    # protected control-plane discovery + exact always_include paths +
    # manifest-declared required files that Git ignore hid from enumeration.
    # No generic ignored-tree walk is introduced for any of these: protected
    # locations are three fixed roots, always_include contributes only its
    # exact (glob-free) paths, and manifests contribute only paths they name.
    overlay_candidates: dict[str, set[str]] = {}  # rel -> tags

    def _add_candidate(raw: str, tag: str) -> None:
        try:
            rel = validate_rel_path(raw)
        except SourceInventoryError:
            return
        if rel in entries:
            return
        overlay_candidates.setdefault(rel, set()).add(tag)

    for raw_path in overlay_paths:
        _add_candidate(raw_path, "protected")

    # 5a. Explicit always_include paths Git never enumerated. Only exact,
    # glob-free patterns can be probed with one stat each; a glob pattern
    # would require an ignored-tree walk, which stays out of scope.
    for pattern in always_include or []:
        pat = (pattern or "").strip().replace("\\", "/")
        if not pat or any(ch in pat for ch in "*?["):
            continue
        parts = [seg for seg in pat.split("/") if seg]
        if not parts or any(seg in ("..", ".") for seg in parts):
            continue
        probe = source_resolved.joinpath(*parts)
        try:
            if probe.is_file():
                _add_candidate(pat, "always_include")
        except OSError:
            continue

    # 5b. Manifest-declared required files (conservative MANIFEST.json
    # contract) Git ignore hid from enumeration. Only manifests already in
    # the inventory are read, and only paths they explicitly name are probed.
    for m_rel, m_entry in sorted(entries.items()):
        if not (m_entry.include and m_rel.rpartition("/")[2].lower() == "manifest.json"):
            continue
        m_path = source_resolved.joinpath(*m_rel.split("/"))
        for req in _manifest_required_candidates(m_path, m_rel):
            probe = source_resolved.joinpath(*req.split("/"))
            try:
                if probe.is_file():
                    _add_candidate(req, "manifest_required")
            except OSError:
                continue

    # 5c. SAIPEN audit evidence. The protocol declares its own evidence in
    # `.saipen/MANIFEST.json`; Git ignore must not decide whether that
    # evidence exists. Bounded by that contract -- one stat for the manifest,
    # then only the directories it names, each capped -- so no generic
    # ignored-tree walk enters packing. A project with no manifest (or no
    # `.saipen`) costs one failed stat and nothing more.
    saipen_collection = saipen_evidence.collect_for_inventory(
        source_resolved, cancel_event=cancel_event
    )
    for rel in saipen_collection.paths:
        _add_candidate(rel, "saipen_evidence")

    for rel, tags in sorted(overlay_candidates.items()):
        # Only protected control-plane material or explicitly required paths
        # enter the inventory through the overlay; any other ignored file that
        # merely happens to sit inside a bounded location stays governed by
        # Git (ignored -> never enumerated), exactly as before the overlay.
        if not (
            "always_include" in tags
            or "manifest_required" in tags
            or "saipen_evidence" in tags
            or _is_protected_control_plane_rel(rel)
        ):
            continue
        _claim_lower(rel)
        full = source_resolved.joinpath(*rel.split("/"))
        st = _stat_existing(full, rel)
        if stat_module.S_ISDIR(st.st_mode):
            continue
        if stat_module.S_ISLNK(st.st_mode):
            entries[rel] = _resolve_symlink_entry(source_resolved, full, rel, ORIGIN_UNTRACKED)
            continue
        if not stat_module.S_ISREG(st.st_mode):
            continue
        include, reason, priority = _classify_untracked(
            rel,
            st.st_size,
            mandatory=mandatory,
            always_excl=always_excl,
            always_incl=always_incl,
            configured=configured,
            saipen_declared="saipen_evidence" in tags,
        )
        # Manifest-required promotion mirrors the filesystem-mode plan: a
        # required file is priority-1 audit material, never discretionary.
        if "manifest_required" in tags and include:
            priority = 1
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_UNTRACKED,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=include,
            reason=reason,
            priority=priority,
        )

    # SAIPEN contract reclassification (T-850/SRC-007). The overlay applies
    # the declared-evidence precedence only to files Git ignore hid, but the
    # SAIPEN contract governs the WHOLE memory root wherever a file came from:
    # an untracked, non-ignored `.saipen/logs/**` segment must not lose to the
    # generic `logs` exclude (the sealed segments make LOG's parent chain
    # resolvable), and a non-exportable surface (locks/, recovery/, quarantine/,
    # runtime epoch state) must never reach the archive because Git happened to
    # enumerate it -- tracked INCLUDED, since `git ls-files` enumerates a
    # committed lock file exactly like any other tracked file. One bounded pass
    # over the ALREADY-enumerated entries using the collection's own tier map
    # and ban list -- no new discovery, no new walk. A configured
    # `always_exclude` stays authoritative and is reported by the verdict as a
    # required-evidence omission, as everywhere else.
    if saipen_collection.contract.detected and saipen_collection.contract.status == saipen_evidence.STATUS_COMPLETE:
        _contract = saipen_collection.contract
        _tier_of = saipen_collection.tier_of
        # CORE-001: the memory root is ALLOWLISTED, not denylisted. A contract
        # BAN refuses only what it names; it says nothing about the far larger
        # set the contract never declared, and every one of those survived as
        # ordinary payload the moment Git enumerated it -- tracked or not.
        # Measured on this very project: 278 `.saipen/scratch_*` files,
        # 4,547,574 bytes, shipped as archive content. The allowed set is the
        # collector's own result, so mandatory, conditional, optional,
        # cited-required, the per-directory caps, the transient exclusions and
        # the contract version all keep exactly their existing semantics; this
        # refuses only the rest. A walk that stopped at the global ceiling is
        # an incomplete picture, not a declaration, so the allowlist stands
        # down there rather than calling undeclared evidence private.
        _root = _contract.memory_root + "/"
        _undeclared = not saipen_collection.budget_exhausted
        _declared = set(saipen_collection.paths) | set(saipen_collection.cited_required)
        for _rel, _entry in entries.items():
            if saipen_evidence.is_non_exportable(_rel, _contract):
                # A contract BAN is not a discretionary exclude, so it is not
                # subject to the origin precedence below: the protocol declared
                # this surface private, and a file being tracked does not make
                # it publishable. The ban therefore applies wherever the file
                # came from -- which is precisely the case the untracked-only
                # guard used to let through. An entry an explicit operator
                # `always_exclude` already removed is left untouched: it is
                # excluded either way, and its reason stays the operator's own.
                if _entry.include:
                    entries[_rel] = SourceInventoryEntry(
                        rel=_rel,
                        origin=_entry.origin,
                        size=_entry.size,
                        mtime_ns=_entry.mtime_ns,
                        st_ino=_entry.st_ino,
                        st_dev=_entry.st_dev,
                        include=False,
                        reason=saipen_evidence.REASON_SAIPEN_NON_EXPORTABLE,
                        priority=_entry.priority,
                    )
                continue
            if _undeclared and _rel.startswith(_root) and _rel not in _declared:
                # Same symmetry as the ban leg above: an undeclared file is not
                # a discretionary exclude, so tracking status never outranks the
                # contract, and an entry an explicit operator `always_exclude`
                # already removed keeps that more specific reason.
                if _entry.include:
                    entries[_rel] = SourceInventoryEntry(
                        rel=_rel,
                        origin=_entry.origin,
                        size=_entry.size,
                        mtime_ns=_entry.mtime_ns,
                        st_ino=_entry.st_ino,
                        st_dev=_entry.st_dev,
                        include=False,
                        reason=saipen_evidence.REASON_SAIPEN_UNDECLARED,
                        priority=_entry.priority,
                    )
                continue
            if _entry.origin != ORIGIN_UNTRACKED:
                continue
            if _rel in _tier_of and not _entry.include and _entry.reason == "configured_ignore":
                # Declared evidence lost to a GENERIC configured pattern:
                # the contract outranks it exactly as in the overlay leg.
                # An explicit operator always_exclude produced a different
                # reason (configured_ignore is shared, so re-check the
                # matcher before overriding).
                if not always_excl(_rel.lower()):
                    entries[_rel] = SourceInventoryEntry(
                        rel=_rel,
                        origin=_entry.origin,
                        size=_entry.size,
                        mtime_ns=_entry.mtime_ns,
                        st_ino=_entry.st_ino,
                        st_dev=_entry.st_dev,
                        include=True,
                        reason=None,
                        priority=1,
                    )

    # Tracked-but-deleted: Git's own state decides. A deletion Git records is
    # an explicit tracked_deleted entry; an absence Git does NOT record is an
    # unexplained inventory hole and fails the pack. Both untakeable policies
    # are checked even here -- secret material AND reserved archive-control
    # names -- so a tracked hard-deny path cannot dodge its own classification
    # by being deleted, and each keeps the diagnostic that names its real
    # cause.
    seen_tracked = set(entries)
    for raw_path in {p for _m, p in tracked_records}:
        rel = validate_rel_path(raw_path)
        if rel in seen_tracked:
            continue
        if rel in RESERVED_ARCHIVE_NAMES or hard(rel):
            raise _tracked_path_conflict(rel)
        if rel in git_deleted:
            tracked_deleted.append(rel)
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_TRACKED,
                size=0,
                include=False,
                reason=REASON_TRACKED_DELETED,
            )
            continue
        raise SourceInventoryError(
            CODE_INVENTORY_INCONSISTENT,
            "tracked file is absent from the worktree and git status does not "
            "record a deletion",
            rel=rel,
        )
    tracked_deleted.sort()
    superseded_control.sort()

    # Manifest truth (T-273): a proven same-origin component can never survive
    # final inventory as `included=False` with an empty reason. Exactly two
    # legal states -- represented (included, reason "nested_git_component") or
    # the pack already failed closed in the ingestion phase above. Anything
    # else is an invariant breach, reported under the component's own code so
    # the operator repairs that checkout rather than hunting a generic gap.
    for component in nested_components:
        if not component.included and not component.reason:
            raise SourceInventoryError(
                CODE_NESTED_COMPONENT_INVENTORY,
                f"nested component {component.rel!r} is neither inventoried "
                "nor named as an omission",
                rel=component.rel,
            )

    return SourceInventory(
        mode="git",
        source=source_resolved,
        entries=entries,
        tracked_deleted=tracked_deleted,
        superseded_control=superseded_control,
        git_head=git_head,
        git_dirty=git_dirty,
        nested_git_components=nested_components,
        saipen=saipen_verdict(source_resolved, entries, collection=saipen_collection),
    )


# ---------------------------------------------------------------------------
# Filesystem inventory (non-Git fallback, same frozen shape)
# ---------------------------------------------------------------------------


def _reserved_control_verdict(
    rel: str,
    source_resolved: Path,
    supersedable_reserved: Optional[frozenset],
    *,
    origin: str = ORIGIN_FILESYSTEM,
    tracked: bool = False,
) -> Optional[SourceInventoryEntry]:
    """The ONE classification for a reserved archive-control name.

    AUDAPACK owns these names: the packer writes its own control artifact
    under each of them. So a source file sitting on one is never ordinary
    payload -- it is either a stale copy of AUDAPACK's own earlier output, or
    project-owned content masquerading under a name the packer is about to
    write. Those two answers are opposites and must not blur:

    - bytes carrying AUDAPACK's own marker, on a name THIS pack regenerates,
      are superseded: the fresh artifact replaces them and the source file is
      recorded as a named omission with its real frozen evidence, so it stays
      auditable. The worktree is never touched.
    - anything else fails closed. Packaging project bytes under a name
      AUDAPACK writes would let content masquerade as generated archive
      metadata, and silently dropping it would be the omission T-190 exists
      to prevent.

    Origin is a parameter, not a mode. T-246 taught the TRACKED leg this
    discipline and the UNTRACKED git leg never received it, so a project that
    packed once and never committed the result got an archive carrying
    ``.audapack/manifest.json`` twice and a verification failure naming
    neither the cause nor the file. A control artifact is the same artifact
    whether Git happens to track it; the entry still records where it actually
    came from.

    Returns the entry that must replace the path, or ``None`` when ``rel`` is
    not a reserved control name at all.
    """
    if rel not in RESERVED_ARCHIVE_NAMES:
        return None
    path = source_resolved.joinpath(*rel.split("/"))
    if supersedable_reserved is not None and rel in supersedable_reserved:
        if is_generated_archive_control(path, rel):
            # Frozen source evidence, not a size-0 placeholder: a superseded
            # file the operator can no longer see must still be measurable
            # afterwards, so it counts as an exclusion carrying real bytes.
            try:
                st = path.stat()
                size, mtime_ns, st_ino, st_dev = st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev
            except OSError:  # pragma: no cover - raced away mid-probe
                size = mtime_ns = st_ino = st_dev = 0
            return SourceInventoryEntry(
                rel=rel,
                origin=origin,
                size=size,
                mtime_ns=mtime_ns,
                st_ino=st_ino,
                st_dev=st_dev,
                include=False,
                reason=REASON_SUPERSEDED_GENERATED_CONTROL,
            )
    raise _tracked_path_conflict(rel, tracked=tracked)


def inventory_from_plan(source: Path, plan, supersedable_reserved: Optional[frozenset] = None) -> SourceInventory:
    """Freeze a completed fidelity plan's decisions into inventory entries.

    The plan IS the one traversal; freezing adds no second walk. Included
    entries capture their stat identity here so the writer can prove each file
    unchanged between freeze and read.
    """
    source_resolved = source.resolve()
    entries: dict[str, SourceInventoryEntry] = {}
    for rel, decision in plan.decisions.items():
        reserved = _reserved_control_verdict(rel, source_resolved, supersedable_reserved)
        if reserved is not None:
            entries[rel] = reserved
            continue
        if not decision.include:
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_FILESYSTEM,
                size=decision.size,
                include=False,
                reason=decision.reason,
                priority=decision.priority,
            )
            continue
        full = source_resolved.joinpath(*rel.split("/"))
        st = _stat_existing(full, rel)
        if stat_module.S_ISDIR(st.st_mode):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "planned file is a directory in the worktree",
                rel=rel,
            )
        if stat_module.S_ISLNK(st.st_mode):
            # A link planned as include would not have survived the walk; this
            # is a post-plan change and fails closed.
            raise SourceInventoryError(
                CODE_SYMLINK_UNSAFE,
                "planned file became a symlink after planning",
                rel=rel,
            )
        if not stat_module.S_ISREG(st.st_mode):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "planned path is not a regular file",
                rel=rel,
            )
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_FILESYSTEM,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=True,
            priority=decision.priority,
        )
    return SourceInventory(
        mode="filesystem",
        source=source_resolved,
        entries=entries,
        saipen=saipen_verdict(source_resolved, entries),
    )


def inventory_from_walk(
    source: Path,
    excludes: set[str],
    cancel_event=None,
    supersedable_reserved: Optional[frozenset] = None,
) -> SourceInventory:
    """Bounded walk fallback for legacy packs (no fidelity config supplied).

    Same matcher contract the legacy zipper applied, one traversal, results
    frozen into inventory entries before any archive byte is written.
    """
    from audapack.packing import _build_exclusion_matcher

    source_resolved = source.resolve()
    matcher = _build_exclusion_matcher(set(excludes) | set(MANDATORY_EXCLUDES))
    entries: dict[str, SourceInventoryEntry] = {}
    walk_errors = 0

    def _on_walk_error(err):
        nonlocal walk_errors
        # A directory deleted after its parent listed it holds nothing to miss;
        # the source root itself disappearing is still a failure.
        if isinstance(err, FileNotFoundError) and Path(err.filename or "") != source_resolved:
            return
        walk_errors += 1

    for root, dirs, files in os.walk(source_resolved, onerror=_on_walk_error):
        if cancel_event is not None and cancel_event.is_set():
            raise SourceInventoryError(CODE_INVENTORY_INCONSISTENT, "inventory cancelled")
        base = Path(root)
        kept: list[str] = []
        for name in dirs:
            dpath = base / name
            rel_dir = dpath.relative_to(source_resolved).as_posix().lower()
            if matcher(rel_dir) or dpath.is_symlink():
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            path = base / name
            rel = path.relative_to(source_resolved).as_posix()
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                walk_errors += 1
                continue
            if stat_module.S_ISLNK(st.st_mode):
                entries[rel] = SourceInventoryEntry(
                    rel=rel,
                    origin=ORIGIN_FILESYSTEM,
                    size=0,
                    include=False,
                    reason="unsupported_special_file",
                )
                continue
            if matcher(rel):
                entries[rel] = SourceInventoryEntry(
                    rel=rel,
                    origin=ORIGIN_FILESYSTEM,
                    size=st.st_size,
                    include=False,
                    reason=exclusion_reason_for(rel.lower(), name.lower()),
                    priority=3,
                )
                continue
            reserved = _reserved_control_verdict(rel, source_resolved, supersedable_reserved)
            if reserved is not None:
                entries[rel] = reserved
                continue
            entries[rel] = SourceInventoryEntry(
                rel=rel,
                origin=ORIGIN_FILESYSTEM,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                st_ino=st.st_ino,
                st_dev=st.st_dev,
                include=True,
                priority=1,
            )
    if walk_errors:
        raise SourceInventoryError(
            CODE_INVENTORY_INCONSISTENT,
            f"source traversal incomplete: {walk_errors} unreadable entr(y|ies)",
        )
    return SourceInventory(
        mode="filesystem",
        source=source_resolved,
        entries=entries,
        saipen=saipen_verdict(source_resolved, entries),
    )


def inventory_from_single_file(source: Path, excludes: set[str]) -> SourceInventory:
    """Legacy single-file pack contract, frozen as a one-entry inventory."""
    from audapack.packing import _build_exclusion_matcher, _path_is_excluded_normalized

    source_resolved = source.resolve()
    if source_resolved.is_symlink():
        raise SourceInventoryError(
            CODE_SYMLINK_UNSAFE,
            f"Refusing to package symlink source: {source_resolved}",
            rel=source_resolved.name,
        )
    if _path_is_excluded_normalized(source_resolved, _build_exclusion_matcher(set(excludes) | set(MANDATORY_EXCLUDES))):
        raise SourceInventoryError(
            CODE_PATH_INVALID,
            f"Refusing to package an excluded file: {source_resolved.name} matches an exclusion rule",
            rel=source_resolved.name,
        )
    try:
        st = source_resolved.stat()
    except OSError as exc:
        raise SourceInventoryError(
            CODE_INVENTORY_INCONSISTENT,
            f"source file is unreadable: {exc}",
            rel=source_resolved.name,
        ) from exc
    entry = SourceInventoryEntry(
        rel=source_resolved.name,
        origin=ORIGIN_FILESYSTEM,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        st_ino=st.st_ino,
        st_dev=st.st_dev,
        include=True,
        priority=1,
    )
    return SourceInventory(
        mode="filesystem",
        # A single-file source's write root is its PARENT directory: the
        # writer resolves every member as ``root / rel``, so a file source
        # must never become its own root (that produced ``notes.md/notes.md``).
        source=source_resolved.parent,
        entries={entry.rel: entry},
    )


# ---------------------------------------------------------------------------
# Top-level entry used by the packer
# ---------------------------------------------------------------------------


def build_pack_inventory(
    source: Path,
    excludes: set[str],
    *,
    packing=None,
    prebuilt_plan=None,
    supersedable_reserved: Optional[frozenset] = None,
    cancel_event=None,
) -> tuple[SourceInventory, object]:
    """Discover, validate and freeze the pack's source inventory.

    Returns ``(inventory, plan)``. ``plan`` is the fidelity plan backing a
    filesystem-mode pack (None for Git mode, where no tree walk happened and
    none may). Raises ``SourceInventoryError`` when the inventory cannot be
    established reliably -- the caller must fail the pack, never fall back.
    """
    source = Path(source)
    if source.is_file():
        return inventory_from_single_file(source, excludes), None

    if detect_git_worktree(source) is not None:
        inventory = build_git_inventory(
            source,
            excludes,
            always_include=list(getattr(packing, "always_include", None) or []) if packing is not None else [],
            always_exclude=list(getattr(packing, "always_exclude", None) or []) if packing is not None else [],
            supersedable_reserved=supersedable_reserved,
            cancel_event=cancel_event,
        )
        return inventory, None

    if prebuilt_plan is not None:
        if getattr(prebuilt_plan, "stopped_on_stale", False) or getattr(prebuilt_plan, "cancelled", False):
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "prebuilt plan is a freshness verdict over a tree prefix, not a pack input",
            )
        if getattr(prebuilt_plan, "walk_incomplete", False) or getattr(prebuilt_plan, "failed", 0) > 0:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "source traversal incomplete; the frozen inventory cannot be trusted",
                rel=getattr(prebuilt_plan, "first_failure_rel", None) or "",
            )
        return inventory_from_plan(source, prebuilt_plan, supersedable_reserved), prebuilt_plan

    if packing is not None:
        from audapack.fidelity import build_plan_from_config

        plan = build_plan_from_config(source, packing, excludes, cancel_event=cancel_event)
        if plan.cancelled:
            raise SourceInventoryError(CODE_INVENTORY_INCONSISTENT, "inventory cancelled")
        if plan.walk_incomplete or plan.failed > 0:
            raise SourceInventoryError(
                CODE_INVENTORY_INCONSISTENT,
                "source traversal incomplete; the frozen inventory cannot be trusted",
                rel=plan.first_failure_rel or "",
            )
        return inventory_from_plan(source, plan, supersedable_reserved), plan

    return inventory_from_walk(
        source, excludes, cancel_event=cancel_event,
        supersedable_reserved=supersedable_reserved,
    ), None
