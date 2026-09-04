"""Audit-fidelity profiles for AUDAPACK archives.

An archive is either an honest *audit representation* (COMPACT/STANDARD/DEEP,
files intentionally omitted under a declared policy) or a *full snapshot*
(FULL, only explicit exclusion/safety policy applies). Every discovered file
is accounted for exactly once, so ``discovered == included + excluded +
failed`` always holds and nothing silently disappears.

The engine never implements ``if size > budget: drop arbitrary files``.
Files carry a semantic priority; budget enforcement only ever trims the
lowest-priority *ordinary assets*, never mandatory audit material (source,
tests, configs, manifests, .saipen, docs, schemas, git evidence) and never
dependency-referenced assets.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

PROFILE_COMPACT = "compact"
PROFILE_STANDARD = "standard"
PROFILE_DEEP = "deep"
PROFILE_FULL = "full"

PROFILES = (PROFILE_COMPACT, PROFILE_STANDARD, PROFILE_DEEP, PROFILE_FULL)
DEFAULT_PROFILE = PROFILE_STANDARD

#: Soft budget bytes per profile (0 = unlimited). These are TARGETS, never
#: hard caps on mandatory material.
PROFILE_BUDGET_BYTES = {
    PROFILE_COMPACT: 10 * 1024 * 1024,   # ~5-10 MB
    PROFILE_STANDARD: 30 * 1024 * 1024,  # ~20-30 MB
    PROFILE_DEEP: 100 * 1024 * 1024,     # ~75-100 MB
    PROFILE_FULL: 0,                     # no artificial budget
}

#: Representative media files physically kept per media-heavy directory.
PROFILE_MEDIA_SAMPLES = {
    PROFILE_COMPACT: 1,
    PROFILE_STANDARD: 3,
    PROFILE_DEEP: 0,     # 0 = keep everything
    PROFILE_FULL: 0,
}

#: Per media directory byte cap for the physically included samples.
PROFILE_MEDIA_BYTES = {
    PROFILE_COMPACT: 1 * 1024 * 1024,
    PROFILE_STANDARD: 2 * 1024 * 1024,
    PROFILE_DEEP: 0,
    PROFILE_FULL: 0,
}

#: Files below this size are "small assets" kept at STANDARD/DEEP even when
#: their media directory is otherwise sampled.
MEDIA_SMALL_BYTES = 256 * 1024


def normalize_fidelity_profile(value: object) -> str:
    """Coerce a persisted/imported profile value to a known member.

    Unknown / missing values fall back to STANDARD so an older config.json
    keeps working and remains truthful about what it produces.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in PROFILES:
            return v
    return DEFAULT_PROFILE


def archive_semantics_for(profile: str) -> str:
    """FULL is a complete snapshot; everything else is an audit representation."""
    return "full_snapshot" if normalize_fidelity_profile(profile) == PROFILE_FULL else "audit_representation"


def profile_budget_bytes(profile: str, override_mb: int = 0) -> int:
    """Soft budget for the profile, or ``override_mb`` megabytes when set."""
    if override_mb and override_mb > 0:
        return override_mb * 1024 * 1024
    return int(PROFILE_BUDGET_BYTES.get(normalize_fidelity_profile(profile), 0))


def profile_media_samples(profile: str, override: int = 0) -> int:
    if override and override > 0:
        return override
    return int(PROFILE_MEDIA_SAMPLES.get(normalize_fidelity_profile(profile), 0))


def profile_media_bytes(profile: str, override: int = 0) -> int:
    if override and override > 0:
        return override
    return int(PROFILE_MEDIA_BYTES.get(normalize_fidelity_profile(profile), 0))


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

#: Reason categories a file can carry when excluded (spec).
REASON_MEDIA_BUDGET = "profile_media_budget"
REASON_CONFIGURED_IGNORE = "configured_ignore"
REASON_DEPENDENCY_CACHE = "dependency_cache"
REASON_GENERATED_OUTPUT = "generated_output"
REASON_SIZE_LIMIT = "size_limit"
REASON_SECRET_POLICY = "secret_policy"
REASON_UNSUPPORTED = "unsupported_special_file"

