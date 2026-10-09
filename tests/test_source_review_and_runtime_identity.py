"""T-274: an archive names its producer and proves its own source is in it.

The defect this suite pins: a normal ZAICODE pack shipped a nested same-origin
component as ``included=false, reason=""`` -- an archive that opened cleanly,
verified cleanly, reported success, and carried 6,287 ``.saipen`` members while
the application source it existed to deliver was absent. Two independent causes
were proven and both are covered here:

* the resident process had LOADED packing code that no longer matched the
  checkout (so a fixed inventory implementation was not the one running), and
* nothing in the archive asserted that the source it claims to carry is
  actually inside it.

Every case below runs through a REAL pack entry point -- ``pack_single``, the
``PackingService`` path the GUI uses, the silent PACK ALL loop, the AUTO PACK
ALL tick and the row-click dispatch -- never through a helper written for the
test. Nothing is keyed to a particular project or component name: recognition is
by Git origin identity.
"""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from audapack import packing, runtime_identity
from audapack import source_inventory as si
from audapack.config import AppConfig, PackingConfig
from audapack.models import PackResult, Project
from audapack.packing import pack_single
from audapack.services.packing_service import PackingService

PRODUCT_ORIGIN = "https://github.com/acme/product"
FOREIGN_ORIGIN = "https://github.com/other/tool"


# --------------------------------------------------------------------- fixture

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


