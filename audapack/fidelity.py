"""Audit-fidelity profiles for AUDAPACK archives.

An archive is either an honest *audit representation* (COMPACT/STANDARD/DEEP,
files intentionally omitted under a declared policy) or a *full snapshot*
(FULL, only explicit exclusion/safety policy applies). Every discovered file
is accounted for exactly once, so ``discovered == included + excluded +
failed`` always holds and nothing silently disappears.

The engine never implements ``if size > budget: drop arbitrary files``.
Material is governed by a strict semantic priority hierarchy:
1. Safety / explicit exclusions (mandatory excludes, secrets, operator always_exclude)
2. Active control plane (.saipen root state, active intake .saipen/intake/active/**,
   canonical numeric audit layers audit/<numeric>.md and .saipen/audit/<numeric>.md,
   direct project-root active checkpoints *_CHECKPOINT.md) and manifest-declared
   requirements (project MANIFEST.json closure)
3. Source / tests / configs / schemas / build files
4. Useful prose (documentation, markdown guides)
5. Ordinary bulk / media / history (disposable assets, unreferenced media samples)

Profile byte budgets are SOFT TARGETS subordinate to audit correctness:
proven load-bearing control-plane material and manifest-declared requirements
are never removed merely to chase an arbitrary byte target. When mandatory
material alone exceeds the profile budget, budget feasibility is explicitly
surfaced:
  budget_bytes:        31457280
  budget_floor_bytes:  73400320
  budget_feasible:     false
  budget_met:          false
This is preferable to deleting load-bearing control-plane files or current task
instructions while still missing the target.
"""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
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
#: CORE-005 (audit/6.md): every audit profile has a real count, DEEP included.
#: 0 means "keep everything" and belongs to FULL alone -- while DEEP carried it,
#: three arbitrary 40 MB videos entered a 100 MB "bounded" archive untouched.
PROFILE_MEDIA_SAMPLES = {
    PROFILE_COMPACT: 1,
    PROFILE_STANDARD: 3,
    PROFILE_DEEP: 10,
    PROFILE_FULL: 0,     # 0 = keep everything
}

#: Per media directory byte cap for the physically included samples. Applied
#: JOINTLY with the count above: an unreferenced media file needs room under
#: both, never one or the other.
PROFILE_MEDIA_BYTES = {
    PROFILE_COMPACT: 1 * 1024 * 1024,
    PROFILE_STANDARD: 2 * 1024 * 1024,
    PROFILE_DEEP: 50 * 1024 * 1024,
    PROFILE_FULL: 0,
}

#: Sampling order threshold: files at or below this size are cheap coverage per
#: byte, so they are offered to the cap FIRST. They still consume both caps --
#: CORE-005: an unconditional keep for small files was a second way past the
#: count cap, which is the defect this layer removes.
MEDIA_SMALL_BYTES = 256 * 1024

#: PERF-003 (audit/6.md): how many media filenames a group reports as diagnostic
#: evidence. The group's counts and bytes stay EXACT -- only the per-file name
#: list is bounded, because the manifest previously carried one record per media
#: asset and grew linearly with the tree (20k media files: 1.13 MiB of payload).
MEDIA_SAMPLE_LIMIT = 5

#: How many of the largest omitted files the plan reports. Selected with a
#: bounded heap, never by sorting the whole omitted collection.
LARGEST_OMITTED_LIMIT = 10

#: How many of the largest included files the plan reports.
LARGEST_INCLUDED_LIMIT = 10

#: How many top directory byte contributors the plan reports.
LARGEST_INCLUDED_DIRECTORIES_LIMIT = 10

#: Bounded prose allowance settings for impossible budgets (Case 2).
PROSE_DISCRETIONARY_CEILING_RATIO = 0.10
PROSE_DISCRETIONARY_FLOOR_BYTES = 2 * 1024 * 1024


def prose_discretionary_allowance(budget_bytes: int) -> int:
    """Bounded prose allowance above mandatory floor when budget is impossible."""
    if budget_bytes <= 0:
        return 0
    return max(PROSE_DISCRETIONARY_FLOOR_BYTES, int(budget_bytes * PROSE_DISCRETIONARY_CEILING_RATIO))



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
    """FULL is a complete snapshot; everything else is an audit representation.

    This is the *declared* semantics of a profile. The semantics an archive
    actually earns is decided by ``semantics_for_plan`` after planning: a
    profile name alone cannot promise a snapshot.
    """
    return "full_snapshot" if normalize_fidelity_profile(profile) == PROFILE_FULL else "audit_representation"


#: Reasons that make an archive an audit representation rather than a snapshot:
#: fidelity POLICY decided to omit material. Explicit configuration and safety
#: exclusions are not in this set -- the operator asked for those, and a full
#: snapshot of a source minus its secrets is still a snapshot by contract.
#: Defined next to the reason names themselves, below.


#: Bumped when the fingerprint's canonical form changes, so archives fingerprinted
#: by an older rule are not compared against a newer one -- they simply repack.
#: 4 -- T-190 (SRC-046): archive membership moved from walk-derived to the
#: frozen source inventory (Git: tracked UNION untracked_nonignored, tracked
#: overrides noise excludes), so archives built by the pre-inventory writer can
#: silently differ in contents under an identical policy. One clean repack wave.
POLICY_FINGERPRINT_VERSION = 4