REASON_CATEGORIES = (
    REASON_MEDIA_BUDGET,
    REASON_CONFIGURED_IGNORE,
    REASON_DEPENDENCY_CACHE,
    REASON_GENERATED_OUTPUT,
    REASON_SIZE_LIMIT,
    REASON_SECRET_POLICY,
    REASON_UNSUPPORTED,
)

#: ext -> media class. Only extensions that map to a class are "media".
MEDIA_EXTENSIONS = {
    # audio
    "wav": "audio", "mp3": "audio", "ogg": "audio", "flac": "audio",
    "aac": "audio", "m4a": "audio", "opus": "audio",
    # video
    "mp4": "video", "avi": "video", "mov": "video", "mkv": "video",
    "webm": "video", "m4v": "video", "wmv": "video", "flv": "video",
    # image
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "webp": "image", "bmp": "image", "ico": "image", "tif": "image",
    "tiff": "image",
    # font
    "woff": "font", "woff2": "font", "ttf": "font", "otf": "font",
    "eot": "font",
    # binary datasets
    "npy": "dataset", "npz": "dataset", "h5": "dataset", "hdf5": "dataset",
    "mat": "dataset",
}


def media_class_for(path: Path | str) -> Optional[str]:
    """Media class for a path's extension, or None for non-media files."""
    suffix = Path(path).suffix.lower().lstrip(".")
    return MEDIA_EXTENSIONS.get(suffix)


#: Priority-1 (mandatory audit material) extensions: code, tests, configs,
#: manifests, build definitions, schemas. Never trimmed by any budget.
P1_EXTENSIONS = {
    # source
    "py", "pyw", "js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte",
    "rs", "go", "c", "h", "cc", "cpp", "hpp", "cs", "java", "kt", "kts",
    "swift", "rb", "php", "lua", "jl", "r", "sh", "ps1", "bat", "cmd",
    "vbs", "psm1", "psd1", "pl", "pm", "scala", "clj", "ex", "exs",
    "sql", "proto", "graphql", "gql", "dart", "hs", "fs", "fsx", "zig",
    "nim", "ml", "mli", "asm", "s", "S",
    # tests
    # (test files share source extensions; test dirs are classified by name)
    # configs / manifests / build
    "json", "json5", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
    "properties", "xml", "xsd", "lock", "gradle", "bazel", "bzl",
    "cmake", "mk", "make", "ninja", "dockerfile", "service", "desktop",
    "nuspec", "csproj", "sln", "vbproj", "fsproj", "pro", "pri",
    "godot", "tscn", "tres", "cu", "cuh", "glsl", "hlsl", "vert", "frag",
    "ipynb",
}

#: Priority-2 (prose, docs, presentation) extensions. Worth keeping, but a
#: budget trims them AFTER every ordinary asset is gone and never before.
P2_EXTENSIONS = {
    "md", "mdx", "rst", "txt", "adoc", "html", "htm", "css", "scss",
    "sass", "less", "pdf", "svg", "tex",
}

#: Names that are mandatory audit material wherever they sit, even though
#: their extension is prose.
P1_NAME_PREFIXES = (
    "readme", "license", "changelog", "contributing", "code_of_conduct",
    "security", "notice",
)
P1_NAMES = frozenset({
    "version", "gitignore", "editorconfig", "dockerfile", "makefile",
    "gemfile", "rakefile", "procfile", "requirements.txt", "cargo.toml",
    "package.json", "pyproject.toml", "cmakelists.txt", "gradlew",
    "gradlew.bat",
})