def _write(path: Path, rel: str, text: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def product(tmp_path) -> Path:
    """A first-party checkout: root source plus one nested same-origin component."""
    root = _init(tmp_path / "product", PRODUCT_ORIGIN)
    _write(root, "src/app.ts", "export const app = 1;\n")
    _write(root, "package.json", '{"name": "product"}\n')
    _commit(root)
    return root


def _component(
    root: Path,
    name: str = "zcode",
    origin: str = PRODUCT_ORIGIN,
    files: dict[str, str] | None = None,
    bulk: bool = True,
) -> Path:
    """A nested component with real source and, by default, real bulk on disk."""
    child = _init(root / name, origin)
    for rel, text in (files or {
        "src/main.ts": "export const main = 1;\n",
        "src/service/audit.ts": "export const audit = 1;\n",
        "package.json": '{"name": "zcode"}\n',
        "package-lock.json": '{"lockfileVersion": 3}\n',
    }).items():
        _write(child, rel, text)
    if bulk:
        for rel in (
            "node_modules/left-pad/index.js",
            "dist/bundle.js",
            "build/out.map",
            ".next/cache/data.json",
            "coverage/report.html",
        ):
            _write(child, rel, rel)
    _commit(child)
    return child


def _pack(root: Path, tmp_path: Path, logs: list[str], **overrides) -> PackResult:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    packing_kwargs = overrides.pop("packing_kwargs", {})
    return pack_single(
        source_path=root,
        output_dir=out,
        archive_stem="product",
        excludes=set(),
        delete_old=False,
        include_timestamp=False,
        log_callback=logs.append,
        packing=PackingConfig(output_dir=str(out), **packing_kwargs),
        manifest_meta={"project_name": "product", "extra_meta": {}},
        **overrides,
    )


def _manifests(zip_path: Path) -> tuple[dict, dict, list[str]]:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        legacy = json.loads(zf.read(packing.MANIFEST_FILENAME).decode("utf-8"))
        inventory = json.loads(zf.read(packing.INVENTORY_MANIFEST_PATH).decode("utf-8"))
    return legacy, inventory, names


def _config(root: Path, tmp_path: Path, project_id: str = "product") -> AppConfig:
    config = AppConfig(
        packing=PackingConfig(
            output_dir=str(tmp_path / "out"), delete_old=False, include_timestamp=False
        )
    )
    config.projects = [
        Project(
            id=project_id,
            display_name="product",
            source_path=str(root),
            archive_name="product",
            priority_group="MAIN0",
            slot=1,
        )
    ]
    return config


# ------------------------------------------------- 1/2. the component is source

def test_a_component_hidden_by_the_outer_gitignore_is_still_packed(product, tmp_path):
    """Case 1: `/zcode/` in the ROOT .gitignore hides it from the outer `ls-files`."""
    _component(product)
    _write(product, ".gitignore", "/zcode/\n")
    _commit(product, "ignore the component")
    assert _git("ls-files", "--others", "--exclude-standard", cwd=product) == ""

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    _legacy, inventory, names = _manifests(result.output_path)

    component = inventory["nested_git_components"][0]
    assert component["included"] is True
    assert component["reason"] == "nested_git_component"
    assert component["files"] > 0
    assert "zcode/src/main.ts" in names
    assert inventory["source_review_ready"] is True


def test_a_visible_component_is_packed_exactly_once(product, tmp_path):
    """Case 2: normal outer enumeration must not double-write the same member."""
    _component(product)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    _legacy, inventory, names = _manifests(result.output_path)

    members = [name for name in names if name.startswith("zcode/")]
    assert len(members) == len(set(members)), "a member was written twice"
    assert members == sorted(members), "component members are not in deterministic order"
    assert inventory["counts"]["included"] == sum(
        1 for name in names if name not in (packing.MANIFEST_FILENAME, packing.INVENTORY_MANIFEST_PATH)
    )


# ------------------------------------------------ 3. foreign keeps its omission

def test_a_foreign_component_is_a_named_omission(product, tmp_path):
    """Case 3: a different origin is reported, never silently absorbed or dropped."""
    _component(product, name="vendor_tool", origin=FOREIGN_ORIGIN)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    _legacy, inventory, names = _manifests(result.output_path)

    component = inventory["nested_git_components"][0]
    assert component["included"] is False
    assert component["reason"], "an omission without a reason is a silent hole"
    assert not [name for name in names if name.startswith("vendor_tool/")]


# -------------------------------------------------- 4/5. inventory failures close

def test_a_component_the_git_layer_cannot_describe_fails_the_pack(product, tmp_path, monkeypatch):
    """Case 4: an un-inventoriable component refuses the pack, with a name."""
    child = _component(product)
    real_run = si._run_git

    def _boom(args, cwd=None, **kwargs):
        cwd = Path(cwd or ".").resolve()
        if cwd == child.resolve() or child.resolve() in cwd.parents:
            raise si.SourceInventoryError(
                si.CODE_NESTED_COMPONENT_INVENTORY, "simulated failure", rel=cwd.name
            )
        return real_run(args, cwd=cwd)

    monkeypatch.setattr(si, "_run_git", _boom)
    logs: list[str] = []
    result = _pack(product, tmp_path, logs)

    assert result.success is False
    assert result.status == packing.PACK_STATUS_FAILED_INVENTORY
    assert si.CODE_NESTED_COMPONENT_INVENTORY in result.error_message
    assert not list((tmp_path / "out").glob("*.zip")), "no archive may exist after a refusal"


def test_an_included_component_without_source_is_a_refused_archive(product, tmp_path, monkeypatch):
    """Case 5/13: `included=true` with nothing written under it cannot ship."""
    _component(product)
    monkeypatch.setattr(si, "build_nested_component_entries", lambda *a, **k: {})

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)

    assert result.success is False
    assert result.error_code == si.CODE_SOURCE_REVIEW_INCOMPLETE
    assert "carries no source" in result.error_message
    assert not list((tmp_path / "out").glob("*.zip"))


