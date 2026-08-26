"""Repository hygiene + secret sentinel tests (Wave K)."""

import pathlib
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent


def _repo_relative(p: pathlib.Path) -> str:
    return p.relative_to(REPO).as_posix()


def _scan_root():
    """Yield files under repo root excluding _implement/.saipen/kitchen internals we may inspect."""
    for p in REPO.rglob("*"):
        if p.is_file():
            rel = _repo_relative(p)
            if rel.startswith("_implement/") or rel.startswith(".saipen/kitchen/"):
                continue
            yield p


def test_no_generated_artifacts_in_source_tree():
    # The test runner itself recreates __pycache__/.pytest_cache while running,
    # so those two are verified inside the packed archive instead (the true
    # hygiene gate). Everything else must be absent from the tree.
    forbidden_dirs = {".workbuddy-ai"}
    forbidden_exts = {".pyo", ".pycx"}
    forbidden_names = {"pack_all_audit_silent.log", "_AUDAPACK_MANIFEST.json", "audapack.json.pre-redact"}
    probe_roots = [p for p in REPO.rglob("*") if p.is_dir() and p.name.endswith("_probe_tmproot")]

    for d in probe_roots:
        raise AssertionError(f"probe root remains: {_repo_relative(d)}")

    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        rel = _repo_relative(p)
        if any(part in forbidden_dirs for part in p.parts):
            raise AssertionError(f"cache dir present: {rel}")
        if p.suffix.lower() in forbidden_exts:
            raise AssertionError(f"bytecode present: {rel}")
        if p.name in forbidden_names:
            raise AssertionError(f"forbidden artifact: {rel}")


def test_package_has_no_caches_or_bytecode():
    """Packed archive must never contain generated caches/bytecode (true hygiene gate).

    Content-scan for a planted secret is covered separately by
    tests/test_packing.py::test_secret_content_absent_from_package.
    """
    from audapack.config import DEFAULT_EXCLUDES
    from audapack.packing import pack_single

    out_dir = REPO / ".pytest_tmp"
    out_dir.mkdir(exist_ok=True)
    zip_path = None
    try:
        plant = REPO / "cfg_plant"
        plant.mkdir(exist_ok=True)
        # Recreate the exact artifacts that must never ship.
        (plant / "__pycache__").mkdir(exist_ok=True)
        (plant / "__pycache__" / "x.pyc").write_bytes(b"c")
        (plant / ".pytest_cache").mkdir(exist_ok=True)
        (plant / ".workbuddy-ai").mkdir(exist_ok=True)
        (plant / "nope.pycx").write_text("x", encoding="utf-8")
        (plant / "secret.token").write_text("x", encoding="utf-8")
        (plant / "keep.txt").write_text("y", encoding="utf-8")

        res = pack_single(
            source_path=plant,
            output_dir=out_dir,
            archive_stem="Hygiene",
            excludes=set(DEFAULT_EXCLUDES),
            delete_old=True,
        )
        assert res.success, res.error_message
        zip_path = res.output_path
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            joined = "|".join(names).lower()
            for bad in ("__pycache__", ".pytest_cache", ".workbuddy-ai", ".pycx", ".token", ".pyc"):
                assert bad not in joined, f"forbidden artifact in package: {bad} in {names}"
            assert any(n.endswith("keep.txt") for n in names)
    finally:
        if zip_path and zip_path.exists():
            zip_path.unlink()
        import shutil
        shutil.rmtree(plant, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def test_package_has_exactly_one_manifest():
    """Generated archive must contain exactly one _AUDAPACK_MANIFEST.json even if
    the source tree accidentally carries a physical copy."""
    from audapack.config import DEFAULT_EXCLUDES
    from audapack.packing import MANIFEST_FILENAME, pack_single

    out_dir = REPO / ".pytest_tmp"
    out_dir.mkdir(exist_ok=True)
    zip_path = None
    try:
        plant = REPO / "cfg_plant"
        plant.mkdir(exist_ok=True)
        (plant / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")  # physical stale copy
        (plant / "keep.txt").write_text("y", encoding="utf-8")

        res = pack_single(
            source_path=plant,
            output_dir=out_dir,
            archive_stem="ManifestCheck",
            excludes=set(DEFAULT_EXCLUDES),
            delete_old=True,
            manifest_meta={"project_name": "ManifestCheck"},
        )
        assert res.success, res.error_message
        zip_path = res.output_path
        with zipfile.ZipFile(zip_path, "r") as zf:
            man = [n for n in zf.namelist() if n.endswith(MANIFEST_FILENAME)]
            assert len(man) == 1, f"expected exactly 1 manifest, got {man}"
    finally:
        if zip_path and zip_path.exists():
            zip_path.unlink()
        import shutil
        shutil.rmtree(plant, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def test_no_live_token_in_source():
    import json

    # A source root should never carry a real credential value.
    # scan config.example.json specifically for a non-empty token field.
    example = REPO / "config.example.json"
    if example.exists():
        data = json.loads(example.read_text(encoding="utf-8"))
        assert data.get("bridge", {}).get("token", "") == ""