#: `.saipen/` is agent protocol memory: its decision documents ARE audit
#: material, but the content-addressed blob stores, journals and settled
#: recovery records below it are bulk. Measured on FastPrompter: 45.70 MB of
#: 62.64 MB "priority-1" material was `.saipen/`, which is what made COMPACT
#: (10 MB target) indistinguishable from FULL.
SAIPEN_P1_DOCS = frozenset({
    "state.md", "board.md", "log.md", "project.md", "roadmap.md",
    "identity.md", "core.md", "ui.md", "style.md", "boot.md", "index.md",
})
SAIPEN_DOC_DIRS = frozenset({"tickets", "work"})


def _priority_for(rel_lower: str, name_lower: str) -> int:
    """Semantic priority: 1 = mandatory audit material, 2 = prose, 3 = asset.

    Budget enforcement only ever trims 3 then 2. Nothing trims 1.
    """
    segs = rel_lower.split("/")
    if segs[0] == ".saipen":
        # Decision documents at the memory root plus the ticket/work trees.
        if len(segs) == 2 and name_lower in SAIPEN_P1_DOCS:
            return 1
        if len(segs) > 2 and segs[1] in SAIPEN_DOC_DIRS:
            return 2
        return 3
    if name_lower.startswith(P1_NAME_PREFIXES) or name_lower in P1_NAMES:
        return 1
    ext = name_lower.rsplit(".", 1)[1] if "." in name_lower else ""
    if ext in P1_EXTENSIONS:
        return 1
    # Priority directories: git evidence (non-objects), schemas, tests, tools.
    for seg in segs:
        if seg in (".github", "schemas", "schema", "specs", "spec"):
            return 1
        if seg == "tests" or seg.startswith("test_") or seg.startswith("tests_"):
            return 1
        if seg == "scripts" or seg == "tools":
            return 1
    if ext in P2_EXTENSIONS:
        return 2
    for seg in segs:
        if seg == "docs" or seg.startswith("docs_"):
            return 2
    return 3


#: Patterns whose exclusion reason is dependency_cache (things the build pulls
#: in, never project-owned).
DEPENDENCY_PATTERNS = {
    "node_modules", ".venv", ".build-venv", ".cargo", ".git/objects",
    ".conda", ".mypy_cache", ".tox",
}
DEPENDENCY_EXTS = {"whl", "tar.gz"}

#: Patterns whose exclusion reason is generated_output (build/cache/junk).
GENERATED_PATTERNS = {
    "dist", "build", "target", ".next", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "logs", ".coverage", "htmlcov",
    "*.egg-info", ".gitignore.bak", ".DS_Store",
}
GENERATED_EXTS = {
    "pyc", "pyo", "pycx", "log", "sqlite", "sqlite3", "db", "db-shm",
    "db-wal", "zip", "part", "tmp", "bak", "old", "exe", "dll", "so",
    "dylib", "pyd", "msi", "obj", "lib", "class", "jar", "wasm", "pyz",
    "tar", "tgz", "gz", "bz2", "xz", "7z", "rar", "iso", "zst", "whl",
    "node", "lock.html", "cache",
}


def exclusion_reason_for(rel_lower: str, name_lower: str) -> str:
    """Classify a matched configured/mandatory exclusion into a reason category."""
    if name_lower.endswith((".secret", ".secrets", ".token")) or name_lower in (
        "token.txt", "secrets"
    ) or name_lower.endswith(".pid"):
        return REASON_SECRET_POLICY
    for pat in DEPENDENCY_PATTERNS:
        if pat in rel_lower:
            return REASON_DEPENDENCY_CACHE
    if any(name_lower.endswith(ext) for ext in DEPENDENCY_EXTS):
        return REASON_DEPENDENCY_CACHE
    for pat in GENERATED_PATTERNS:
        if fnmatch.fnmatch(rel_lower, pat) or fnmatch.fnmatch(name_lower, pat):
            return REASON_GENERATED_OUTPUT
    if any(name_lower.endswith(ext) for ext in GENERATED_EXTS):
        return REASON_GENERATED_OUTPUT
    return REASON_CONFIGURED_IGNORE