def packing_policy_fingerprint(
    *,
    profile: str,
    max_mb: int = 0,
    media_samples: int = 0,
    media_bytes: int = 0,
    excludes=(),
    always_include=(),
    always_exclude=(),
    mandatory_excludes=None,
) -> str:
    """Canonical identity of every content-affecting packing policy input.

    CORE-006 (audit/6.md): archive freshness was a timestamp property alone, so
    packing an archive as COMPACT, then switching the profile to FULL, reused the
    COMPACT archive -- an audit asking for a full snapshot silently consumed a
    sampled representation whose own manifest still said ``compact``.

    Fingerprinted over the EFFECTIVE policy, not the raw fields: the profile's
    resolved budgets are what decide content, so FULL's inert overrides (CORE-004)
    cannot produce a spurious mismatch, and an override equal to the profile
    default is correctly the same policy. Pattern lists are lowercased and sorted
    because the matcher is case-insensitive and order-independent.
    """
    from audapack.packing import MANDATORY_EXCLUDES

    profile = normalize_fidelity_profile(profile)

    def norm(patterns) -> list[str]:
        return sorted({str(p).strip().lower().replace("\\", "/") for p in patterns if str(p).strip()})

    canonical = {
        "v": POLICY_FINGERPRINT_VERSION,
        "profile": profile,
        "budget_bytes": profile_budget_bytes(profile, max_mb),
        "media_samples": profile_media_samples(profile, media_samples),
        "media_bytes": profile_media_bytes(profile, media_bytes),
        "excludes": norm(excludes),
        "always_include": norm(always_include),
        "always_exclude": norm(always_exclude),
        "mandatory": norm(MANDATORY_EXCLUDES if mandatory_excludes is None else mandatory_excludes),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def policy_fingerprint_from_config(packing, excludes) -> str:
    """``packing_policy_fingerprint`` for a ``PackingConfig`` + its excludes."""
    return packing_policy_fingerprint(
        profile=str(getattr(packing, "fidelity_profile", DEFAULT_PROFILE)),
        max_mb=int(getattr(packing, "fidelity_max_mb", 0) or 0),
        media_samples=int(getattr(packing, "fidelity_media_samples", 0) or 0),
        media_bytes=int(getattr(packing, "fidelity_media_bytes", 0) or 0),
        excludes=excludes,
        always_include=list(getattr(packing, "always_include", None) or []),
        always_exclude=list(getattr(packing, "always_exclude", None) or []),
    )


def profile_budget_bytes(profile: str, override_mb: int = 0) -> int:
    """Soft budget for the profile, or ``override_mb`` megabytes when set.

    CORE-004 (audit/6.md): FULL is structurally unbounded. A generic override
    used to apply to it too, so ``fidelity_max_mb=1`` trimmed a FULL archive by
    ``size_limit`` while the manifest still declared ``full_snapshot``. FULL now
    ignores the override rather than silently downgrading -- one canonical rule,
    enforced in the planner's only source of budgets.
    """
    if normalize_fidelity_profile(profile) == PROFILE_FULL:
        return 0
    if override_mb and override_mb > 0:
        return override_mb * 1024 * 1024
    return int(PROFILE_BUDGET_BYTES.get(normalize_fidelity_profile(profile), 0))


def profile_media_samples(profile: str, override: int = 0) -> int:
    if normalize_fidelity_profile(profile) == PROFILE_FULL:
        return 0  # 0 = keep everything; see profile_budget_bytes
    if override and override > 0:
        return override
    return int(PROFILE_MEDIA_SAMPLES.get(normalize_fidelity_profile(profile), 0))


def profile_media_bytes(profile: str, override: int = 0) -> int:
    if normalize_fidelity_profile(profile) == PROFILE_FULL:
        return 0
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

#: Failure categories. CORE-003 (audit/6.md): a file that could not be read is
#: not an exclusion -- nobody decided to omit it -- so failures carry their own
#: vocabulary instead of borrowing an exclusion reason. Part of the accounting
#: identity: ``sum(failure_stats.values()) == failed``.
FAILURE_STAT = "stat_failure"
FAILURE_WALK = "walk_failure"

FAILURE_CATEGORIES = (FAILURE_STAT, FAILURE_WALK)

#: Pause before re-probing a directory Windows refused with "access denied". A
#: directory that is being deleted reports that error until its last handle
#: closes, so one short re-probe tells "still going away" from "unreadable".
_DELETE_PENDING_REPROBE_S = 0.05

#: Omissions decided by fidelity POLICY rather than by the operator. CORE-004
#: (audit/6.md): if any of these fire, the archive is an audit representation no
#: matter which profile asked for it. Configuration, dependency/output and
#: secret exclusions are deliberately absent -- the operator asked for those, and
#: a snapshot of a source minus its secrets is still a snapshot by contract.
LOSSY_POLICY_REASONS = frozenset({REASON_MEDIA_BUDGET, REASON_SIZE_LIMIT})

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
    """Media class for a path's extension, or None for non-media files.

    PERF-001: called once per discovered file, so it takes the plain name the
    walk already has. Constructing a ``Path`` just to read one suffix cost more
    than the dict lookup it fed.
    """
    name = path if isinstance(path, str) else path.name
    name = name.rpartition("/")[2].rpartition("\\")[2]
    dot = name.rfind(".")
    # dot > 0 matches Path.suffix: a leading-dot name (".gitignore") has none.
    if dot <= 0:
        return None
    return MEDIA_EXTENSIONS.get(name[dot + 1:].lower())


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
    "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
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
    "package.json", "manifest.json", "pyproject.toml", "cmakelists.txt",
    "gradlew", "gradlew.bat",
})

#: Known config and manifest filenames recognized as priority 1 regardless of directory.
P1_JSON_EXACT_NAMES = frozenset({
    "manifest.json",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "tsconfig.json",
    "jsconfig.json",
    "composer.json",
    "composer.lock",
    "deno.json",
    "deno.jsonc",
    "turbo.json",
    "nx.json",
    "lerna.json",
    "workspace.json",
    "bower.json",
    "launch.json",
    "tasks.json",
    "settings.json",
    "extensions.json",
    "keybindings.json",
    "components.json",
    "theme.json",
    "angular.json",
    "nest-cli.json",
    "biome.json",
    "policy.json",
    "audit.json",
    "project.json",
    "bundleconfig.json",
    "api-extractor.json",
    "typedoc.json",
})

P1_JSON_SUFFIXES = (
    ".config.json",
    ".settings.json",
    ".schema.json",
    ".spec.json",
    ".manifest.json",
    ".policy.json",
)

P1_JSON_PREFIXES = (
    "tsconfig.",
    "jsconfig.",
    "appsettings.",
    "launchsettings.",
    "config.",
    "settings.",
)

DISCRETIONARY_JSON_DIRS = frozenset({
    "corpus", "sessions", "session", "history", "historical",
    "transcripts", "transcript", "conversations", "conversation",
    "conversation_history", "telemetry", "dumps", "dump",
    "exports", "export", "snapshots", "snapshot", "data_dump",
    "data_dumps", "dataset", "datasets", "bulk", "archives",
    "archive", "records", "events",
})

DISCRETIONARY_JSON_DIR_PREFIXES = (
    "session_", "corpus_", "history_", "dump_", "export_", "telemetry_",
)

DISCRETIONARY_JSON_DIR_SUFFIXES = (
    "_sessions", "_corpus", "_history", "_dumps", "_exports", "_telemetry",
)

DISCRETIONARY_JSON_NAME_PREFIXES = (
    "session", "history", "transcript", "conversation", "dump",
    "export", "telemetry", "snapshot", "corpus", "event_log",
)

JSON_CONFIG_MAX_BYTES = 256 * 1024

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


def _is_numeric_md(name_lower: str) -> bool:
    """True for <numeric>.md (e.g. 1.md, 3.md, 17.md)."""
    if not name_lower.endswith(".md"):
        return False
    stem = name_lower[:-3]
    return stem.isdigit()


def _priority_for(rel_lower: str, name_lower: str, size: int = 0) -> int:
    """Semantic priority: 1 = mandatory audit material, 2 = prose, 3 = asset.

    Budget enforcement only ever trims 3 then 2. Nothing trims 1.
    """
    segs = rel_lower.split("/")
    if segs[0] == ".saipen":
        # Decision documents at the memory root
        if len(segs) == 2 and name_lower in SAIPEN_P1_DOCS:
            return 1
        # Canonical active audit layer: .saipen/audit/<numeric>.md
        if len(segs) == 3 and segs[1] == "audit" and _is_numeric_md(name_lower):
            return 1
        # Active intake: .saipen/intake/active/**
        if len(segs) >= 4 and segs[1] == "intake" and segs[2] == "active":
            return 1
        # Ticket / work trees
        if len(segs) > 2 and segs[1] in SAIPEN_DOC_DIRS:
            return 2
        return 3

    # Direct project-root checkpoint documents: *_CHECKPOINT.md
    if len(segs) == 1 and (name_lower.endswith("_checkpoint.md") or name_lower == "checkpoint.md"):
        return 1

    # Canonical active audit layer: audit/<numeric>.md
    if len(segs) == 2 and segs[0] == "audit" and _is_numeric_md(name_lower):
        return 1

    if name_lower.startswith(P1_NAME_PREFIXES) or name_lower in P1_NAMES:
        return 1

    # Priority directories: git evidence (non-objects), schemas, specs, tests, tools, scripts, configs.
    for seg in segs:
        if seg in (
            ".github", "schemas", "schema", "specs", "spec",
            "config", "configs", ".config", ".vscode", ".idea", ".devcontainer"
        ):
            return 1
        if seg == "tests" or seg.startswith("test_") or seg.startswith("tests_") or seg in ("fixtures", "fixture"):
            return 1
        if seg == "scripts" or seg == "tools":
            return 1

    ext = name_lower.rsplit(".", 1)[1] if "." in name_lower else ""
    if ext in P1_EXTENSIONS:
        return 1

    # Role-aware JSON classification:
    if ext in ("json", "json5", "jsonc"):
        if name_lower in P1_JSON_EXACT_NAMES:
            return 1
        if name_lower.endswith(P1_JSON_SUFFIXES) or name_lower.startswith(P1_JSON_PREFIXES):
            return 1
        for seg in segs:
            if seg in DISCRETIONARY_JSON_DIRS:
                return 3
            if seg.startswith(DISCRETIONARY_JSON_DIR_PREFIXES) or seg.endswith(DISCRETIONARY_JSON_DIR_SUFFIXES):
                return 3
        if name_lower.startswith(DISCRETIONARY_JSON_NAME_PREFIXES):
            return 3
        if size > JSON_CONFIG_MAX_BYTES:
            return 3
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
    if name_lower.endswith((".secret", ".secrets", ".token", ".ppk")) or name_lower in (
        "token.txt", "secrets", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"
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
    r"""["'\\(\s]([A-Za-z0-9_\-.\\/ ]+\.(?:bin|wav|mp3|ogg|flac|aac|m4a|mp4|avi|mov|mkv|webm|png|jpe?g|gif|webp|bmp|ico|tiff?|woff2?|ttf|otf|eot|npy|npz|h5|hdf5|mat))["'\)\s,]""",
    re.IGNORECASE,
)
_REF_DIR_RE = re.compile(
    r"""["'\\(\s]([A-Za-z0-9_\-.\\/ ]*(?:Sounds?|Audio|Videos?|Media|Images?|Assets|Fonts?|Resources?)[/\\][A-Za-z0-9_\-.\\/ ]*)["'\)\s,]""",
    re.IGNORECASE,
)