def test_the_reconciliation_names_the_same_failures(product):
    """The verdict is data: included-without-source, and reasonless omission."""
    _component(product)
    inventory, _plan = si.build_pack_inventory(product, set())
    ready = si.source_review_reconciliation(inventory)
    assert ready["source_review_ready"] is True

    inventory.nested_git_components[0].included = False
    inventory.nested_git_components[0].reason = ""
    verdict = si.source_review_reconciliation(inventory)
    assert verdict["source_review_ready"] is False
    assert any("no recorded reason" in issue for issue in verdict["issues"])

    inventory.nested_git_components[0].included = True
    verdict = si.source_review_reconciliation(inventory, archive_members=[])
    assert verdict["source_review_ready"] is False
    assert any("no member under" in issue for issue in verdict["issues"])


# ---------------------------------------- 6/7/8/9. every normal entry point agrees

def test_the_service_path_packs_a_ready_archive(product, tmp_path):
    """Case 6: the path the GUI packs through, end to end."""
    _component(product)
    service = PackingService(_config(product, tmp_path), base_dir=tmp_path)
    result = service.pack_project("product")

    assert result.success, result.error_message
    assert result.source_review_ready is True
    assert result.nested_components_included == 1
    _legacy, inventory, names = _manifests(result.output_path)
    assert "zcode/src/main.ts" in names
    assert inventory["source_review_ready"] is True
    assert inventory["producer"]["packing_implementation_sha256"] == (
        runtime_identity.LOADED_PACKING_DIGEST
    )


def test_auto_pack_all_tick_dispatches_through_the_packing_service(product, tmp_path, qapp, monkeypatch):
    """Case 7: the periodic AUTO PACK ALL occurrence reaches `pack_project`."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    config = _config(product, tmp_path)
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    calls: list[str] = []

    class _Recorder:
        def __init__(self, config):
            self.config = config

        def pack_project(self, project_id, **kwargs):
            calls.append(project_id)
            return PackResult(project_id=project_id, name=project_id, source_path=str(product), success=True)

    monkeypatch.setattr(window, "_packing", _Recorder(config))
    try:
        window._on_auto_pack_tick()
        deadline = time.time() + 10
        while not calls and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
    finally:
        window.close()

    assert calls == ["product"]


def test_a_row_click_dispatches_through_the_packing_service(product, tmp_path, qapp, monkeypatch):
    """Case 8: auto-pack-on-click reaches `pack_project` for that project."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    config = _config(product, tmp_path)
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    calls: list[str] = []

    class _Recorder:
        def __init__(self, config):
            self.config = config

        def pack_project(self, project_id, **kwargs):
            calls.append(project_id)
            return PackResult(project_id=project_id, name=project_id, source_path=str(product), success=True)

    monkeypatch.setattr(window, "_packing", _Recorder(config))
    try:
        window._on_project_row_clicked("MAIN0", 1)
        deadline = time.time() + 10
        while not calls and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
    finally:
        window.close()

    assert calls == ["product"]


def test_the_silent_pack_all_path_writes_a_ready_archive(product, tmp_path, monkeypatch):
    """Case 9: `PACK_ALL_SILENT.vbs` -> `run_silent_pack_all`, the real loop."""
    from audapack import app as app_module

    _component(product)
    config = _config(product, tmp_path)
    monkeypatch.setattr(app_module, "load_config", lambda *a, **k: config)

    class _Registry:
        def __init__(self, *_a, **_k):
            self.projects = config.projects

    monkeypatch.setattr(app_module, "ProjectRegistry", _Registry)

    assert app_module.run_silent_pack_all() == 0

    archives = list((tmp_path / "out").glob("product*.zip"))
    assert len(archives) == 1, archives
    _legacy, inventory, names = _manifests(archives[0])
    assert inventory["source_review_ready"] is True
    assert "zcode/src/main.ts" in names


# ------------------------------------------------------ 10. the stale runtime

def test_changed_packing_code_moves_the_implementation_digest(tmp_path):
    """Case 10 (positive): the digest tracks the bytes on disk, not the clock."""
    fake = tmp_path / "audapack"
    (fake / "services").mkdir(parents=True)
    for rel in runtime_identity.PACKING_SUBSYSTEM_MODULES:
        (fake / rel).write_text(f"# {rel}\n", encoding="utf-8")

    before = runtime_identity.packing_implementation_digest(fake)
    assert before == runtime_identity.packing_implementation_digest(fake)
    (fake / "packing.py").write_text("# changed\n", encoding="utf-8")
    assert runtime_identity.packing_implementation_digest(fake) != before