#: Text-ish extensions worth scanning for asset references.
_REF_SCAN_EXTS = {
    "py", "pyw", "js", "jsx", "ts", "tsx", "mjs", "cjs", "json", "yaml",
    "yml", "toml", "ini", "cfg", "md", "rst", "txt", "html", "htm", "css",
    "scss", "sass", "sh", "ps1", "bat", "cmd", "vbs", "xml", "properties",
    "lock", "env", "conf", "gradle", "cmake", "mk", "sql", "vue", "svelte",
}
_REF_SCAN_MAX_FILE = 256 * 1024
_REF_SCAN_MAX_TOTAL = 4 * 1024 * 1024

#: A reference to a concrete file with a media extension, or to a media-ish
#: directory, as it appears inside build/runtime config text.
_REF_FILE_RE = re.compile(
    r"""["'\\(\s]([A-Za-z0-9_\-.\\/ ]+\.(?:wav|mp3|ogg|flac|aac|m4a|mp4|avi|mov|mkv|webm|png|jpe?g|gif|webp|bmp|ico|tiff?|woff2?|ttf|otf|eot|npy|npz|h5|hdf5|mat))["'\)\s,]""",
    re.IGNORECASE,
)
_REF_DIR_RE = re.compile(
    r"""["'\\(\s]([A-Za-z0-9_\-.\\/ ]*(?:Sounds?|Audio|Videos?|Media|Images?|Assets|Fonts?|Resources?)[/\\][A-Za-z0-9_\-.\\/ ]*)["'\)\s,]""",
    re.IGNORECASE,
)


def scan_asset_references(
    source: Path, prune: Optional[Callable[[Path], bool]] = None
) -> tuple[set[str], set[str]]:
    """Find assets referenced by build/runtime text files.

    Returns ``(referenced_files, referenced_dirs)`` with POSIX-relative paths
    inside ``source``. Bounded: skips non-text and files over 256 KiB, stops
    after ~4 MiB of scanned text. Exact file references outrank directory
    references during media sampling.

    T-147: when ``prune`` is supplied (the plan's configured/mandatory
    matcher) excluded directories and files are never descended into or
    stat'ed, so the reference scan costs the same as the pack itself.
    """
    referenced_files: set[str] = set()
    referenced_dirs: set[str] = set()
    scanned = 0
    try:
        for root, dirs, files in os.walk(source):
            kept = []
            for d in dirs:
                dp = Path(root) / d
                if prune is not None and prune(dp):
                    continue
                if dp.is_symlink():
                    continue
                kept.append(d)
            dirs[:] = kept
            for name in files:
                path = Path(root) / name
                if path.is_symlink():
                    continue
                if prune is not None and prune(path):
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in _REF_SCAN_EXTS:
                    continue
                try:
                    if path.stat().st_size > _REF_SCAN_MAX_FILE:
                        continue
                except OSError:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                scanned += len(text)
                for m in _REF_FILE_RE.finditer(text):
                    raw = m.group(1).replace("\\", "/").strip().lstrip("./").strip("'\" ")
                    if not raw:
                        continue
                    candidate = (source / raw).resolve()
                    try:
                        if candidate.is_relative_to(source.resolve()) and candidate.is_file():
                            referenced_files.add(raw)
                            continue
                    except (OSError, ValueError):
                        pass
                    # Directory reference: remember every ancestor prefix.
                    segs = raw.split("/")
                    for i in range(1, len(segs)):
                        referenced_dirs.add("/".join(segs[:i]))
                for m in _REF_DIR_RE.finditer(text):
                    raw = m.group(1).replace("\\", "/").strip().lstrip("./").strip("'\" ")
                    if not raw:
                        continue
                    segs = raw.split("/")
                    for i in range(1, len(segs) + 1):
                        referenced_dirs.add("/".join(segs[:i]))
                if scanned > _REF_SCAN_MAX_TOTAL:
                    break
            if scanned > _REF_SCAN_MAX_TOTAL:
                break
    except OSError:
        pass
    return referenced_files, referenced_dirs


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class FileDecision:
    rel: str                      # POSIX relative path inside the source
    size: int
    include: bool
    reason: Optional[str] = None  # exclusion reason when include is False
    priority: int = 3
    media_class: Optional[str] = None


