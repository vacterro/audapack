"""A nested same-product Git component is project source, not foreign tooling.

AUDAPACK used to record any untracked directory that was itself a Git
worktree as ONE excluded entry, which meant a product's own first-party
component vanished from the archive. This suite pins the replacement: a bounded
scan of DIRECT children, identity resolved through Git, equal origin means the
component is canonical source and joins the inventory under its real nested
paths -- while a different origin keeps exactly the omission it had before.

Nothing here is keyed to any particular project name. A component is recognised
by its origin, never by a folder or file that happens to be spelled a certain
way, and no README is parsed to guess where the source lives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from audapack import source_inventory as si
from audapack.source_inventory import SourceInventoryError

#: One product's remote, spelled the way the fixture spells it. Recognition is
#: by normalized identity, so a component may spell the same product any way.
PRODUCT_ORIGIN = "https://github.com/acme/product"


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


def _commit(path: Path, message: str = "c") -> None:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)


@pytest.fixture
def root(tmp_path) -> Path:
    project = _init(tmp_path / "product", origin=PRODUCT_ORIGIN)
    (project / "src").mkdir()
    (project / "src" / "app.ts").write_text("root source\n", encoding="utf-8")
    _commit(project)
    return project


def component(root: Path, name: str, origin: str, files: dict[str, bytes]) -> Path:
    child = _init(root / name, origin=origin)
    for rel, payload in files.items():
        target = child / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _commit(child)
    return child


def _rels(inventory) -> set[str]:
    return {entry.rel for entry in inventory.included_entries()}


# --------------------------------------------------------------- discovery


def test_a_same_origin_child_is_canonical_source_not_a_hole(root):
    """The defect: `zcode/` was one excluded directory entry."""
    component(root, "zcode", "https://github.com/acme/product.git",
              {"packages/ui/src/panel.tsx": b"export const Panel = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert "zcode/packages/ui/src/panel.tsx" in _rels(inventory)
    assert "zcode" not in inventory.entries, "no opaque placeholder directory entry"


def test_members_keep_their_real_path_and_are_never_flattened(root):
    """A pack that renames source paths is not the source."""
    component(root, "zcode", "https://github.com/acme/product.git",
              {"packages/ui/src/panel.tsx": b"export const Panel = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    packed = root / "zcode" / "packages" / "ui" / "src" / "panel.tsx"
    assert packed.read_bytes() == b"export const Panel = 1;\n"
    assert "zcode/packages/ui/src/panel.tsx" in _rels(inventory)
    assert not any(rel.startswith("packages/") for rel in _rels(inventory))


def test_the_mode_is_still_git_and_the_head_semantics_do_not_move(root):
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert inventory.mode == "git"
    assert inventory.git_head == _git("rev-parse", "HEAD", cwd=root)
    assert inventory.git_dirty is False, (
        "a clean root with an untracked child is still a clean root"
    )


# ------------------------------------------------------------------ origin


def test_origin_identity_is_normalized_across_spellings(root):
    """git@host:o/r.git, https://host/o/r and a trailing slash are one remote."""
    component(root, "zcode", "git@github.com:acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/panel.ts" in _rels(inventory)
    assert si.normalize_origin_url("git@github.com:acme/product.git") == (
        si.normalize_origin_url("https://github.com/acme/product")
    )
    assert si.normalize_origin_url("https://github.com/acme/product/") == (
        si.normalize_origin_url("https://github.com/acme/product.git")
    )


def test_a_different_origin_keeps_the_existing_safe_omission(root):
    component(root, "vendor-thing", "https://github.com/other/tool.git",
              {"src/tool.ts": b"export const T = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert "vendor-thing" in inventory.entries
    assert inventory.entries["vendor-thing"].include is False
    assert inventory.entries["vendor-thing"].reason == si.REASON_UNTRACKED_GIT_DIR
    assert "vendor-thing/src/tool.ts" not in _rels(inventory)


def test_a_different_origin_omission_is_named_in_the_manifest(root):
    component(root, "vendor-thing", "https://github.com/other/tool.git",
              {"src/tool.ts": b"export const T = 1;\n"})

    inventory = si.build_git_inventory(root, set())
    payload = si.nested_component_manifest_section(inventory)

    assert [item["path"] for item in payload] == ["vendor-thing"]
    assert payload[0]["included"] is False
    assert payload[0]["origin"] == "github.com/other/tool"
    assert payload[0]["reason"] == si.REASON_NESTED_GIT_DIFFERENT_ORIGIN


def test_a_child_without_an_origin_remote_is_never_assumed_canonical(root):
    """No remote means no identity: proving sameness is impossible, so omit."""
    component(root, "mystery", origin="", files={"src/thing.ts": b"export const X = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert "mystery/src/thing.ts" not in _rels(inventory)
    assert inventory.entries["mystery"].include is False


def test_the_manifest_section_is_backward_compatible(root):
    """A root with no components still produces a well-formed, empty section."""
    inventory = si.build_git_inventory(root, set())
    payload = si.nested_component_manifest_section(inventory)
    assert payload == []
    assert json_roundtrip(payload) == []


# ------------------------------------------------------------------- bounds


def test_git_object_stores_are_never_archived(root):
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert not any(rel.startswith("zcode/.git") for rel in _rels(inventory))
    assert not any(".git/objects" in rel for rel in inventory.entries)


@pytest.mark.parametrize("junk", ["node_modules/pkg/index.js", "dist/app.js",
                                  "build/out.js", ".next/server.js"])
def test_dependency_and_build_trees_are_never_archived(root, junk):
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n", junk: b"generated\n"})

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/panel.ts" in _rels(inventory)
    assert f"zcode/{junk}" not in _rels(inventory)


def test_a_tracked_media_corpus_is_not_mandatory_just_because_it_is_committed(root):
    """A 38 MB WAV corpus is upstream payload, not the source we are packing."""
    corpus = b"RIFF" + b"\0" * (38 * 1024 * 1024)
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n", "assets/sfx.wav": corpus})

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/panel.ts" in _rels(inventory)
    assert "zcode/assets/sfx.wav" not in _rels(inventory)
    omitted = inventory.entries["zcode/assets/sfx.wav"]
    assert omitted.include is False
    assert omitted.reason == si.REASON_COMPONENT_OPTIONAL, (
        "the omission is named, never silent"
    )


def test_large_source_files_stay_mandatory(root):
    """The compact tier must not become a byte cap that drops real code."""
    big = b"export const P = '" + b"x" * (9 * 1024 * 1024) + b"';\n"
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/big.ts": big})

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/big.ts" in _rels(inventory)


def test_only_direct_children_are_scanned(root):
    """One level deep: a grandchild is not a component of this product."""
    deep = component(root / "outer", "inner", "https://github.com/acme/product.git",
                     {"src/deep.ts": b"export const D = 1;\n"})
    assert (deep / ".git").exists()

    inventory = si.build_git_inventory(root, set())

    assert "outer" not in inventory.entries or inventory.entries["outer"].include is False
    assert not any(rel.endswith("src/deep.ts") for rel in _rels(inventory))


def test_secret_policy_still_rules_inside_a_component(root):
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n", "id_ed25519": b"PRIVATE KEY\n"})

    with pytest.raises(SourceInventoryError) as caught:
        si.build_git_inventory(root, set())
    assert caught.value.code == si.CODE_TRACKED_HARD_DENY_CONFLICT


def test_an_uninventoriable_same_origin_component_fails_closed(root, monkeypatch):
    """Fail closed by NAME, never a silently missing component."""
    child = component(root, "zcode", "https://github.com/acme/product.git",
                      {"src/panel.ts": b"export const P = 1;\n"})

    real = subprocess.run

    def _boom(args, **kwargs):
        # Only the component's own index is unreadable; the root still answers,
        # so the failure can only be the component-inventory refusal.
        if "ls-files" in args and kwargs.get("cwd") == str(child):
            raise OSError("component index unreadable")
        return real(args, **kwargs)

    monkeypatch.setattr(si.subprocess, "run", _boom)

    with pytest.raises(SourceInventoryError) as caught:
        si.build_git_inventory(root, set())
    assert caught.value.code == si.CODE_NESTED_COMPONENT_INVENTORY
    assert caught.value.code == "NESTED_GIT_COMPONENT_INVENTORY_FAILED"
    assert "zcode" in str(caught.value)


def test_recognition_is_by_origin_not_by_any_particular_name(root, tmp_path):
    """Nothing is keyed to a folder, a filename, or prose in a README."""
    decoy = component(root, "release-assets", "https://github.com/acme/product.git",
                      {"README.md": b"source lives in zcode/\n",
                       "src/tool.py": b"print('real')\n"})

    inventory = si.build_git_inventory(root, set())

    assert "release-assets/src/tool.py" in _rels(inventory)
    assert "zcode" not in inventory.entries
    assert decoy.exists()


def json_roundtrip(payload):
    import json

    return json.loads(json.dumps(payload))


def test_component_metadata_travels_on_the_inventory(root):
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    by_path = {item.rel: item for item in inventory.nested_git_components}
    assert set(by_path) == {"zcode"}
    detail = by_path["zcode"]
    assert detail.included is True
    assert detail.origin == "github.com/acme/product"
    assert detail.head == _git("rev-parse", "HEAD", cwd=root / "zcode")
    assert detail.file_count >= 1

# ------------------------------------------- outer .gitignore hides the child
#
# The T-268 implementation ingested a canonical component ONLY when outer Git
# happened to emit "<child>/" through `ls-files --others --exclude-standard`.
# A product is free to ignore its own component checkout (the canonical ZAICODE
# layout ignores /zcode/), so that coupling silently dropped first-party source
# from a successful pack. These fixtures pin the decoupling: bounded direct-child
# discovery + equal normalized origin decide membership, outer ignore policy
# never does.


def _outer_ignores(root: Path, name: str) -> None:
    """Commit an outer .gitignore that hides the whole component directory."""
    (root / ".gitignore").write_text(f"/{name}/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=root)
    _git("commit", "-q", "-m", f"ignore /{name}/", cwd=root)


def _outer_untracked(root: Path) -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard", cwd=root)
    return [line for line in out.splitlines() if line]


def test_same_origin_child_still_packages_when_outer_git_ignores_it(root):
    """RED fixture: canonical layout, outer hides /zcode/, source must pack."""
    _outer_ignores(root, "zcode")
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    assert not any(
        entry.startswith("zcode") for entry in _outer_untracked(root)
    ), "fixture precondition: outer Git must not report the ignored child"

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/panel.ts" in _rels(inventory)
    record = {item.rel: item for item in inventory.nested_git_components}["zcode"]
    assert record.included is True
    assert record.reason == "nested_git_component"
    payload = si.nested_component_manifest_section(inventory)
    assert payload[0]["included"] is True
    assert payload[0]["reason"] == "nested_git_component"
    assert payload[0]["files"] >= 1


def test_ignored_same_origin_dirty_source_packs_current_worktree_bytes(root):
    """Dirty state must not regress to committed bytes: worktree is the truth."""
    _outer_ignores(root, "zcode")
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})
    edited = (root / "zcode" / "src" / "panel.ts")
    edited.write_bytes(b"export const P = 2; // edited\n")

    inventory = si.build_git_inventory(root, set())

    entry = inventory.entries["zcode/src/panel.ts"]
    assert entry.include is True
    assert entry.size == edited.stat().st_size == len(b"export const P = 2; // edited\n")
    assert edited.read_bytes() == b"export const P = 2; // edited\n"


def test_ignored_same_origin_untracked_child_source_is_included(root):
    """Untracked inside the child is proven by the CHILD's own Git, not the outer."""
    _outer_ignores(root, "zcode")
    child = component(root, "zcode", "https://github.com/acme/product.git",
                      {"src/panel.ts": b"export const P = 1;\n"})
    extra = child / "src" / "late.ts"
    extra.write_bytes(b"export const L = 1;\n")

    inventory = si.build_git_inventory(root, set())

    assert "zcode/src/late.ts" in _rels(inventory)
    assert "src/late.ts" in {
        line for line in _git("ls-files", "--others", "--exclude-standard",
                              cwd=child).splitlines() if line
    }, "fixture precondition: the CHILD's own Git sees the file as untracked"


def test_ignored_same_origin_bulk_stays_excluded(root):
    """Compact tier unchanged: reproducible trees and optional media stay out."""
    _outer_ignores(root, "zcode")
    component(root, "zcode", "https://github.com/acme/product.git", {
        "src/panel.ts": b"export const P = 1;\n",
        "node_modules/pkg/index.js": b"generated\n",
        "dist/app.js": b"generated\n",
        "build/out.js": b"generated\n",
        "assets/sfx.wav": b"RIFF" + b"\0" * 1024,
    })

    inventory = si.build_git_inventory(root, set())
    rels = _rels(inventory)

    assert "zcode/src/panel.ts" in rels
    assert "zcode/node_modules/pkg/index.js" not in rels
    assert "zcode/dist/app.js" not in rels
    assert "zcode/build/out.js" not in rels
    assert "zcode/assets/sfx.wav" not in rels
    assert inventory.entries["zcode/assets/sfx.wav"].reason == (
        si.REASON_COMPONENT_OPTIONAL
    )
    assert not any(".git/objects" in rel for rel in inventory.entries)


def test_ignored_different_origin_component_is_named_and_omitted(root):
    """Foreign stays out even when outer Git hides it too; manifest names why."""
    _outer_ignores(root, "vendor-thing")
    component(root, "vendor-thing", "https://github.com/other/tool.git",
              {"src/tool.ts": b"export const T = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    assert not any(rel.startswith("vendor-thing/") for rel in _rels(inventory))
    record = {item.rel: item
              for item in inventory.nested_git_components}["vendor-thing"]
    assert record.included is False
    assert record.reason == si.REASON_NESTED_GIT_DIFFERENT_ORIGIN
    payload = si.nested_component_manifest_section(inventory)
    assert payload[0]["included"] is False
    assert payload[0]["reason"] == si.REASON_NESTED_GIT_DIFFERENT_ORIGIN


def test_visible_same_origin_component_is_ingested_exactly_once(root, monkeypatch):
    """One dedicated merge phase: no merge-while-iterating plus merge-after."""
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    calls: list[str] = []
    real = si.build_nested_component_entries

    def counting(component_obj, excludes, *, cancel_event=None):
        calls.append(component_obj.rel)
        return real(component_obj, excludes, cancel_event=cancel_event)

    monkeypatch.setattr(si, "build_nested_component_entries", counting)

    inventory = si.build_git_inventory(root, set())

    assert calls == ["zcode"], f"merged {len(calls)} times, expected exactly 1"
    assert "zcode/src/panel.ts" in _rels(inventory)
    assert "zcode" not in inventory.entries, (
        "the directory placeholder is superseded, not recorded twice"
    )


def test_ignored_same_origin_inventory_failure_fails_the_whole_pack(root, monkeypatch):
    """Failure mode B: NESTED_GIT_COMPONENT_INVENTORY_FAILED, never a silent gap."""
    _outer_ignores(root, "zcode")
    child = component(root, "zcode", "https://github.com/acme/product.git",
                      {"src/panel.ts": b"export const P = 1;\n"})

    real = subprocess.run

    def _boom(args, **kwargs):
        if "ls-files" in args and kwargs.get("cwd") == str(child):
            raise OSError("component index unreadable")
        return real(args, **kwargs)

    monkeypatch.setattr(si.subprocess, "run", _boom)

    with pytest.raises(SourceInventoryError) as caught:
        si.build_git_inventory(root, set())
    assert caught.value.code == si.CODE_NESTED_COMPONENT_INVENTORY
    assert caught.value.code == "NESTED_GIT_COMPONENT_INVENTORY_FAILED"


def test_no_successful_inventory_leaves_same_origin_included_false_and_reasonless(root):
    """Manifest truth: included=False + reason='' on a proven component = bug."""
    component(root, "zcode", "https://github.com/acme/product.git",
              {"src/panel.ts": b"export const P = 1;\n"})

    inventory = si.build_git_inventory(root, set())

    for item in inventory.nested_git_components:
        if item.origin and item.origin == si.normalize_origin_url(PRODUCT_ORIGIN):
            assert item.included is True
            assert item.reason, "same-origin component must carry a real reason"