def scan_asset_references(
    source: Path,
    prune: Optional[Callable[[Path], bool]] = None,
    known_media_rel_lower: Optional[set[str]] = None,
) -> tuple[set[str], set[str]]:
    """Find assets referenced by build/runtime text files.

    Returns ``(referenced_files, referenced_dirs)`` with POSIX-relative paths
    inside ``source``. Bounded: skips non-text and files over 256 KiB, stops
    after ~4 MiB of scanned text. Exact file references outrank directory
    references during media sampling.

    T-147: when ``prune`` is supplied (the plan's configured/mandatory
    matcher) excluded directories and files are never descended into or
    stat'ed, so the reference scan costs the same as the pack itself.

    T-168: when ``known_media_rel_lower`` is supplied (the plan's already
    completed classification walk IS the existence evidence), a reference
    whose normalized path names one of those discovered media entries is
    verified without an exact-case ``is_file()`` probe -- reference matching
    is case-insensitive by contract, while file identity stays exact-case.
    Without it the exact candidate containment + existence check remains the
    only verification path.
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
                    # T-168: membership in the already-discovered media
                    # inventory verifies the reference case-insensitively --
                    # the set only ever contains paths the classification
                    # walk found inside the source tree, so containment and
                    # existence both hold without an exact-case stat probe.
                    if (
                        known_media_rel_lower is not None
                        and raw.lower() in known_media_rel_lower
                    ):
                        referenced_files.add(raw)
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


class PriorityBytes(dict):
    """Dictionary mapping priority to byte totals supporting int and str keys."""

    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        try:
            ikey = int(key)
            if ikey in self:
                return super().__getitem__(ikey)
        except (ValueError, TypeError):
            pass
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


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
    #: The four fields below carry profile-derived defaults so a plan can be
    #: constructed by name alone (the CORE-004 defensiveness tests build a bare
    #: plan and flip individual flags). ``build_fidelity_plan`` always passes
    #: them explicitly, so the post-init fill only ever backstops a manual plan.
    archive_semantics: Optional[str] = None
    budget_bytes: Optional[int] = None
    media_samples: Optional[int] = None
    media_bytes_per_dir: Optional[int] = None
    decisions: dict[str, FileDecision] = field(default_factory=dict)
    #: Relative paths excluded purely by profile decisions (media budget,
    #: size limit, always_exclude) so freshness walks can skip them too.
    extra_excluded_rel: set[str] = field(default_factory=set)
    #: Relative directories pruned whole -> ``{"reason", "files", "bytes"}`` so
    #: the zipper never descends into them and the manifest can say how much
    #: material the prune removed. CORE-003 (audit/6.md): the tree below a prune
    #: used to vanish from every counter, so a manifest could not answer how much
    #: source was omitted -- three physical files reported discovered=1.
    pruned_dirs_rel: dict[str, dict[str, object]] = field(default_factory=dict)
    #: False when the pruned-tree census was deliberately not taken. A freshness
    #: probe never touches excluded weight (PERF-004), and the census is manifest
    #: metadata a reuse decision does not consume -- so the counts below describe
    #: only the traversed tree and must not read as a full census.
    pruned_census_taken: bool = True
    discovered: int = 0
    included: int = 0
    excluded: int = 0
    failed: int = 0
    source_bytes: int = 0
    included_bytes: int = 0
    excluded_bytes: int = 0
    mandatory_bytes: int = 0
    discretionary_bytes: int = 0
    budget_floor_bytes: int = 0
    budget_feasible: bool = True
    included_bytes_by_priority: dict[int, int] = field(default_factory=PriorityBytes)
    largest_included: list[dict[str, object]] = field(default_factory=list)
    largest_included_directories: list[dict[str, object]] = field(default_factory=list)
    #: Discovered entries whose byte size could not be determined. Their bytes
    #: are absent from ``source_bytes`` rather than fabricated as a known zero,
    #: so "unknown" stays distinguishable from "empty".
    unknown_size_entries: int = 0
    reason_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Failure categories (``FAILURE_CATEGORIES``). A file nobody decided to omit
    #: is not an exclusion, so failures are counted in their own vocabulary and
    #: ``sum(failure_stats.values()) == failed`` is part of the identity.
    failure_stats: dict[str, int] = field(default_factory=dict)
    largest_omitted: list[tuple[str, int]] = field(default_factory=list)
    #: PERF-003: one entry per media GROUP, keyed ``"<directory>#<class>"``.
    #: Counts and bytes are exact; the filename lists are bounded samples. The
    #: previous shape kept one record per media asset and was serialized into
    #: every manifest, so both planner memory and manifest size grew with the
    #: media count. Keying by directory alone also let a second media class in
    #: the same directory overwrite the first group's aggregates outright.
    media_inventory: dict[str, dict[str, object]] = field(default_factory=dict)
    referenced_files: set[str] = field(default_factory=set)
    #: Freshness fusion (T-147): True when any INCLUDED file is newer
    #: than ``newer_than_mtime``. Excluded/sampled/trimmed files never
    #: count, so a sampled-out media file can never force endless repacks.
    newer_found: bool = False
    #: T-155 (PERF-004 residue): True when the probe stopped the traversal the
    #: moment a priority-1 INCLUDED file proved the archive stale. The plan
    #: then describes only the prefix the probe walked -- a truthful stale
    #: verdict that must never be packed, because the tree was never finished.
    stopped_on_stale: bool = False
    #: T-155: True when the caller's ``cancel_event`` fired mid-traversal. The
    #: plan is a prefix of the tree and carries no freshness verdict at all;
    #: the caller must abandon the decision instead of reusing or packing it.
    cancelled: bool = False
    #: True when the walk could not enumerate the whole tree (an untraversable
    #: directory, or an OSError that ended the walk early). The counters below
    #: then describe only what was reached, so an archive built from this plan
    #: is not a complete audit representation and the manifest must say so --
    #: without this flag the identity discovered == included + excluded + failed
    #: still holds over the truncated numbers and reads as full accounting.
    walk_incomplete: bool = False
    #: Entries that disappeared between their parent's listing and their own
    #: read (a live tool removing its session or lock directories mid-walk).
    #: They no longer exist, so they are neither discovered nor failed; the
    #: count only records that the tree changed under the walk.
    vanished_entries: int = 0
    #: Source-relative path of the first entry the walk could not read, so an
    #: incomplete traversal names something the operator can go look at.
    first_failure_rel: Optional[str] = None
    #: Why this plan's accounting does not reconcile, or None. Set at the end of
    #: ``build_fidelity_plan`` from ``plan_accounting_error``; a non-None value
    #: forbids a full_snapshot claim.
    accounting_error: Optional[str] = None
    #: Canonical identity of the policy that produced this plan. CORE-006
    #: (audit/6.md): archive reuse was decided on mtime alone, so a profile or
    #: exclude change silently reused an archive with materially different
    #: contents. Persisted in the manifest, compared before any mtime reuse.
    policy_fingerprint: str = ""
    #: T-155: whole-pruned directories whose census is pending, rel -> path.
    #: The manifest writer finalizes these counts; a probe that stopped early
    #: never carries them. Internal, not serialized.
    _pruned_paths: dict[str, Path] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        profile = normalize_fidelity_profile(self.profile)
        if self.archive_semantics is None:
            self.archive_semantics = archive_semantics_for(profile)
        if self.budget_bytes is None:
            self.budget_bytes = profile_budget_bytes(profile)
        if self.media_samples is None:
            self.media_samples = profile_media_samples(profile)
        if self.media_bytes_per_dir is None:
            self.media_bytes_per_dir = profile_media_bytes(profile)

    def decision_for(self, rel_posix: str) -> Optional[FileDecision]:
        """Exact source-relative POSIX path lookup.

        T-150: file identity is case-EXACT -- ``Asset.PNG`` and ``asset.png``
        are two distinct files with two distinct decisions. Policy and
        reference matching stay case-insensitive; only this identity map is
        exact, so no lowercase fallback exists here by design.
        """
        return self.decisions.get(rel_posix)

    @property
    def budget_met(self) -> bool:
        """True when the included bytes do not exceed the plan's byte budget."""
        if self.budget_bytes is None or self.budget_bytes == 0:
            return True
        return self.included_bytes <= self.budget_bytes