@dataclass
class FidelityPlan:
    profile: str
    archive_semantics: str
    budget_bytes: int
    media_samples: int
    media_bytes_per_dir: int
    decisions: dict[str, FileDecision] = field(default_factory=dict)
    #: Relative paths excluded purely by profile decisions (media budget,
    #: size limit, always_exclude) so freshness walks can skip them too.
    extra_excluded_rel: set[str] = field(default_factory=set)
    #: Relative directories pruned whole (rel lower -> reason category) so
    #: the zipper never descends into them and the manifest names them.
    pruned_dirs_rel: dict[str, str] = field(default_factory=dict)
    discovered: int = 0
    included: int = 0
    excluded: int = 0
    failed: int = 0
    source_bytes: int = 0
    included_bytes: int = 0
    excluded_bytes: int = 0
    reason_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    largest_omitted: list[tuple[str, int]] = field(default_factory=list)
    media_inventory: dict[str, dict[str, object]] = field(default_factory=dict)
    referenced_files: set[str] = field(default_factory=set)
    #: Freshness fusion (T-147): True when any INCLUDED file is newer
    #: than ``newer_than_mtime``. Excluded/sampled/trimmed files never
    #: count, so a sampled-out media file can never force endless repacks.
    newer_found: bool = False
    #: True when the walk could not enumerate the whole tree (an untraversable
    #: directory, or an OSError that ended the walk early). The counters below
    #: then describe only what was reached, so an archive built from this plan
    #: is not a complete audit representation and the manifest must say so --
    #: without this flag the identity discovered == included + excluded + failed
    #: still holds over the truncated numbers and reads as full accounting.
    walk_incomplete: bool = False

    def decision_for(self, rel_posix_lower: str) -> Optional[FileDecision]:
        return self.decisions.get(rel_posix_lower)


def _build_matcher(patterns: set[str]):
    lowered = frozenset(pat.lower() for pat in patterns)
    exact = {p for p in lowered if not any(ch in p for ch in "*?[") and "/" not in p}
    globs = tuple(
        re.compile(fnmatch.translate(p))
        for p in lowered
        if p not in exact and "/" not in p
    )
    multi = tuple(
        tuple(seg for seg in p.split("/") if seg)
        for p in lowered
        if "/" in p
    )
    multi_res = tuple(tuple(re.compile(fnmatch.translate(s)) for s in segs) for segs in multi)

    def matches(path: Path | str) -> bool:
        p = path if isinstance(path, Path) else Path(path)
        parts = tuple(part.lower() for part in p.parts)
        for part in (p.name.lower(), *parts):
            if part in exact or any(g.fullmatch(part) for g in globs):
                return True
        for segs in multi_res:
            span = len(segs)
            if span > len(parts):
                continue
            for start in range(len(parts) - span + 1):
                if all(segs[i].fullmatch(parts[start + i]) for i in range(span)):
                    return True
        return False

    return matches


