"""A versioned test oracle is source, even when its bytes happen to be an image.

The defect this file pins (found by a downstream portable-audit handoff): a
project declared nine versioned visual-regression baselines under
``tests/baselines/*.png`` and its ``test:visual`` suite passed against all nine,
yet the portable archive carried three. The media sampler ranked baselines as
ordinary unreferenced assets and kept only the profile's sample count, so the
archive could not reproduce the validation it advertised while its manifest
still reported ``source_review_ready``.

The rule under test is structural, never a project or a directory spelling: a
path carrying a test-context directory with an oracle directory BELOW it is
reproducibility input. ``screenshots/actual/**``, ``docs/reference/**`` and a
bare ``reference/**`` keep the classification they always had.

Nothing here special-cases a route name, ``tests/baselines`` or PNG. The route
names below are the downstream report's own acceptance evidence: they exist to
prove the rule covers the reported shape, not to define it.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from audapack.fidelity import (
    PROFILE_STANDARD,
    REASON_MEDIA_BUDGET,
    build_fidelity_plan,
    is_test_oracle_path,
)
from audapack.packing import (
    CODE_SOURCE_REVIEW_INCOMPLETE,
    ArchiveVerifyError,
    pack_single,
    stage_inventory_zip,
)
from audapack.source_inventory import (
    ORIGIN_FILESYSTEM,
    REASON_COMPONENT_OPTIONAL,
    REASON_NESTED_GIT_DIFFERENT_ORIGIN,
    SourceInventory,
    SourceInventoryEntry,
    build_git_inventory,
    source_review_reconciliation,
)

#: The nine baselines the downstream visual suite asserts against. Acceptance
#: evidence only -- the policy never reads these names.
BASELINE_ROUTES = (
    "home", "docs", "spec", "ecosystem", "pricing",
    "debug-components", "debug-themes", "debug-rendering", "home-dracula",
)

BASELINE_RELS = tuple(f"tests/baselines/{name}.png" for name in BASELINE_ROUTES)

#: Generated visual-test output: a run's own artifacts, never the oracle.
GENERATED_RELS = (
    "test-results/home-actual.png",
    "test-results/home-diff.png",
    "playwright-report/screenshot.png",
)

def _png(seed: bytes, pad: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + seed + b"\x00" * pad

#: Big enough that nine baselines exceed a deliberately tiny soft budget, so the
#: budget path is exercised for real rather than skipped.
BASELINE_PAD_BYTES = 200 * 1024

def _write(root: Path, rel: str, payload: bytes) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

def _visual_project(root: Path) -> Path:
    """A project with the reported shape: nine baselines plus run output."""
    for rel in BASELINE_RELS:
        _write(root, rel, _png(rel.encode(), BASELINE_PAD_BYTES))
    for rel in GENERATED_RELS:
        _write(root, rel, _png(b"generated:" + rel.encode()))
    return root

def _included(plan) -> set[str]:
    return {rel for rel, d in plan.decisions.items() if d.include}

def _excluded_reasons(plan) -> dict[str, str]:
    return {rel: d.reason for rel, d in plan.decisions.items() if not d.include}

def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()

def _init(path: Path, origin: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    if origin:
        _git("remote", "add", "origin", origin, cwd=path)
    return path

def _commit(path: Path) -> None:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "c", cwd=path)

# ------------------------------------------------------------------ policy

@pytest.mark.parametrize(
    "rel",
    [
        "tests/baselines/home.png",
        "test/golden/sheet.png",
        "e2e/goldens/checkout.png",
        "playwright/snapshots/a.png",
        "__tests__/__snapshots__/button.snap",
        "visual-regression/baselines/hero.webp",
        "regression-tests/reference/model.json",
        "child/tests/baselines/reference.png",
    ],
)
def test_a_test_context_with_an_oracle_directory_below_it_is_an_oracle(rel):
    assert is_test_oracle_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        # Generated run output, not an oracle.
        "test-results/home-actual.png",
        "playwright-report/screenshot.png",
        "screenshots/actual/home.png",
        "screenshots/diff/home.png",
        # Oracle names with no test context: ordinary prose/media trees.
        "docs/reference/theme.png",
        "reference/theme.png",
        "expected/theme.png",
        "docs/snapshots/a.json",
        # Context without an oracle directory below it.
        "tests/helpers/world.png",
        "expected/tests/theme.png",
    ],
)
def test_a_context_free_oracle_name_keeps_its_ordinary_classification(rel):
    assert not is_test_oracle_path(rel)

# ------------------------------------------------------- classification

def test_standard_profile_keeps_every_baseline_and_never_samples_them(tmp_path):
    """The red proof: this exact shape used to plan 3 of 9 baselines."""
    project = _visual_project(tmp_path / "site")

    plan = build_fidelity_plan(project, set(), profile=PROFILE_STANDARD)

    included = _included(plan)
    missing = [rel for rel in BASELINE_RELS if rel not in included]
    assert missing == [], f"baselines dropped by packing policy: {missing}"
    for rel in BASELINE_RELS:
        assert plan.decisions[rel].priority == 1, (
            f"{rel} must be mandatory audit material, not discretionary bulk"
        )
    assert REASON_MEDIA_BUDGET not in _excluded_reasons(plan).values()


def test_a_sampled_media_cap_cannot_reduce_a_baseline_set(tmp_path):
    """Sample count and byte budget are caps on ordinary media, not on oracles."""
    project = _visual_project(tmp_path / "site")

    plan = build_fidelity_plan(
        project, set(), profile=PROFILE_STANDARD, media_samples=1, max_mb=1
    )

    for rel in BASELINE_RELS:
        assert plan.decisions[rel].include, f"{rel} was trimmed by a media/budget cap"
    # Truthful size reporting, never silent partial loss: the archive is simply
    # larger than the soft target and says so.
    assert plan.budget_met is False


def test_ordinary_media_and_run_output_keep_the_existing_sampling_policy(tmp_path):
    project = tmp_path / "site"
    for i in range(5):
        _write(project, f"public/screenshots/shot-{i}.png", _png(b"shot"))
    for rel in GENERATED_RELS:
        _write(project, rel, _png(b"generated"))

    plan = build_fidelity_plan(project, set(), profile=PROFILE_STANDARD)
    reasons = _excluded_reasons(plan)

    sampled = [
        rel for rel in reasons
        if rel.startswith("public/screenshots/") and reasons[rel] == REASON_MEDIA_BUDGET
    ]
    assert len(sampled) == 2, f"ordinary media sampling changed: {sorted(reasons)}"
    assert not is_test_oracle_path("public/screenshots/shot-0.png")
    # The oracle rule is not a general "keep every image" switch.
    assert "tests/baselines/home.png" not in reasons

# ----------------------------------------------------------- real archive

def test_filesystem_pack_carries_all_nine_baselines_with_matching_bytes(tmp_path):
    project = _visual_project(tmp_path / "site")
    output = tmp_path / "out"
    output.mkdir()

    result = pack_single(
        source_path=project,
        output_dir=output,
        archive_stem="Site",
        excludes=set(),
        delete_old=True,
        include_timestamp=False,
        manifest_meta={"project_name": "Site"},
    )
    assert result.success, result.error_message

    with zipfile.ZipFile(result.output_path) as zf:
        names = set(zf.namelist())
        payloads = {rel: zf.read(rel) for rel in BASELINE_RELS if rel in names}

    assert set(payloads) == set(BASELINE_RELS), (
        f"archive carries {len(payloads)}/{len(BASELINE_RELS)} declared baselines"
    )
    for rel in BASELINE_RELS:
        assert payloads[rel] == (project / rel).read_bytes()


def test_git_pack_carries_every_tracked_baseline(tmp_path):
    repo = _init(tmp_path / "site")
    _visual_project(repo)
    _commit(repo)
    output = tmp_path / "out"
    output.mkdir()

    result = pack_single(
        source_path=repo,
        output_dir=output,
        archive_stem="Site",
        excludes=set(),
        delete_old=True,
        include_timestamp=False,
        manifest_meta={"project_name": "Site"},
    )
    assert result.success, result.error_message

    with zipfile.ZipFile(result.output_path) as zf:
        names = set(zf.namelist())
    missing = [rel for rel in BASELINE_RELS if rel not in names]
    assert missing == [], f"tracked baselines missing from the archive: {missing}"

# ------------------------------------------------------------ fail closed

def _partial_baseline_inventory(project: Path) -> SourceInventory:
    """Nine declared baselines, six of them dropped by profile media policy.

    This is the shape that produced the reported 3/9 archive, reconstructed
    deliberately: the point of the two tests below is that no such inventory may
    ever again accompany ``source_review_ready = true`` or reach a ZIP.
    """
    entries: dict[str, SourceInventoryEntry] = {}
    for index, rel in enumerate(BASELINE_RELS):
        path = project / rel
        kept = index < 3
        st = path.stat()
        entries[rel] = SourceInventoryEntry(
            rel=rel,
            origin=ORIGIN_FILESYSTEM,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            include=kept,
            reason=None if kept else REASON_MEDIA_BUDGET,
            priority=1 if kept else 3,
        )
    return SourceInventory(mode="filesystem", source=project.resolve(), entries=entries)


def test_a_partial_baseline_set_can_never_report_source_review_ready(tmp_path):
    project = _visual_project(tmp_path / "site")
    inventory = _partial_baseline_inventory(project)

    review = source_review_reconciliation(inventory)

    assert review["test_oracle_files_total"] == len(BASELINE_RELS)
    assert review["test_oracle_files_included"] == 3
    assert review["source_review_ready"] is False
    assert any("test-oracle" in issue for issue in review["issues"]), review["issues"]


def test_a_partial_baseline_set_is_refused_before_a_zip_is_committed(tmp_path):
    project = _visual_project(tmp_path / "site")
    inventory = _partial_baseline_inventory(project)
    review = source_review_reconciliation(inventory)

    with pytest.raises(ArchiveVerifyError) as caught:
        stage_inventory_zip(
            project,
            tmp_path / "out" / "Site.zip",
            inventory,
            manifest_meta={"project_name": "Site"},
            source_review=review,
        )
    assert caught.value.code == CODE_SOURCE_REVIEW_INCOMPLETE
    assert not list((tmp_path / "out").glob("*.zip*")), "a refused pack left a staging archive"


def test_a_complete_baseline_set_reports_the_counts_it_proves(tmp_path):
    project = _visual_project(tmp_path / "site")
    plan = build_fidelity_plan(project, set(), profile=PROFILE_STANDARD)
    from audapack.source_inventory import inventory_from_plan

    review = source_review_reconciliation(inventory_from_plan(project, plan))

    assert review["source_review_ready"] is True
    assert review["test_oracle_files_total"] == len(BASELINE_RELS)
    assert review["test_oracle_files_included"] == len(BASELINE_RELS)

# ------------------------------------------------------ nested component

def test_a_nested_component_keeps_its_own_versioned_baseline(tmp_path):
    origin = "https://example.test/acme/product"
    root = _init(tmp_path / "product", origin=origin)
    (root / "main.py").write_text("print('root')\n", encoding="utf-8")
    _commit(root)

    child = _init(root / "child", origin=origin + ".git")
    _write(child, "tests/baselines/reference.png", _png(b"reference"))
    _write(child, "tests/baselines/actual.png", _png(b"actual"))
    _write(child, "src/lib.py", b"LIB = 1\n")
    _commit(child)

    for scope in ("auto", "complete_source"):
        inventory = build_git_inventory(root, set(), project_archive_scope=scope)
        included = {e.rel for e in inventory.included_entries()}
        excluded = {e.rel: e.reason for e in inventory.excluded_entries()}

        assert "child/tests/baselines/reference.png" in included, (
            f"nested baseline lost under archive_scope={scope}"
        )
        assert "child/tests/baselines/actual.png" in included
        assert REASON_COMPONENT_OPTIONAL not in excluded.values(), excluded
        assert inventory.mode == "git"


def test_a_foreign_component_keeps_its_existing_omission(tmp_path):
    """The oracle rule never widens which components are archived at all."""
    root = _init(tmp_path / "product", origin="https://example.test/acme/product")
    (root / "main.py").write_text("print('root')\n", encoding="utf-8")
    _commit(root)

    foreign = _init(root / "tooling", origin="https://example.test/other/tooling")
    _write(foreign, "tests/baselines/reference.png", _png(b"reference"))
    _commit(foreign)

    inventory = build_git_inventory(root, set(), project_archive_scope="auto")

    # Git cannot enumerate inside a nested worktree, so the child stays the ONE
    # named directory omission it always was, and its reason travels on the
    # component record -- the oracle rule changes neither.
    excluded = {e.rel: e.reason for e in inventory.excluded_entries()}
    assert "tooling" in excluded
    assert not any(rel.startswith("tooling/") for rel in excluded)
    component = next(c for c in inventory.nested_git_components if c.rel == "tooling")
    assert component.included is False
    assert component.reason == REASON_NESTED_GIT_DIFFERENT_ORIGIN