def plan_accounting_error(plan: "FidelityPlan") -> Optional[str]:
    """Why ``plan``'s accounting is untruthful, or None when it reconciles.

    CORE-003 (audit/6.md): ``discovered == included + excluded + failed`` was
    advertised but never enforced, and it is not sufficient on its own -- an
    excluded file with no reason, a reason total that disagrees with the
    terminals, or bytes that do not add up all leave the identity intact. This
    is the single oracle both the planner and the manifest are checked against.
    """
    if plan.discovered != plan.included + plan.excluded + plan.failed:
        return (
            f"discovered {plan.discovered} != included {plan.included} + "
            f"excluded {plan.excluded} + failed {plan.failed}"
        )
    reason_count = sum(int(s.get("count", 0)) for s in plan.reason_stats.values())
    if reason_count != plan.excluded:
        return f"reason totals {reason_count} != excluded {plan.excluded}"
    reason_bytes = sum(int(s.get("bytes", 0)) for s in plan.reason_stats.values())
    if reason_bytes != plan.excluded_bytes:
        return f"reason bytes {reason_bytes} != excluded_bytes {plan.excluded_bytes}"
    failure_count = sum(plan.failure_stats.values())
    if failure_count != plan.failed:
        return f"failure totals {failure_count} != failed {plan.failed}"
    unknown = set(plan.reason_stats) - set(REASON_CATEGORIES)
    if unknown:
        return f"unknown exclusion reason(s): {sorted(unknown)}"
    unknown_failures = set(plan.failure_stats) - set(FAILURE_CATEGORIES)
    if unknown_failures:
        return f"unknown failure categor(y/ies): {sorted(unknown_failures)}"
    if plan.source_bytes != plan.included_bytes + plan.excluded_bytes:
        return (
            f"source_bytes {plan.source_bytes} != included {plan.included_bytes} + "
            f"excluded {plan.excluded_bytes} (unknown-size entries: "
            f"{plan.unknown_size_entries})"
        )
    undecided = [rel for rel, d in plan.decisions.items() if not d.include and not d.reason]
    if undecided:
        return f"excluded without a reason: {sorted(undecided)[:3]}"
    return None


def pruned_census_totals(plan: "FidelityPlan") -> tuple[int, int]:
    """``(files, bytes)`` a whole-directory prune removed from the archive."""
    files = sum(int(info.get("files", 0)) for info in plan.pruned_dirs_rel.values())
    size = sum(int(info.get("bytes", 0)) for info in plan.pruned_dirs_rel.values())
    return files, size


@lru_cache(maxsize=16384)
def _lower_path_parts(key: str) -> tuple[str, tuple[str, ...]]:
    """``(name_lower, parts_lower)`` for a path, parsed once.

    PERF-001 (audit/6.md): the four exclusion matchers are asked about the same
    path in sequence, and each one re-parsed it into a ``Path`` and re-lowered
    every component. Bounded cache: a walk of an arbitrarily large tree cannot
    grow it without limit.
    """
    p = Path(key)
    return p.name.lower(), tuple(part.lower() for part in p.parts)


def _build_matcher(patterns: set[str]):
    lowered = frozenset(pat.lower() for pat in patterns)
    exact = {p for p in lowered if not any(ch in p for ch in "*?[") and "/" not in p}
    # PERF-001: ONE alternation instead of one fullmatch per pattern per path
    # component. The freshness profile showed 465,000 generator+fullmatch calls
    # for a 1,000-file tree (52% of the whole reuse decision) purely because
    # every glob was tried separately against every component.
    glob_pats = sorted(p for p in lowered if p not in exact and "/" not in p)
    glob_re = (
        re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in glob_pats))
        if glob_pats
        else None
    )
    multi = tuple(
        tuple(seg for seg in p.split("/") if seg)
        for p in lowered
        if "/" in p
    )
    multi_res = tuple(tuple(re.compile(fnmatch.translate(s)) for s in segs) for segs in multi)

    def matches(path: Path | str) -> bool:
        name, parts = _lower_path_parts(path if isinstance(path, str) else str(path))
        for part in (name, *parts):
            if part in exact or (glob_re is not None and glob_re.fullmatch(part)):
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


def _census_pruned_tree(top: Path) -> tuple[int, int, int]:
    """``(files, known_bytes, unknown_size_entries)`` under a whole-pruned dir.

    CORE-003 (audit/6.md): a pruned subtree used to vanish from every counter,
    so the manifest could not answer how much source material was omitted. The
    census never opens a file: ``os.scandir`` already carries the size on the
    platforms this ships on, so representing the omission costs directory
    enumeration and nothing more. A size that cannot be read is reported as
    unknown rather than fabricated as a known zero.
    """
    files = 0
    known = 0
    unknown = 0
    stack = [top]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                    except OSError:
                        files += 1
                        unknown += 1
                        continue
                    files += 1
                    try:
                        known += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        unknown += 1
        except OSError:
            # An unreadable pruned directory is still omitted material; its
            # contents are simply uncountable. Counted as one unknown entry so
            # the census never silently reports a complete zero.
            files += 1
            unknown += 1
    return files, known, unknown


def _scan_live_directory(path: Path, *, may_vanish: bool) -> Optional[list]:
    """List ``path``, or return None when it vanished after its parent listed it.

    A project whose own tooling runs during the pack (agent session, exec and
    lock directories) removes directories between the parent's listing and this
    read. A directory that no longer exists holds no source the walk could miss,
    so that is not a traversal failure. Windows reports a directory pending
    deletion as access denied, so one short re-probe separates "still going
    away" from "unreadable". Every other error, and any error on the source
    root itself (``may_vanish=False``), still raises.
    """
    for attempt in range(2):
        try:
            with os.scandir(path) as it:
                return list(it)
        except FileNotFoundError:
            if may_vanish:
                return None
            raise
        except PermissionError:
            if not may_vanish or attempt:
                raise
            time.sleep(_DELETE_PENDING_REPROBE_S)
    return None  # pragma: no cover - the loop always returns or raises