def test_a_stale_resident_runtime_refuses_before_writing_an_archive(product, tmp_path, monkeypatch):
    """Case 10: disk code newer than the loaded code -> named refusal, no ZIP."""
    monkeypatch.setattr(
        runtime_identity, "stale_packing_subsystem", lambda: "f" * 64
    )
    logs: list[str] = []
    result = _pack(product, tmp_path, logs)

    assert result.success is False
    assert result.status == packing.PACK_STATUS_FAILED_STALE_RUNTIME
    assert result.error_code == runtime_identity.CODE_STALE_PACKER_RUNTIME
    assert "Restart AUDAPACK" in result.error_message
    assert "no archive was produced" in result.error_message
    assert not list((tmp_path / "out").glob("*.zip"))
    assert not list((tmp_path / "out").glob("*.part*"))


def test_a_matching_runtime_still_packs(product, tmp_path):
    """The gate is a comparison, not a blanket refusal."""
    assert runtime_identity.stale_packing_subsystem() is None
    logs: list[str] = []
    assert _pack(product, tmp_path, logs).success is True

def test_a_hot_reuse_cannot_come_from_a_stale_process(product, tmp_path, monkeypatch):
    """Case 10 (reuse leg): the bridge reaches the packer through `ensure_fresh_archive`.

    A hot proof answers that call WITHOUT packing, so a resident whose packing
    code has moved on would have served an archive its own superseded walk
    judged fresh -- the same untrustworthy success the pack gate refuses. The
    refusal must come first: the reuse lookup is never consulted.
    """
    from types import SimpleNamespace

    from audapack import hot_freshness

    _component(product)
    stale_archive = tmp_path / "out" / "product.zip"
    stale_archive.parent.mkdir(parents=True, exist_ok=True)
    stale_archive.write_bytes(b"PK\x03\x04 archive built by superseded code")
    consulted: list[str] = []

    def _would_reuse(source_path, fingerprint, existing):
        consulted.append(str(existing))
        return SimpleNamespace(fidelity_profile="standard", archive_semantics="audit_representation")

    monkeypatch.setattr(hot_freshness, "lookup", _would_reuse)
    monkeypatch.setattr(runtime_identity, "stale_packing_subsystem", lambda: "f" * 64)

    service = PackingService(_config(product, tmp_path), base_dir=tmp_path)
    result = service.ensure_fresh_archive("product")

    assert consulted == [], "the reuse proof was consulted by a stale process"
    assert result.success is False
    assert result.reused is False
    assert result.output_path is None
    assert result.status == packing.PACK_STATUS_FAILED_STALE_RUNTIME
    assert result.error_code == runtime_identity.CODE_STALE_PACKER_RUNTIME
    assert stale_archive.read_bytes() == b"PK\x03\x04 archive built by superseded code"

def test_a_stale_process_refuses_a_direct_pack_through_the_service(product, tmp_path, monkeypatch):
    """Case 6/10: the UI PACK and autopack entry points refuse the same way."""
    monkeypatch.setattr(runtime_identity, "stale_packing_subsystem", lambda: "f" * 64)
    service = PackingService(_config(product, tmp_path), base_dir=tmp_path)
    result = service.pack_project("product")

    assert result.success is False
    assert result.status == packing.PACK_STATUS_FAILED_STALE_RUNTIME
    assert result.error_code == runtime_identity.CODE_STALE_PACKER_RUNTIME
    assert not list((tmp_path / "out").glob("*.zip"))


# ------------------------------- 11/12. what the archive carries, and what it must not

