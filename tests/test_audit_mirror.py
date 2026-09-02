"""A finished audit lands in the audited project too, so `cc` finds it there.

The audit root is one central place -- right for the desktop, wrong for an
agent working inside the repo, which then has to be told where audits live.
With the mirror on, the pipeline closes: start audit -> wait -> audit done ->
agent `cc` in that repo picks it up with no configuration at all.
"""

from __future__ import annotations

from pathlib import Path

from audapack.bridge.storage import mirror_project_audits
from audapack.config import AppConfig, AuditsConfig


def _config(tmp_path: Path, **audits) -> AppConfig:
    return AppConfig(audits=AuditsConfig(root=str(tmp_path / "root"), **audits))


def _audit_dir(tmp_path: Path) -> Path:
    src = tmp_path / "root" / "MAIN0" / "PROJ"
    (src / "_history").mkdir(parents=True)
    (src / "PROJ__00_AUDIT_ALL_3.md").write_text("final", encoding="utf-8")
    (src / "PROJ__01_AUDIT_CORE.md").write_text("core", encoding="utf-8")
    (src / "campaign.json").write_text("{}", encoding="utf-8")
    (src / "_history" / "PROJ__00_AUDIT_ALL_3__old.md").write_text("old", encoding="utf-8")
    return src


def test_the_mirror_is_off_until_it_is_asked_for(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert mirror_project_audits(_config(tmp_path), project, _audit_dir(tmp_path)) == []
    assert not (project / "audit").exists()


def test_a_finished_audit_is_copied_into_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = _config(tmp_path, mirror_into_project=True)

    copied = mirror_project_audits(config, project, _audit_dir(tmp_path))

    names = sorted(path.name for path in copied)
    assert names == ["PROJ__00_AUDIT_ALL_3.md", "PROJ__01_AUDIT_CORE.md", "campaign.json"]
    assert (project / "audit" / "PROJ__00_AUDIT_ALL_3.md").read_text(encoding="utf-8") == "final"


def test_history_is_not_mirrored(tmp_path):
    """The project wants the current audit, not every past run."""
    project = tmp_path / "proj"
    project.mkdir()
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, _audit_dir(tmp_path))
    assert not (project / "audit" / "_history").exists()


def test_the_central_root_still_holds_everything(tmp_path):
    """A copy, never a move: the desktop keeps reading the audit root."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src)
    assert (src / "PROJ__00_AUDIT_ALL_3.md").is_file()
    assert (src / "_history" / "PROJ__00_AUDIT_ALL_3__old.md").is_file()


def test_the_folder_name_is_configurable(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = _config(tmp_path, mirror_into_project=True, mirror_dir_name="_AUDITS")
    mirror_project_audits(config, project, _audit_dir(tmp_path))
    assert (project / "_AUDITS" / "PROJ__00_AUDIT_ALL_3.md").is_file()


def test_a_folder_name_cannot_escape_the_project(tmp_path):
    """A configured name is one path segment, never a way out of the repo."""
    project = tmp_path / "proj"
    project.mkdir()
    config = _config(tmp_path, mirror_into_project=True, mirror_dir_name=r"..\..\elsewhere")
    mirror_project_audits(config, project, _audit_dir(tmp_path))
    assert (project / "audit" / "PROJ__00_AUDIT_ALL_3.md").is_file()
    assert not (tmp_path.parent / "elsewhere").exists()


def test_a_project_with_no_source_path_is_skipped(tmp_path):
    config = _config(tmp_path, mirror_into_project=True)
    assert mirror_project_audits(config, "", _audit_dir(tmp_path)) == []


def test_the_setting_survives_a_config_round_trip(tmp_path, monkeypatch):
    from audapack.config import load_config, save_config

    monkeypatch.setenv("AUDAPACK_STATE_DIR", str(tmp_path / "state"))
    config = load_config()
    config.audits.mirror_into_project = True
    config.audits.mirror_dir_name = "audit"
    save_config(config)

    reloaded = load_config()
    assert reloaded.audits.mirror_into_project is True
    assert reloaded.audits.mirror_dir_name == "audit"


def test_the_mirror_excludes_itself_from_the_repository(tmp_path):
    """Audits are generated output; a `git add -A` must not sweep them in.

    The mirror lands inside a working repository, so the folder carries its own
    .gitignore rather than relying on the audited project to add one.
    """
    project = tmp_path / "proj"
    project.mkdir()
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, _audit_dir(tmp_path))
    assert (project / "audit" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_an_existing_gitignore_is_left_alone(tmp_path):
    project = tmp_path / "proj"
    (project / "audit").mkdir(parents=True)
    (project / "audit" / ".gitignore").write_text("# mine\n", encoding="utf-8")
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, _audit_dir(tmp_path))
    assert (project / "audit" / ".gitignore").read_text(encoding="utf-8") == "# mine\n"
