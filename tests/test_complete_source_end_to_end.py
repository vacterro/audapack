"""T-290: ONE end-to-end path from persisted Project config to the archive.

The four T-290 defect fixes are pinned at unit level in
``tests/test_nested_git_components.py``. Those tests call the inventory builder
directly, which is exactly the layer that was wrong -- and therefore exactly
the layer that cannot prove the fix reaches a real pack. This file closes the
coverage gap: it drives the REAL facades in the REAL order,

    AppConfig -> ProjectRegistry persistence
              -> PackingService.pack_project / ensure_fresh_archive
              -> pack_single
              -> build_pack_inventory
              -> nested component inventory
              -> both archive manifests
              -> freshness

so a regression that only breaks the wiring between them -- a normalized scope
that never reaches the inventory, a policy that reaches the fingerprint but not
the matcher, a component that is inventoried but never written -- fails here.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from audapack.config import AppConfig, PackingConfig, save_config
from audapack.models import ARCHIVE_SCOPE_AUTO, ARCHIVE_SCOPE_COMPLETE_SOURCE, Project
from audapack.packing import INVENTORY_MANIFEST_PATH, read_archive_manifest
from audapack.services.packing_service import PackingService

PROJECT_ID = "proj"
ROOT_ORIGIN = "https://example.test/acme/product"
#: A DIFFERENT product: only explicit ``complete_source`` authority may pull it
#: in. Under AUTO it stays the named omission it always was.
CHILD_ORIGIN = "https://example.test/other/tooling"


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _init(path: Path, origin: str) -> Path:
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


def _write(path: Path, rel: str, text: str) -> None:
    # Bytes, not text: the assertions below compare archive bytes against the
    # fixture, and ``write_text`` would translate ``\n`` to ``\r\n`` on Windows.
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, AppConfig]:
    """A registered project on disk: root repo + untracked nested child repo."""
    root = tmp_path / "workspace"
    root.mkdir()
    project_dir = _init(root / "product", origin=ROOT_ORIGIN)
    _write(project_dir, "main.py", "print('root')\n")
    _write(project_dir, "src/app.py", "APP = 1\n")
    _commit(project_dir)

    # Deliberately left UNCOMMITTED in the root: a Git worktree of its own is
    # discovered by the child scan, never by a gitlink.
    child = _init(project_dir / "child", origin=CHILD_ORIGIN)
    _write(child, "src/lib.py", "LIB = 1\n")
    _write(child, "config/app.json", '{"child": true}\n')
    _commit(child)

    output_dir = root / "out"
    output_dir.mkdir()
    config = AppConfig(
        packing=PackingConfig(output_dir=str(output_dir), delete_old=True)
    )
    config.projects = [
        Project(
            id=PROJECT_ID,
            display_name="proj",
            source_path=str(project_dir),
            archive_name="proj",
        )
    ]
    # The registry transaction path reloads the LATEST config from disk, so the
    # project has to be registered there before any edit can persist.
    assert save_config(config, base_dir=root), "fixture config could not be saved"
    return root, project_dir, config


def _set_scope(service: PackingService, scope: str) -> None:
    """Persist ``archive_scope`` through the registry, as the UI does."""

    def editor(proj: Project) -> None:
        proj.archive_scope = scope

    assert service.registry.edit_project(PROJECT_ID, editor), "registry edit refused"
    # The transaction syncs the caller's AppConfig in place, so the service and
    # the test observe the committed value -- this IS the persisted state.
    live = service.registry.get_project_by_id(PROJECT_ID)
    assert live is not None and live.archive_scope == scope


def _manifests(archive: Path) -> tuple[dict, dict]:
    legacy = read_archive_manifest(archive)
    assert legacy is not None, "legacy manifest unreadable"
    with zipfile.ZipFile(archive) as zf:
        inventory = json.loads(zf.read(INVENTORY_MANIFEST_PATH).decode("utf-8"))
    return legacy, inventory


def test_complete_source_flows_from_project_config_to_a_fresh_archive(workspace):
    root, _project_dir, config = workspace
    service = PackingService(config, base_dir=root)
    _set_scope(service, ARCHIVE_SCOPE_COMPLETE_SOURCE)

    result = service.pack_project(PROJECT_ID)
    assert result.success, result.error_message
    archive = Path(result.output_path)

    with zipfile.ZipFile(archive) as zf:
        members = set(zf.namelist())
        payload = {name: zf.read(name) for name in members if name.startswith("child/")}

    # The nested component is real source under its REAL path, never flattened.
    assert "child/src/lib.py" in members
    assert "child/config/app.json" in members
    assert payload["child/src/lib.py"] == b"LIB = 1\n"
    assert not any(m.startswith("src/lib.py") for m in members)
    # Reproducible bulk stays out even though the component is included.
    assert not any("/.git/" in m for m in members)

    legacy, inventory = _manifests(archive)

    # Both manifests describe the same membership and the same authorization.
    assert legacy["archive_scope"] == ARCHIVE_SCOPE_COMPLETE_SOURCE
    assert legacy["large_archive_authorized"] is True
    assert inventory["archive_scope"] == ARCHIVE_SCOPE_COMPLETE_SOURCE
    assert inventory["large_archive_authorized"] is True
    assert legacy["policy_fingerprint"] == inventory["policy_fingerprint"]

    # The component reports WHY it was included, not merely that it was.
    components = inventory["nested_git_components"]
    child = next(c for c in components if c["path"] == "child")
    assert child["included"] is True
    assert child["authority"] == "project_complete_source"
    assert child["files"] > 0 and child["bytes"] > 0

    # The archive can be audited: its own source review passed over real bytes.
    assert inventory["source_review_ready"] is True
    assert inventory["source_review"]["included_files"] > 0
    assert inventory["source_review"]["included_bytes"] > 0
    assert legacy["accounting_reconciled"] is True

    # T-290: a complete-source pack is a normal pack for FRESHNESS. The very
    # next unchanged ensure is satisfied by the archive this pack just wrote --
    # neither the nested component nor the larger authorization may silently
    # turn every later request into a repack.
    hot = service.ensure_fresh_archive(PROJECT_ID)
    assert hot.success and hot.reused is True, (
        f"post-pack ensure did not reuse the archive it just wrote "
        f"(reused={hot.reused}, packed={hot.packed})"
    )
    assert Path(hot.output_path) == archive


def test_a_scope_toggle_is_never_satisfied_by_the_other_scope_archive(workspace):
    """The chain is only correct if the policy also governs FRESHNESS.

    An archive built for one scope must never be reused for the other -- under
    AUTO the complete-source archive is policy-stale (it carries files AUTO
    would not have written), and under complete_source the AUTO archive is
    source-incomplete. Superficial correctness -- right manifest keys, wrong
    reuse -- is the failure this leg exists to catch.
    """
    root, _project_dir, config = workspace
    service = PackingService(config, base_dir=root)
    _set_scope(service, ARCHIVE_SCOPE_COMPLETE_SOURCE)

    first = service.pack_project(PROJECT_ID)
    assert first.success, first.error_message

    # Toggle to AUTO through the same persistence path.
    _set_scope(service, ARCHIVE_SCOPE_AUTO)
    auto = service.ensure_fresh_archive(PROJECT_ID)
    assert auto.success, auto.error_message
    assert auto.reused is not True, (
        "the complete-source archive was reused under AUTO; its policy mismatch "
        "was not honored"
    )
    assert auto.packed is True
    with zipfile.ZipFile(Path(auto.output_path)) as zf:
        auto_members = set(zf.namelist())
    assert "child/src/lib.py" not in auto_members, (
        "AUTO carried a foreign-origin nested component"
    )
    auto_legacy, auto_inventory = _manifests(Path(auto.output_path))
    assert auto_legacy["archive_scope"] == ARCHIVE_SCOPE_AUTO
    assert auto_legacy["large_archive_authorized"] is False
    excluded = next(
        c for c in auto_inventory["nested_git_components"] if c["path"] == "child"
    )
    assert excluded["included"] is False
    assert excluded["reason"], "an omitted component must name its reason"

    # ... and back again: the AUTO archive must not satisfy complete_source.
    _set_scope(service, ARCHIVE_SCOPE_COMPLETE_SOURCE)
    complete = service.ensure_fresh_archive(PROJECT_ID)
    assert complete.success, complete.error_message
    assert complete.reused is not True, (
        "the AUTO archive was reused for complete_source"
    )
    assert complete.packed is True
    with zipfile.ZipFile(Path(complete.output_path)) as zf:
        complete_members = set(zf.namelist())
    assert "child/src/lib.py" in complete_members
    complete_legacy, complete_inventory = _manifests(Path(complete.output_path))
    assert complete_legacy["archive_scope"] == ARCHIVE_SCOPE_COMPLETE_SOURCE
    assert complete_inventory["source_review_ready"] is True