def test_the_archive_carries_the_component_source_and_no_reproducible_bulk(product, tmp_path):
    """Cases 11 and 12: source in, dependency/build/Git trees out."""
    _component(product)
    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    _legacy, inventory, names = _manifests(result.output_path)

    for expected in (
        "zcode/src/main.ts",
        "zcode/src/service/audit.ts",
        "zcode/package.json",
        "zcode/package-lock.json",
    ):
        assert expected in names, expected

    forbidden = (
        "zcode/.git",
        "zcode/node_modules",
        "zcode/dist",
        "zcode/build",
        "zcode/coverage",
        "zcode/.next",
    )
    for name in names:
        for prefix in forbidden:
            assert not name.startswith(prefix + "/"), name
    assert inventory["source_review"]["components"][0]["archive_members"] > 0


def test_a_component_member_inside_a_bulk_tree_cannot_reach_the_archive(product, tmp_path):
    """Case 12: the postcondition catches bulk that got in, whatever let it in."""
    _component(product)
    inventory, _plan = si.build_pack_inventory(product, set())
    verdict = si.source_review_reconciliation(
        inventory, archive_members=["zcode/node_modules/x/index.js", "zcode/src/main.ts"]
    )
    assert verdict["source_review_ready"] is False
    assert any("reproducible bulk entered" in issue for issue in verdict["issues"])


# ------------------------------------------------- 14/15. size contract, source first

def test_size_pressure_keeps_mandatory_source_and_warns(product, tmp_path, monkeypatch):
    """Case 14: the working threshold only ever names the split; it never trims."""
    _component(product)
    monkeypatch.setattr(packing, "ARCHIVE_WARNING_BYTES", 1)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    assert result.mandatory_bytes > 0
    assert result.discretionary_bytes >= 0
    assert any("above the" in line and "working threshold" in line for line in logs)

    legacy, inventory, names = _manifests(result.output_path)
    assert "zcode/src/main.ts" in names
    assert legacy["archive_warning_bytes"] == 1
    assert legacy["mandatory_bytes"] == result.mandatory_bytes
    assert inventory["source_review_ready"] is True


def test_mandatory_source_above_the_ceiling_is_reported_not_dropped(product, tmp_path, monkeypatch):
    """Case 15: BUDGET_INFEASIBLE names the floor and never removes source."""
    _component(product)
    monkeypatch.setattr(packing, "ARCHIVE_HARD_CEILING_BYTES", 1)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, "an oversized mandatory floor is reported, not fatal"

    legacy, inventory, names = _manifests(result.output_path)
    assert legacy["budget_feasible"] is False
    assert legacy["budget_infeasible_code"] == packing.CODE_BUDGET_INFEASIBLE
    assert legacy["budget_floor_bytes"] == legacy["mandatory_bytes"] > 0
    assert legacy["largest_mandatory"], "the contributors must be nameable"
    assert "zcode/src/main.ts" in names, "source was dropped to fit"
    assert inventory["source_review_ready"] is True
    assert any("BUDGET_INFEASIBLE" in line for line in logs)


def test_complete_source_authorizes_large_archive_above_ceiling(product, tmp_path, monkeypatch):
    """T-290: complete_source sets large_archive_authorized=True and budget_feasible=True."""
    _component(product)
    monkeypatch.setattr(packing, "ARCHIVE_HARD_CEILING_BYTES", 1)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs, project_archive_scope="complete_source")
    assert result.success, "large archive under complete_source is authorized"

    legacy, inventory, names = _manifests(result.output_path)
    assert legacy["large_archive_authorized"] is True
    assert legacy["budget_feasible"] is True
    assert not legacy.get("budget_infeasible_code")
    assert legacy.get("archive_scope") == "complete_source"
    assert inventory.get("archive_scope") == "complete_source"
    assert inventory.get("large_archive_authorized") is True
    assert "zcode/src/main.ts" in names
    assert inventory["source_review_ready"] is True
    assert not any("BUDGET_INFEASIBLE" in line for line in logs)