def _take_pruned_census(
    plan: "FidelityPlan", rel_dir: str, info: dict[str, object], path: Path
) -> None:
    """Finalize one prune's census into ``plan`` (T-155: once, on demand).

    The counters must not read as complete while the tree is unfinished, so a
    probe that stops early leaves its prunes uncensused and the manifest writer
    calls this for every prune it is about to describe.
    """
    if plan._pruned_paths.get(rel_dir) is None:
        return
    files, known, unknown = _census_pruned_tree(path)
    info["files"] = files
    info["bytes"] = known
    if unknown:
        info["unknown_size_entries"] = unknown
    if files:
        plan.discovered += files
        plan.excluded += files
        plan.source_bytes += known
        plan.excluded_bytes += known
        plan.unknown_size_entries += unknown
        reason = str(info.get("reason", ""))
        stats = plan.reason_stats.setdefault(reason, {"count": 0, "bytes": 0})
        stats["count"] += files
        stats["bytes"] += known
    plan._pruned_paths.pop(rel_dir, None)


def finalize_pruned_census(plan: "FidelityPlan") -> None:
    """Take every pending prune census so ``plan`` fully describes its tree."""
    for rel_dir, path in list(plan._pruned_paths.items()):
        _take_pruned_census(plan, rel_dir, plan.pruned_dirs_rel[rel_dir], path)