def build_fidelity_plan(
    source: Path,
    excludes: set[str],
    *,
    profile: str = DEFAULT_PROFILE,
    max_mb: int = 0,
    media_samples: int = 0,
    media_bytes: int = 0,
    always_include: Optional[list[str]] = None,
    always_exclude: Optional[list[str]] = None,
    mandatory_excludes: Optional[set[str]] = None,
    newer_than_mtime: Optional[float] = None,
) -> FidelityPlan:
    """Classify every file under ``source`` and decide what the archive holds.

    One traversal, one truth: the returned plan feeds both the zipper and the
    freshness walk, so the archive and the freshness check can never drift.
    """
    from audapack.packing import MANDATORY_EXCLUDES

    profile = normalize_fidelity_profile(profile)
    plan = FidelityPlan(
        profile=profile,
        archive_semantics=archive_semantics_for(profile),
        budget_bytes=profile_budget_bytes(profile, max_mb),
        media_samples=profile_media_samples(profile, media_samples),
        media_bytes_per_dir=profile_media_bytes(profile, media_bytes),
    )
    always_incl = _build_matcher({p for p in (always_include or [])})
    always_excl = _build_matcher({p for p in (always_exclude or [])})
    configured = _build_matcher({p for p in excludes})
    mandatory = _build_matcher(set(mandatory_excludes or MANDATORY_EXCLUDES))

    referenced_files, referenced_dirs = scan_asset_references(
        source, prune=lambda p: always_excl(p) or configured(p) or mandatory(p)
    )
    plan.referenced_files = referenced_files

    # Media groups: (media_class, parent_rel) -> list of (rel, size, path)
    media_groups: dict[tuple[str, str], list[tuple[str, int, Path]]] = {}
    # Non-media decisions first (media sampling needs the full group first).
    raw: list[tuple[str, int, FileDecision]] = []
    mtimes: dict[str, float] = {}  # rel -> st_mtime, fused freshness data

    def reason_stat(reason: str, size: int) -> None:
        stats = plan.reason_stats.setdefault(reason, {"count": 0, "bytes": 0})
        stats["count"] += 1
        stats["bytes"] += size

    def _plan_walk_error(err):
        # A directory that cannot be traversed means files below it were never
        # discovered: freshness cannot be proven, so the plan counts the walk
        # failure and the caller must repack rather than reuse an archive.
        # It counts as discovered as well: every increment of failed/excluded
        # has a matching discovered, or the manifest identity
        # discovered == included + excluded + failed silently goes false and an
        # incomplete archive reads as fully accounted for.
        plan.discovered += 1
        plan.failed += 1
        plan.walk_incomplete = True

    try:
        for root, dirs, files in os.walk(source, onerror=_plan_walk_error):
            base = Path(root)
            kept_dirs = []
            for d in dirs:
                dp = base / d
                rel_dir = dp.relative_to(source).as_posix().lower()
                # PERF-004 (audit/2.md): matcher BEFORE is_symlink(). The symlink
                # probe is a stat, and an excluded directory must never be stat'ed
                # (packing.py's eligible_source_files short-circuits the same way:
                # matcher first, symlink second).
                if mandatory(rel_dir):
                    plan.pruned_dirs_rel[rel_dir] = REASON_SECRET_POLICY
                    continue
                if always_excl(rel_dir):
                    plan.extra_excluded_rel.add(rel_dir)
                    plan.pruned_dirs_rel[rel_dir] = REASON_CONFIGURED_IGNORE
                    continue
                if configured(rel_dir):
                    plan.pruned_dirs_rel[rel_dir] = exclusion_reason_for(rel_dir, d.lower())
                    continue
                try:
                    if dp.is_symlink():
                        continue
                except OSError:
                    # An unreadable directory entry is one failed entry, not a
                    # reason to abandon the rest of the tree.
                    plan.discovered += 1
                    plan.failed += 1
                    plan.walk_incomplete = True
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs
            for name in files:
                fp = base / name
                # The symlink probe is itself a stat and raises on an unreadable
                # entry, so it shares the guard: outside it, one EACCES file
                # aborted the whole walk and the plan reported an empty tree as a
                # complete audit representation.
                try:
                    if fp.is_symlink():
                        plan.discovered += 1
                        plan.excluded += 1
                        reason_stat(REASON_UNSUPPORTED, 0)
                        continue
                    st = fp.stat()
                    size = st.st_size
                except OSError:
                    plan.discovered += 1
                    plan.failed += 1
                    continue
                plan.discovered += 1
                plan.source_bytes += size
                rel = fp.relative_to(source).as_posix()
                if newer_than_mtime is not None:
                    mtimes[rel] = st.st_mtime
                rel_lower = rel.lower()
                name_lower = name.lower()

                # 1. safety policy wins over everything, including overrides.
                if mandatory(rel_lower):
                    plan.excluded += 1
                    plan.excluded_bytes += size
                    reason_stat(REASON_SECRET_POLICY, size)
                    raw.append((rel, size, FileDecision(rel, size, False, REASON_SECRET_POLICY)))
                    continue
                # 2. user always_exclude wins over the profile.
                if always_excl(rel_lower):
                    plan.excluded += 1
                    plan.excluded_bytes += size
                    plan.extra_excluded_rel.add(rel)
                    reason_stat(REASON_CONFIGURED_IGNORE, size)
                    raw.append((rel, size, FileDecision(rel, size, False, REASON_CONFIGURED_IGNORE)))
                    continue
                # 3. configured excludes (deps / generated / plain).
                if configured(rel_lower):
                    reason = exclusion_reason_for(rel_lower, name_lower)
                    plan.excluded += 1
                    plan.excluded_bytes += size
                    reason_stat(reason, size)
                    raw.append((rel, size, FileDecision(rel, size, False, reason)))
                    continue

                mclass = media_class_for(fp)
                if mclass is not None:
                    media_groups.setdefault((mclass, str(base.relative_to(source).as_posix())), []).append(
                        (rel, size, fp)
                    )
                    continue

                priority = _priority_for(rel_lower, name_lower)
                # 4. user always_include wins over the profile: an explicitly
                # named file is mandatory material, so no budget trims it.
                if always_incl(rel_lower):
                    priority = 1
                plan.included += 1
                plan.included_bytes += size
                raw.append((rel, size, FileDecision(rel, size, True, None, priority)))
    except OSError:
        # Last-resort guard: the per-entry handlers above own the expected
        # failures, so reaching here means the walk itself died and the tree is
        # only partly enumerated. Marked, never swallowed silently.
        plan.walk_incomplete = True

    # ---- media sampling ---------------------------------------------------
    for (mclass, parent_rel), members in media_groups.items():
        inventory: dict[str, object] = {
            "class": mclass,
            "directory": parent_rel,
            "included": 0,
            "total": len(members),
            "bytes": 0,
            "files": [],
        }
        plan.media_inventory[parent_rel or "."] = inventory
        if plan.media_samples <= 0 or profile == PROFILE_FULL:
            # DEEP/FULL: keep every media file.
            for rel, size, _fp in members:
                plan.included += 1
                plan.included_bytes += size
                raw.append((rel, size, FileDecision(rel, size, True, None, 2)))
                inventory["included"] = int(inventory["included"]) + 1
                inventory["bytes"] = int(inventory["bytes"]) + size
                inventory["files"].append({"rel": rel, "size": size, "included": True})
            continue

        cap_bytes = plan.media_bytes_per_dir
        budget_left = cap_bytes if cap_bytes > 0 else None

        def _ref_rank(member) -> int:
            rel, _size, _fp = member
            if rel in referenced_files:
                return 0
            for seg_i in range(1, len(rel.split("/"))):
                if "/".join(rel.split("/")[:seg_i]) in referenced_dirs:
                    return 1
            return 2

        def _small_ok(member) -> bool:
            return member[1] <= MEDIA_SMALL_BYTES and profile != PROFILE_COMPACT

        # Small assets always count at STANDARD/DEEP; then referenced, then
        # largest-first until the count/byte cap. User always_include wins
        # over every profile decision (but never over safety policy above).
        ordered = sorted(members, key=lambda m: (_ref_rank(m), 0 if m[1] > MEDIA_SMALL_BYTES else 1, -m[1]))
        kept_count = 0
        for rel, size, fp in ordered:
            referenced = _ref_rank((rel, size, fp)) == 0
            if always_incl(rel.lower()):
                keep = True
            elif _small_ok((rel, size, fp)):
                keep = True
            elif referenced:
                keep = True  # build/runtime-referenced media outranks the cap
            elif kept_count < plan.media_samples:
                keep = True
            elif budget_left is not None and budget_left >= size:
                keep = True
            else:
                keep = False
            if keep:
                plan.included += 1
                plan.included_bytes += size
                inventory["included"] = int(inventory["included"]) + 1
                inventory["bytes"] = int(inventory["bytes"]) + size
                if budget_left is not None:
                    budget_left = max(0, budget_left - size)
                if not referenced and not _small_ok((rel, size, fp)) and not always_incl(rel.lower()):
                    kept_count += 1
                raw.append((rel, size, FileDecision(rel, size, True, None, 2)))
                inventory["files"].append({"rel": rel, "size": size, "included": True})
            else:
                plan.excluded += 1
                plan.excluded_bytes += size
                plan.extra_excluded_rel.add(rel)
                reason_stat(REASON_MEDIA_BUDGET, size)
                raw.append((rel, size, FileDecision(rel, size, False, REASON_MEDIA_BUDGET)))
                inventory["files"].append({"rel": rel, "size": size, "included": False})
    # ---- soft budget trim: ordinary assets first, prose second, never P1 ---
    # Two ordered passes, largest-first inside each. Priority 1 (code, tests,
    # configs, manifests, schemas, explicit always_include) is never offered to
    # the trim at all, so a budget can be missed but source is never sacrificed.
    if plan.budget_bytes > 0 and plan.included_bytes > plan.budget_bytes:
        for tier in (3, 2):
            if plan.included_bytes <= plan.budget_bytes:
                break
            trimmable = [d for d in raw if d[2].include and d[2].priority == tier]
            trimmable.sort(key=lambda d: d[1], reverse=True)
            for rel, size, decision in trimmable:
                if plan.included_bytes <= plan.budget_bytes:
                    break
                decision.include = False
                decision.reason = REASON_SIZE_LIMIT
                plan.included -= 1
                plan.included_bytes -= size
                plan.excluded += 1
                plan.excluded_bytes += size
                plan.extra_excluded_rel.add(rel)
                reason_stat(REASON_SIZE_LIMIT, size)

    # ---- finish -----------------------------------------------------------
    plan.decisions = {d[0].lower(): d[2] for d in raw}
    if newer_than_mtime is not None:
        # Freshness fusion: only INCLUDED files count. Excluded, sampled-out
        # and budget-trimmed files must never invalidate an archive they were
        # not part of. One stat per file already happened in the walk above.
        for _rel, _size, decision in raw:
            if decision.include and mtimes.get(_rel, 0) > newer_than_mtime:
                plan.newer_found = True
                break
    omitted = sorted(
        ((d[0], d[1]) for d in raw if not d[2].include and d[1] > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    plan.largest_omitted = omitted[:10]
    return plan


def build_plan_from_config(
    source: Path,
    packing,
    excludes: set[str],
    *,
    newer_than_mtime: Optional[float] = None,
) -> FidelityPlan:
    """Build a plan from a ``PackingConfig`` (or object with same attrs)."""
    return build_fidelity_plan(
        source,
        excludes,
        profile=str(getattr(packing, "fidelity_profile", DEFAULT_PROFILE)),
        max_mb=int(getattr(packing, "fidelity_max_mb", 0) or 0),
        media_samples=int(getattr(packing, "fidelity_media_samples", 0) or 0),
        media_bytes=int(getattr(packing, "fidelity_media_bytes", 0) or 0),
        always_include=list(getattr(packing, "always_include", None) or []),
        always_exclude=list(getattr(packing, "always_exclude", None) or []),
        newer_than_mtime=newer_than_mtime,
    )


def exclude_reason_summary(plan: FidelityPlan) -> dict[str, dict[str, int]]:
    """Stable, JSON-friendly reason stats (only categories with hits)."""
    return {k: dict(v) for k, v in sorted(plan.reason_stats.items())}