# ------------------------------------------------------------ 16. who wrote this

def test_both_manifests_record_the_producing_runtime(product, tmp_path):
    """Case 16: producer identity, no private machine paths in the block."""
    _component(product)
    logs: list[str] = []
    result = _pack(product, tmp_path, logs)
    assert result.success, result.error_message
    legacy, inventory, _names = _manifests(result.output_path)

    for block in (legacy["producer"], inventory["producer"]):
        assert block["audapack_version"], "a version must be recorded"
        assert block["packing_implementation_sha256"] == runtime_identity.LOADED_PACKING_DIGEST
        assert block["source_revision"]["kind"] in ("git", "fingerprint")
        assert block["process_start_utc"].endswith("Z")
        assert block["pack_started_utc"].endswith("Z")
        assert block["runtime_entrypoint"]
        assert block["runtime_mode"]
        assert block["inventory_schema_version"] == packing.INVENTORY_MANIFEST_SCHEMA_VERSION

    # The packing policy is identified, not merely timestamped.
    assert inventory["producer"]["packing_policy_fingerprint"] == legacy["policy_fingerprint"]

    # ... and nothing machine-private leaks into the block: no absolute path,
    # no user name, no drive letter.
    text = json.dumps(inventory["producer"], ensure_ascii=False)
    assert str(tmp_path) not in text
    assert ":\\" not in text and ":/" not in text


def test_a_shipped_copy_without_git_still_writes_a_producer(tmp_path, monkeypatch):
    """Case 16: no repository -> a fingerprint, never an empty block."""
    fake = tmp_path / "not_a_repo"
    fake.mkdir()
    _write(fake, "app.py", "print('hi')\n")

    monkeypatch.setattr(runtime_identity, "_package_root", lambda: tmp_path / "missing_pkg")
    revision = runtime_identity.source_revision()
    assert revision["kind"] == "fingerprint"
    assert revision["packing_implementation_sha256"]


# ------------------------------------------------- 19. the reproduced project shape

SAIPEN_CONTRACT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "saipen" / "MANIFEST.v2.json"
)


def test_a_zaicode_shaped_project_ships_source_and_omits_undeclared_history(tmp_path):
    """Validation 8: the reproduced product shape, packed through the real writer.

    A root repository carrying SAIPEN memory (declared control plane plus a large
    pile of undeclared history) with one nested same-origin application
    worktree. STANDARD must deliver the application source, keep the declared
    control plane, and represent undeclared history as a named omission rather
    than as bytes -- the exact inversion the reproduced ZAICODE archive showed.
    """
    import shutil

    root = _init(tmp_path / "zaicode", PRODUCT_ORIGIN)
    _write(root, "src/index.ts", "export const index = 1;\n")
    _write(root, "package.json", '{"name": "zaicode"}\n')

    # Declared control plane: the SAIPEN contract names these mandatory. The
    # contract is written LAST so the mandatory evidence is not newer than it
    # (the AUDIT_MANIFEST staleness gate, which refuses a stale hand-written
    # contract exactly as the live protocol does).
    (root / ".saipen").mkdir(parents=True, exist_ok=True)
    for rel in ("STATE.md", "BOARD.md", "LOG.md", "IDENTITY.md"):
        _write(root, f".saipen/{rel}", f"# {rel}\n")
    time.sleep(0.01)
    shutil.copyfile(SAIPEN_CONTRACT_FIXTURE, root / ".saipen" / "MANIFEST.json")
    _write(root, ".saipen/intake/active/SRC-900.md", "# brief\n")

    # Undeclared history: real bytes, never named by the contract.
    scratch = 0
    for index in range(24):
        payload = bytes(256 * 1024)
        (root / ".saipen" / f"scratch_{index:02d}.bin").write_bytes(payload)
        scratch += len(payload)
    _commit(root)

    _component(root)  # the nested same-origin application worktree
    _write(root, ".gitignore", "/zcode/\n")
    _commit(root, "ignore the nested component")

    logs: list[str] = []
    result = _pack(root, tmp_path, logs, packing_kwargs={"fidelity_profile": "standard"})
    assert result.success, result.error_message
    _legacy, inventory, names = _manifests(result.output_path)

    # The application source the archive exists to deliver is really inside it.
    assert "zcode/src/main.ts" in names
    assert "zcode/src/service/audit.ts" in names
    assert "src/index.ts" in names
    assert inventory["nested_git_components"][0]["included"] is True
    assert inventory["nested_git_components"][0]["files"] > 0
    assert inventory["source_review_ready"] is True
    assert inventory["source_review"]["components"][0]["archive_members"] > 0

    # Declared state survives; undeclared history is an omission, not payload.
    assert ".saipen/STATE.md" in names
    assert not [name for name in names if name.startswith(".saipen/scratch_")], (
        "undeclared history was shipped as bytes"
    )
    assert inventory["counts"]["excluded"] >= 24

    # Storage: the archive is source-sized, not history-sized.
    assert result.archive_bytes < scratch, "the archive is dominated by history"