def _extract_manifest_required(
    manifest_path: Path,
    manifest_rel: str,
    all_known_rels: set[str],
    lower_to_exact: dict[str, list[str]],
) -> set[str]:
    """Conservative required-file closure for project-owned MANIFEST.json files.

    Enforces the conservative manifest contract:
    - filename must be MANIFEST.json, case-insensitive;
    - JSON must parse as an object;
    - recognized 'required' member with relative file paths (string or structured);
    - reject absolute paths and '..' escapes;
    - resolve only inside source root;
    - require path to exist in discovered inventory;
    - malformed manifests fail closed;
    - ambiguous case twins protect both.
    """
    promoted: set[str] = set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return promoted
    if not isinstance(data, dict):
        return promoted
    raw_required = data.get("required")
    if not isinstance(raw_required, (list, tuple, set, dict, str)):
        return promoted

    items: list[object] = []
    if isinstance(raw_required, (list, tuple, set)):
        items = list(raw_required)
    elif isinstance(raw_required, dict):
        items = list(raw_required.keys())
    elif isinstance(raw_required, str):
        items = [raw_required]

    manifest_dir = manifest_rel.rpartition("/")[0]

    for item in items:
        if isinstance(item, str):
            p_str = item
        elif isinstance(item, dict):
            p_str = item.get("path") or item.get("file") or item.get("rel") or item.get("name")
            if not isinstance(p_str, str):
                continue
        else:
            continue

        p_str = p_str.strip()
        if not p_str:
            continue
        p_norm = p_str.replace("\\", "/")
        # Reject absolute paths (POSIX leading slash or Windows drive letter)
        if p_norm.startswith("/") or re.match(r"^[a-zA-Z]:", p_norm):
            continue
        # Reject .. escape
        parts = [seg for seg in p_norm.split("/") if seg]
        if ".." in parts:
            continue
        clean = "/".join(parts)
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue

        cand_dir = f"{manifest_dir}/{clean}" if manifest_dir else clean
        cand_root = clean if manifest_dir else None

        # Check exact inventory first
        if cand_dir in all_known_rels:
            promoted.add(cand_dir)
            continue
        if cand_root and cand_root in all_known_rels:
            promoted.add(cand_root)
            continue

        # Case-insensitive resolution (for Windows-style or case tolerance)
        matches = lower_to_exact.get(cand_dir.lower())
        if not matches and cand_root:
            matches = lower_to_exact.get(cand_root.lower())
        if matches:
            for m in matches:
                promoted.add(m)
    return promoted


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
    census_pruned: bool = True,
    cancel_event: object = None,
) -> FidelityPlan:
    """Classify every file under ``source`` and decide what the archive holds.

    One traversal, one truth: the returned plan feeds both the zipper and the
    freshness walk, so the archive and the freshness check can never drift.

    ``census_pruned`` enumerates whole-pruned directories (names and sizes only,
    never contents) so their omission is represented in the counters instead of
    erased. A freshness probe passes False: it consumes none of that metadata and
    must not touch excluded weight (PERF-004).

    ``cancel_event`` (T-155) is a ``threading.Event``-like object checked
    throughout the traversal: when it fires, ``plan.cancelled`` is set and the
    walk returns with only the prefix it managed to see.
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
    plan.pruned_census_taken = census_pruned
    always_incl = _build_matcher({p for p in (always_include or [])})
    always_excl = _build_matcher({p for p in (always_exclude or [])})
    configured = _build_matcher({p for p in excludes})
    mandatory = _build_matcher(set(mandatory_excludes or MANDATORY_EXCLUDES))
    plan.policy_fingerprint = packing_policy_fingerprint(
        profile=profile,
        max_mb=max_mb,
        media_samples=media_samples,
        media_bytes=media_bytes,
        excludes=excludes,
        always_include=always_include or (),
        always_exclude=always_exclude or (),
        mandatory_excludes=mandatory_excludes,
    )

    # PERF-001 (audit/6.md): the reference scan is a SECOND full walk that reads
    # source text, and its only consumers are media sampling and
    # ``plan.referenced_files``. It used to run unconditionally before the
    # classification walk, so an unchanged 100-file Python project paid one extra
    # traversal and 100 file reads merely to decide an archive was fresh
    # (measured FRESHNESS_COUNTS {'walks': 3, 'reads': 100}). Deferred until the
    # tree is known to hold media: a tree with nothing to sample cannot be
    # affected by the answer.
    referenced_files: set[str] = set()
    referenced_dirs: set[str] = set()

    # Media groups: (media_class, parent_rel) -> list of (rel, size, path)
    media_groups: dict[tuple[str, str], list[tuple[str, int, Path]]] = {}
    # Generic .bin assets are opaque project material, not media. Keep a small
    # existence index so the existing bounded reference scan can promote a
    # verified project-local reference without making every .bin mandatory.
    binary_assets: set[str] = set()
    manifest_candidates: list[tuple[str, Path]] = []
    # Non-media decisions first (media sampling needs the full group first).
    raw: list[tuple[str, int, FileDecision]] = []
    mtimes: dict[str, float] = {}  # rel -> st_mtime, fused freshness data

    def reason_stat(reason: str, size: int, count: int = 1) -> None:
        stats = plan.reason_stats.setdefault(reason, {"count": 0, "bytes": 0})
        stats["count"] += count
        stats["bytes"] += size

    def failure_stat(category: str, rel: str, count: int = 1) -> None:
        """Count a file nobody chose to omit: the tree, not the policy, refused.

        CORE-003 (audit/6.md): ``failed`` had no category vocabulary at all, so
        an I/O failure was indistinguishable from a decision in the manifest.
        Every increment is paired with ``discovered`` and with an unknown byte
        size -- a size that could not be read is never reported as a known zero.
        """
        if plan.first_failure_rel is None:
            plan.first_failure_rel = rel or "."
        plan.discovered += count
        plan.failed += count
        plan.unknown_size_entries += count
        plan.failure_stats[category] = plan.failure_stats.get(category, 0) + count

    def prune_dir(rel_dir: str, reason: str, path: Path) -> None:
        """Record a whole-directory prune, with the material it removed.

        The census counts files and bytes without opening one, so the manifest
        can answer "how much was omitted here" -- the tree below a prune used to
        disappear from every counter, which is what let three physical files
        report ``discovered=1``.
        """
        info: dict[str, object] = {"reason": reason}
        plan.pruned_dirs_rel[rel_dir] = info
        plan._pruned_paths[rel_dir] = path
        if not census_pruned:
            return
        _take_pruned_census(plan, rel_dir, info, path)

    # PERF-001 (audit/6.md): one scandir traversal, and every entry's metadata
    # comes from the directory read that found it. ``os.walk`` discards the
    # ``os.DirEntry`` objects it already built, so classifying a file cost a
    # second and third syscall (``is_symlink`` then ``stat``) plus a ``Path``
    # construction and a ``relative_to`` per entry -- 2,030 stat calls for a
    # 1,000-file tree that scandir had already described.
    stack: list[tuple[Path, str]] = [(source, "")]
    # T-155 (PERF-004 residue): the first INCLUDED file newer than the archive
    # already settles a staleness verdict. Only PRIORITY-1 includes qualify for
    # the early exit: the soft-budget trim runs after the walk and may reverse
    # a tier-2/3 include, and a file the finished plan would exclude must never
    # invalidate the archive. Media decisions are deferred to the sampler, so
    # they stay on the post-walk verdict. A probe that stops early reports a
    # truthful prefix: the counters describe only what was walked, the prune
    # census stays pending, and the caller must treat the plan as a verdict,
    # never as a pack input.
    early_verdict = newer_than_mtime is not None

    def _stale_hit(decision: FileDecision) -> bool:
        if not early_verdict:
            return False
        if not decision.include or decision.priority != 1:
            return False
        return mtimes.get(decision.rel, 0.0) > newer_than_mtime

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    try:
        while stack:
            if _cancelled():
                plan.cancelled = True
                break
            if early_verdict and plan.newer_found:
                plan.stopped_on_stale = True
                break
            base, rel_prefix = stack.pop()
            try:
                entries = _scan_live_directory(base, may_vanish=bool(rel_prefix))
            except OSError:
                # A directory that cannot be enumerated means the files below it
                # were never discovered: freshness cannot be proven, so the plan
                # counts the failure and the caller must repack rather than reuse
                # an archive. It counts as discovered as well -- every increment
                # of failed/excluded has a matching discovered, or the manifest
                # identity discovered == included + excluded + failed silently
                # goes false and an incomplete archive reads as fully accounted
                # for.
                failure_stat(FAILURE_WALK, rel_prefix)
                plan.walk_incomplete = True
                continue
            if entries is None:
                plan.vanished_entries += 1
                continue
            child_dirs: list[tuple[Path, str]] = []
            stop_now = False
            for entry in entries:
                if _cancelled():
                    plan.cancelled = True
                    stop_now = True
                    break
                name = entry.name
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                try:
                    is_dir = entry.is_dir()
                except FileNotFoundError:
                    plan.vanished_entries += 1
                    continue
                except OSError:
                    failure_stat(FAILURE_STAT, rel)
                    continue
                if is_dir:
                    rel_dir = rel.lower()
                    # PERF-004 (audit/2.md): matcher BEFORE any symlink probe.
                    # The probe is filesystem metadata work, and an excluded
                    # directory must never incur it (packing.py's
                    # eligible_source_files short-circuits the same way).
                    if mandatory(rel_dir):
                        prune_dir(rel_dir, REASON_SECRET_POLICY, Path(entry.path))
                        continue
                    if always_excl(rel_dir):
                        plan.extra_excluded_rel.add(rel_dir)
                        prune_dir(rel_dir, REASON_CONFIGURED_IGNORE, Path(entry.path))
                        continue
                    if configured(rel_dir):
                        prune_dir(
                            rel_dir, exclusion_reason_for(rel_dir, name.lower()), Path(entry.path)
                        )
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                    except OSError:
                        # An unreadable directory entry is one failed entry, not
                        # a reason to abandon the rest of the tree.
                        failure_stat(FAILURE_WALK, rel)
                        plan.walk_incomplete = True
                        continue
                    child_dirs.append((Path(entry.path), rel))
                    continue
                # The symlink probe raises on an unreadable entry, so it shares
                # the guard: outside it, one EACCES file aborted the whole walk
                # and the plan reported an empty tree as a complete audit
                # representation.
                try:
                    if entry.is_symlink():
                        # A link's own bytes are not source material and its
                        # target may sit outside the tree, so the size stays
                        # unknown rather than being called a known zero.
                        plan.discovered += 1
                        plan.excluded += 1
                        plan.unknown_size_entries += 1
                        reason_stat(REASON_UNSUPPORTED, 0)
                        raw.append((rel, 0, FileDecision(rel, 0, False, REASON_UNSUPPORTED)))
                        continue
                    # follow_symlinks=False reuses the metadata scandir already
                    # returned, so this costs no syscall at all on Windows and
                    # is equivalent for a non-symlink, which is all that reaches
                    # here.
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size
                except FileNotFoundError:
                    # Deleted after the listing: no longer source material.
                    plan.vanished_entries += 1
                    continue
                except OSError:
                    failure_stat(FAILURE_STAT, rel)
                    continue
                plan.discovered += 1
                plan.source_bytes += size
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
                    plan.extra_excluded_rel.add(rel_lower)
                    reason_stat(REASON_CONFIGURED_IGNORE, size)
                    raw.append((rel, size, FileDecision(rel, size, False, REASON_CONFIGURED_IGNORE)))
                    continue
                # 2b. user always_include wins over configured excludes (but
                # never over safety policy above): an explicitly named file is
                # mandatory audit material even when its extension sits in the
                # default exclude list (e.g. small mandatory *.bin patch assets).
                if always_incl(rel_lower):
                    priority = 1
                    plan.included += 1
                    plan.included_bytes += size
                    raw.append((rel, size, FileDecision(rel, size, True, None, priority)))
                    continue
                # 3. configured excludes (deps / generated / plain).
                if configured(rel_lower):
                    reason = exclusion_reason_for(rel_lower, name_lower)
                    plan.excluded += 1
                    plan.excluded_bytes += size
                    reason_stat(reason, size)
                    raw.append((rel, size, FileDecision(rel, size, False, reason)))
                    continue

                if name_lower == "manifest.json":
                    manifest_candidates.append((rel, Path(entry.path)))

                mclass = media_class_for(name)
                if mclass is not None:
                    # PERF-003: rel + size is everything the sampler decides on.
                    # A per-file Path object was retained here and never read.
                    media_groups.setdefault((mclass, rel_prefix or "."), []).append(
                        (rel, size)
                    )
                    continue
                if name_lower.endswith(".bin"):
                    binary_assets.add(rel)

                priority = _priority_for(rel_lower, name_lower, size)
                # 4. user always_include wins over the profile: an explicitly
                # named file is mandatory material, so no budget trims it.
                if always_incl(rel_lower):
                    priority = 1
                plan.included += 1
                plan.included_bytes += size
                decision = FileDecision(rel, size, True, None, priority)
                raw.append((rel, size, decision))
                if _stale_hit(decision):
                    plan.newer_found = True
            # Depth-first in directory order, so an identical tree plans in an
            # identical sequence.
            stack.extend(reversed(child_dirs))
            if stop_now or (early_verdict and plan.newer_found):
                # T-155: a cancel or a settled staleness verdict ends the probe;
                # the child directories were queued but never walked.
                if plan.newer_found and early_verdict:
                    plan.stopped_on_stale = True
                break
    except OSError:
        # Last-resort guard: the per-entry handlers above own the expected
        # failures, so reaching here means the walk itself died and the tree is
        # only partly enumerated. Marked, never swallowed silently.
        plan.walk_incomplete = True

    # ---- media sampling ---------------------------------------------------
    # CORE-005 (audit/6.md): sampling is a JOINT cap. An unreferenced media file
    # is kept only while BOTH the per-directory sample count and the per-
    # directory byte budget still have room. Either one alone used to be enough,
    # so a nominal 3-sample STANDARD directory kept five 400 KB files, and DEEP
    # (samples=0, i.e. "keep everything") let three arbitrary 40 MB videos
    # produce a ~120 MB archive inside a declared 100 MB tier.
    #
    # Protected rather than capped: explicit always_include, and media a build or
    # runtime text references BY NAME. Those are mandatory material (priority 1).
    # Every sampled-in unreferenced file is an ordinary asset (priority 3), so
    # the global soft budget below can still reach it -- media used to sit at
    # priority 2 above ordinary assets, which shielded exactly the arbitrary
    # bulk this cap exists to bound.
    #
    # PERF-001: the reference scan happens HERE, once, and only when the walk
    # actually found media to rank. Nothing above this point consumes it.
    # T-155: a probe that settled its verdict (or was cancelled) mid-tree
    # consumes none of this: sampling, the reference scan and the budget trim
    # would rank media the finished walk never saw. The verdict rests on the
    # walked prefix alone; the plan is a verdict, never a pack input.
    if plan.cancelled or plan.stopped_on_stale:
        # A prefix of the tree: the counters describe only what was reached,
        # so the plan must never read as a fully enumerated one.
        plan.walk_incomplete = True
    prefix_verdict = plan.stopped_on_stale or plan.cancelled
    # Manifest required file resolution: conservative closure over discovered inventory
    manifest_required_files: set[str] = set()
    if manifest_candidates and not prefix_verdict:
        exact_inventory = {d[0]: d[2] for d in raw}
        all_known_rels = set(exact_inventory.keys())
        for members in media_groups.values():
            for rel_m, _size_m in members:
                all_known_rels.add(rel_m)
        lower_to_exact: dict[str, list[str]] = {}
        for r in all_known_rels:
            lower_to_exact.setdefault(r.lower(), []).append(r)

        for m_rel, m_path in manifest_candidates:
            reqs = _extract_manifest_required(
                m_path,
                m_rel,
                all_known_rels,
                lower_to_exact,
            )
            manifest_required_files.update(reqs)

        for req_rel in manifest_required_files:
            dec = exact_inventory.get(req_rel)
            if dec is not None and dec.include:
                dec.priority = 1

    if (media_groups or binary_assets) and not prefix_verdict:
        # T-168/M1: the classification walk already discovered every physical
        # media or generic binary asset; their relative paths are existence
        # evidence for the one bounded reference scan. No second source walk is
        # introduced for .bin, and only a verified project-local reference is
        # promoted below.
        known_media_rel_lower = {
            rel.lower()
            for members in media_groups.values()
            for rel, _size in members
        }
        known_media_rel_lower.update(rel.lower() for rel in binary_assets)
        referenced_files, referenced_dirs = scan_asset_references(
            source,
            prune=lambda p: always_excl(p) or configured(p) or mandatory(p),
            known_media_rel_lower=known_media_rel_lower,
        )
        plan.referenced_files = referenced_files
    referenced_files_lower = {rel.lower() for rel in referenced_files}
    referenced_dirs_lower = {rel.lower() for rel in referenced_dirs}

    def _ref_rank(rel: str) -> int:
        """0 = referenced by name, 1 = under a referenced dir, 2 = arbitrary.

        Case-insensitive: a reference written ``Sounds/theme.wav`` names the same
        asset as ``Sounds/Theme.wav`` on the filesystems this ships on.
        """
        low = rel.lower()
        if low in referenced_files_lower:
            return 0
        segs = low.split("/")
        for seg_i in range(1, len(segs)):
            if "/".join(segs[:seg_i]) in referenced_dirs_lower:
                return 1
        return 2

    for (_mclass, _parent_rel), members in sorted(media_groups.items()):
        keep_all = profile == PROFILE_FULL or plan.media_samples <= 0
        # Deterministic order: referenced first, then small files (most coverage
        # per byte), then largest-first, then path. Identical trees plan
        # identically, which is what makes a sampled archive reproducible -- and
        # it makes the bounded samples below deterministic for free. Sorted IN
        # PLACE so the inventory pass below can reuse it and PERF-003 does not
        # trade a per-file manifest ledger for a second per-file list.
        members.sort(
            key=lambda m: (
                _ref_rank(m[0]),
                0 if m[1] <= MEDIA_SMALL_BYTES else 1,
                -m[1],
                m[0].lower(),
                # T-150: case-distinct paths share a lowercase sort key, so the
                # exact path is the final tie-break -- order must not lean on
                # traversal order.
                m[0],
            ),
        )
        samples_left = plan.media_samples
        cap_bytes = plan.media_bytes_per_dir
        budget_left = cap_bytes if cap_bytes > 0 else None
        for rel, size in members:
            protected = (
                keep_all
                or always_incl(rel.lower())
                or _ref_rank(rel) == 0
                or rel in manifest_required_files
            )
            if protected:
                keep = True
            else:
                keep = samples_left > 0 and (budget_left is None or budget_left >= size)
            if keep:
                plan.included += 1
                plan.included_bytes += size
                if not protected:
                    samples_left -= 1
                    if budget_left is not None:
                        budget_left -= size
                raw.append((rel, size, FileDecision(rel, size, True, None, 1 if protected else 3)))
            else:
                plan.excluded += 1
                plan.excluded_bytes += size
                plan.extra_excluded_rel.add(rel)
                reason_stat(REASON_MEDIA_BUDGET, size)
                raw.append((rel, size, FileDecision(rel, size, False, REASON_MEDIA_BUDGET)))
    # A verified reference to a generic opaque asset is priority-1 audit
    # material. This reuses the existing soft-budget ladder: referenced .bin
    # survives, while an unrelated bulky .bin remains priority 3 and can still
    # be trimmed normally. The reference scanner has already enforced source
    # containment and physical existence (or membership in the discovered
    # inventory), so this is not a filename-based mandatory rule.
    for _rel, _size, _decision in raw:
        if _decision.include and _rel.lower() in referenced_files_lower:
            _decision.priority = 1
        elif _decision.include and _rel in manifest_required_files:
            _decision.priority = 1

    # Calculate budget feasibility and budget floor before discretionary trimming
    mandatory_bytes = sum(d[1] for d in raw if d[2].include and d[2].priority == 1)
    discretionary_bytes = sum(d[1] for d in raw if d[2].include and d[2].priority > 1)
    plan.mandatory_bytes = mandatory_bytes
    plan.discretionary_bytes = discretionary_bytes
    plan.budget_floor_bytes = mandatory_bytes
    plan.budget_feasible = plan.budget_bytes == 0 or mandatory_bytes <= plan.budget_bytes

    # ---- soft budget trim: ordinary assets first, prose second, never P1 ---
    # Two ordered passes, largest-first inside each. Priority 1 (code, tests,
    # configs, manifests, schemas, active control plane, explicit always_include)
    # is never offered to the trim at all, so a budget can be missed but source
    # and control plane are never sacrificed.
    # T-155: skipped for a prefix verdict -- the trim could reverse an include
    # that already justified the verdict, and a stale probe packs nothing.
    if not prefix_verdict and plan.budget_bytes > 0 and plan.included_bytes > plan.budget_bytes:
        if plan.budget_feasible:
            # CASE 1: mandatory_bytes < budget_bytes
            # Trim normal discretionary material using tier order (3 then 2) until target met
            for tier in (3, 2):
                if plan.included_bytes <= plan.budget_bytes:
                    break
                trimmable = [d for d in raw if d[2].include and d[2].priority == tier]
                trimmable.sort(key=lambda d: (-d[1], d[0].lower(), d[0]))
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
        else:
            # CASE 2: mandatory_bytes >= budget_bytes (target impossible before optional material)
            # 1. Trim all disposable priority 3 bulk largest-first
            trimmable_p3 = [d for d in raw if d[2].include and d[2].priority == 3]
            trimmable_p3.sort(key=lambda d: (-d[1], d[0].lower(), d[0]))
            for rel, size, decision in trimmable_p3:
                decision.include = False
                decision.reason = REASON_SIZE_LIMIT
                plan.included -= 1
                plan.included_bytes -= size
                plan.excluded += 1
                plan.excluded_bytes += size
                plan.extra_excluded_rel.add(rel)
                reason_stat(REASON_SIZE_LIMIT, size)

            # 2. Bounded policy for ordinary priority 2 prose:
            # Stop pretending the budget can be met once only protected/non-disposable material remains.
            # Allow a bounded discretionary prose allowance; trim excess P2 largest-first.
            p2_included = [d for d in raw if d[2].include and d[2].priority == 2]
            p2_total_bytes = sum(d[1] for d in p2_included)
            p2_allowance = prose_discretionary_allowance(plan.budget_bytes)
            if p2_total_bytes > p2_allowance:
                p2_included.sort(key=lambda d: (-d[1], d[0].lower(), d[0]))
                for rel, size, decision in p2_included:
                    if p2_total_bytes <= p2_allowance:
                        break
                    decision.include = False
                    decision.reason = REASON_SIZE_LIMIT
                    plan.included -= 1
                    plan.included_bytes -= size
                    plan.excluded += 1
                    plan.excluded_bytes += size
                    plan.extra_excluded_rel.add(rel)
                    reason_stat(REASON_SIZE_LIMIT, size)
                    p2_total_bytes -= size

    # ---- finish -----------------------------------------------------------
    # T-150: decision keys are the EXACT source-relative POSIX path. A
    # case-sensitive filesystem can hold ``Asset.PNG`` and ``asset.png`` in one
    # directory; a lowercased key collapsed them into one decision and the
    # later traversal entry silently won while the counters stayed consistent.
    plan.decisions = {d[0]: d[2] for d in raw}
    # PERF-003: exact aggregates, bounded evidence -- computed HERE, after the
    # soft-budget trim, because the trim reverses media decisions the sampler
    # already made. Aggregating during sampling reported files as included that
    # the finished plan excludes (measured: 10 of 20 groups claimed
    # included=1/excluded=1 for a group the decisions recorded as 0/2).
    # Keyed by GROUP, because a directory holding two media classes used to
    # write both groups to the same key and keep only the last one's numbers.
    # T-155: skipped for a prefix verdict -- its media groups were never
    # sampled, so they have no decisions to aggregate.
    if not prefix_verdict:
        for (mclass, parent_rel), members in sorted(media_groups.items()):
            included = included_bytes = excluded = excluded_bytes = 0
            included_sample: list[dict[str, object]] = []
            omitted_sample: list[dict[str, object]] = []
            # ``members`` is still in the sampler's deterministic order, so the
            # bounded samples are the first N of a reproducible sequence.
            for rel, size in members:
                if plan.decisions[rel].include:
                    included += 1
                    included_bytes += size
                    if len(included_sample) < MEDIA_SAMPLE_LIMIT:
                        included_sample.append({"rel": rel, "size": size})
                else:
                    excluded += 1
                    excluded_bytes += size
                    if len(omitted_sample) < MEDIA_SAMPLE_LIMIT:
                        omitted_sample.append({"rel": rel, "size": size})
            plan.media_inventory[f"{parent_rel or '.'}#{mclass}"] = {
                "class": mclass,
                "directory": parent_rel,
                "total": len(members),
                "total_bytes": included_bytes + excluded_bytes,
                "included": included,
                "included_bytes": included_bytes,
                "excluded": excluded,
                "excluded_bytes": excluded_bytes,
                "sample_limit": MEDIA_SAMPLE_LIMIT,
                "included_sample": included_sample,
                "omitted_sample": omitted_sample,
            }
    # CORE-003 (audit/6.md): the accounting is checked HERE, at the traversal
    # boundary, before anything can serialize it. A mismatch is recorded rather
    # than raised -- losing a finished archive over a metadata defect would be a
    # worse answer -- and ``semantics_for_plan`` refuses the snapshot claim for a
    # plan that cannot account for itself.
    plan.accounting_error = plan_accounting_error(plan)
    plan.archive_semantics = semantics_for_plan(plan)
    if newer_than_mtime is not None and not plan.newer_found:
        # Freshness fusion: only INCLUDED files count. Excluded, sampled-out
        # and budget-trimmed files must never invalidate an archive they were
        # not part of. One stat per file already happened in the walk above.
        # T-155: a verdict the early exit already settled stands -- the prefix
        # that remains is unsorted raw and must not be able to flip it.
        for _rel, _size, decision in raw:
            if decision.include and mtimes.get(_rel, 0) > newer_than_mtime:
                plan.newer_found = True
                break
    # PERF-003: a bounded selection, not a full sort. The key is the same
    # ``(-size, lowercased path)`` the sort used, and ``nsmallest`` is documented
    # to equal ``sorted(iterable, key=key)[:n]`` -- so the reported top-N is
    # byte-identical while the O(n log n) sort of every omitted file and the
    # O(n) temporary list it materialized are gone. T-150: the exact path is
    # the final tie-break so case-distinct omissions order deterministically.
    plan.largest_omitted = heapq.nsmallest(
        LARGEST_OMITTED_LIMIT,
        ((d[0], d[1]) for d in raw if not d[2].include and d[1] > 0),
        key=lambda item: (-item[1], item[0].lower(), item[0]),
    )
    by_priority = PriorityBytes()
    dir_bytes: dict[str, int] = {}
    for rel, size, decision in raw:
        if decision.include:
            by_priority[decision.priority] = by_priority.get(decision.priority, 0) + size
            parent_dir = rel.rpartition("/")[0] or "."
            dir_bytes[parent_dir] = dir_bytes.get(parent_dir, 0) + size

    plan.included_bytes_by_priority = by_priority

    plan.largest_included = [
        {"rel": d[0], "size": d[1], "priority": d[2].priority}
        for d in heapq.nsmallest(
            LARGEST_INCLUDED_LIMIT,
            (d for d in raw if d[2].include and d[1] >= 0),
            key=lambda item: (-item[1], item[0].lower(), item[0]),
        )
    ]

    plan.largest_included_directories = [
        {"rel": dir_rel, "bytes": total_b}
        for dir_rel, total_b in heapq.nsmallest(
            LARGEST_INCLUDED_DIRECTORIES_LIMIT,
            dir_bytes.items(),
            key=lambda item: (-item[1], item[0].lower(), item[0]),
        )
    ]
    return plan


def semantics_for_plan(plan: "FidelityPlan") -> str:
    """The semantics an archive built from ``plan`` actually earns.

    CORE-004 (audit/6.md): ``archive_semantics_for`` reads the profile NAME, so
    a FULL run that lost material still declared ``full_snapshot``. A snapshot
    claim now has to survive the plan: any fidelity-policy omission
    (``LOSSY_POLICY_REASONS``), an incompletely enumerated tree, or a file the
    walk could not read downgrades it to an audit representation. Configured and
    secret exclusions do not -- the operator asked for those.
    """
    declared = archive_semantics_for(plan.profile)
    if declared != "full_snapshot":
        return declared
    if plan.walk_incomplete or plan.failed:
        return "audit_representation"
    # CORE-003: a plan that cannot account for its own files has not proven it
    # holds everything, whatever the counters happen to say.
    if plan.accounting_error:
        return "audit_representation"
    if any(plan.reason_stats.get(reason, {}).get("count", 0) for reason in LOSSY_POLICY_REASONS):
        return "audit_representation"
    return "full_snapshot"


def build_plan_from_config(
    source: Path,
    packing,
    excludes: set[str],
    *,
    newer_than_mtime: Optional[float] = None,
    census_pruned: bool = True,
    cancel_event=None,
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
        census_pruned=census_pruned,
        cancel_event=cancel_event,
    )


def exclude_reason_summary(plan: FidelityPlan) -> dict[str, dict[str, int]]:
    """Stable, JSON-friendly reason stats (only categories with hits)."""
    return {k: dict(v) for k, v in sorted(plan.reason_stats.items())}


def failure_summary(plan: FidelityPlan) -> dict[str, int]:
    """Stable, JSON-friendly failure categories (only those with hits)."""
    return dict(sorted(plan.failure_stats.items()))