def test_a_reasonless_component_omission_cannot_produce_a_zip(product, tmp_path, monkeypatch):
    """Validation 9: `included == false` + `reason == ""` never yields a success."""
    _component(product)
    real_build = packing.build_pack_inventory

    def _mutinied(source, excludes, *args, **kwargs):
        inventory, plan = real_build(source, excludes, *args, **kwargs)
        inventory.nested_git_components[0].included = False
        inventory.nested_git_components[0].reason = ""
        return inventory, plan

    monkeypatch.setattr(packing, "build_pack_inventory", _mutinied)

    logs: list[str] = []
    result = _pack(product, tmp_path, logs)

    assert result.success is False
    assert result.error_code == si.CODE_SOURCE_REVIEW_INCOMPLETE
    assert not list((tmp_path / "out").glob("*.zip")), "an invalid archive was shipped"


# ------------------------------------------------- 17/18. the operator sees the truth

def test_the_pack_completion_line_reports_what_the_archive_carries():
    """The post-pack surface says size, source, components, omissions, readiness."""
    from audapack.ui_qt.main_window import pack_success_message

    result = PackResult(
        project_id="product",
        name="product",
        source_path=".",
        success=True,
        archive_bytes=4 * 1024 * 1024,
        files_included=321,
        nested_components_included=2,
        nested_component_files=90,
        excluded_bytes=1024 * 1024,
        source_review_ready=True,
    )
    line = pack_success_message("product", result, "product.zip")
    assert "product.zip" in line
    assert "4.0 MB" in line
    assert "321 source files" in line
    assert "2 nested component(s), 90 files" in line
    assert "1.0 MB omitted" in line
    assert "source ready" in line


def test_a_pack_without_its_source_is_never_a_green_tick(qapp, tmp_path):
    """The UI half of case 13: `SOURCE MISSING` is visible, not silently green."""
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import PACK_SOURCE_MISSING, MainWindow, pack_success_message

    config = _config(tmp_path, tmp_path)
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    try:
        result = PackResult(
            project_id="product",
            name="product",
            source_path=str(tmp_path),
            success=True,
            archive_bytes=1024,
            files_included=0,
            source_review_ready=False,
        )
        from audapack.ui_qt.pack_batch import PackJobResult

        window._apply_batch_job_result(
            PackJobResult(project_id="product", success=True, payload=result)
        )
        assert window.model._pack_states["product"][0] == "FAILED"
        assert PACK_SOURCE_MISSING in window.model._pack_states["product"][1]
        assert PACK_SOURCE_MISSING in window.statusBar().currentMessage()
        assert PACK_SOURCE_MISSING in pack_success_message("product", result, "product.zip")
    finally:
        window.close()

