"""A finished audit lands in the audited project too, so `cc` finds it there.

The audit root is one central place -- right for the desktop, wrong for an
agent working inside the repo, which then has to be told where audits live.

Delivered as a CANONICAL LAYER. SAIPEN's Audit Inbox reads exactly the direct
files matching ``^[1-9][0-9]*\\.md$``; everything else is residue it never
reads, never captures and never deletes. Mirroring the run under its own long
filenames therefore delivered NOTHING -- proven live: three projects whose
`audit/` folder was full of AUDAPACK files and whose inbox read EMPTY, with all
five files listed as residue.
"""

from __future__ import annotations

from pathlib import Path

from audapack.bridge.storage import mirror_project_audits
from audapack.config import AppConfig, AuditsConfig


def _config(tmp_path: Path, **audits) -> AppConfig:
    return AppConfig(audits=AuditsConfig(root=str(tmp_path / "root"), **audits))


def _audit_dir(tmp_path: Path, final: str = "final") -> Path:
    src = tmp_path / "root" / "MAIN0" / "PROJ"
    if not src.exists():
        (src / "_history").mkdir(parents=True)
    (src / "PROJ__00_AUDIT_ALL_3.md").write_text(final, encoding="utf-8")
    (src / "PROJ__01_AUDIT_CORE.md").write_text("core", encoding="utf-8")
    (src / "campaign.json").write_text("{}", encoding="utf-8")
    (src / "_history" / "PROJ__00_AUDIT_ALL_3__old.md").write_text("old", encoding="utf-8")
    return src


def _handoff(src: Path) -> Path:
    return src / "PROJ__00_AUDIT_ALL_3.md"


def test_the_mirror_is_off_until_it_is_asked_for(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    assert mirror_project_audits(_config(tmp_path), project, src, _handoff(src)) == []
    assert not (project / "audit").exists()


def test_a_finished_audit_arrives_as_a_canonical_layer(tmp_path):
    """1.md, not PROJ__00_AUDIT_ALL_3.md -- the only name the inbox reads."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)

    copied = mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))

    assert [path.name for path in copied] == ["1.md"]
    assert (project / "audit" / "1.md").read_text(encoding="utf-8") == "final"


def test_the_layer_is_visible_to_the_inbox_reader(tmp_path):
    """The whole point: what we deliver must read as work the agent owes."""
    from audapack import agent_inbox

    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))

    state = agent_inbox.read_inbox(project)
    assert state.verdict == agent_inbox.UNREAD
    assert state.unread_count == 1
    assert state.residue == [], "a delivery must not leave anything the inbox never reads"


def test_a_second_audit_becomes_the_next_layer(tmp_path):
    """Never rewrite a layer the agent may be working right now."""
    project = tmp_path / "proj"
    project.mkdir()
    config = _config(tmp_path, mirror_into_project=True)

    src = _audit_dir(tmp_path, final="first")
    mirror_project_audits(config, project, src, _handoff(src))
    src = _audit_dir(tmp_path, final="second")
    mirror_project_audits(config, project, src, _handoff(src))

    assert (project / "audit" / "1.md").read_text(encoding="utf-8") == "first"
    assert (project / "audit" / "2.md").read_text(encoding="utf-8") == "second"


def test_redelivering_the_same_bytes_adds_nothing(tmp_path):
    """Otherwise the operator is told the agent owes work it already has."""
    project = tmp_path / "proj"
    project.mkdir()
    config = _config(tmp_path, mirror_into_project=True)
    src = _audit_dir(tmp_path)

    mirror_project_audits(config, project, src, _handoff(src))
    second = mirror_project_audits(config, project, src, _handoff(src))

    assert second == []
    assert sorted(p.name for p in (project / "audit").iterdir()) == [".gitignore", "1.md"]


def test_a_layer_number_a_settled_receipt_used_is_never_reissued(tmp_path):
    """Reusing it would rebind fresh bytes onto a closed receipt's path."""
    import json

    project = tmp_path / "proj"
    binding = project / ".saipen" / "intake"
    binding.mkdir(parents=True)
    (binding / "audit_inbox.json").write_text(json.dumps({
        "schema_version": 1,
        "layers": {"audit/1.md": {"state": "DELETED", "layer": 1, "file_sha256": "x"}},
    }), encoding="utf-8")
    src = _audit_dir(tmp_path)

    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))
    assert (project / "audit" / "2.md").is_file()
    assert not (project / "audit" / "1.md").exists()


def test_per_wave_files_are_residue_and_stay_off(tmp_path):
    """They are never read and never cleaned up, so a settled inbox reads dirty."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))
    assert not (project / "audit" / "campaign.json").exists()
    assert not (project / "audit" / "PROJ__01_AUDIT_CORE.md").exists()


def test_per_wave_files_can_be_asked_for(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True, mirror_include_waves=True)

    copied = mirror_project_audits(config, project, src, _handoff(src))

    assert "1.md" in {path.name for path in copied}
    assert (project / "audit" / "campaign.json").is_file()
    assert (project / "audit" / "PROJ__01_AUDIT_CORE.md").read_text(encoding="utf-8") == "core"


def test_history_is_not_mirrored(tmp_path):
    """The project wants the current audit, not every past run."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True, mirror_include_waves=True)
    mirror_project_audits(config, project, src, _handoff(src))
    assert not (project / "audit" / "_history").exists()


def test_the_central_root_still_holds_everything(tmp_path):
    """A copy, never a move: the desktop keeps reading the audit root."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))
    assert (src / "PROJ__00_AUDIT_ALL_3.md").is_file()
    assert (src / "_history" / "PROJ__00_AUDIT_ALL_3__old.md").is_file()


def test_the_folder_name_is_configurable(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True, mirror_dir_name="_AUDITS")
    mirror_project_audits(config, project, src, _handoff(src))
    assert (project / "_AUDITS" / "1.md").is_file()


def test_a_folder_name_cannot_escape_the_project(tmp_path):
    """A configured name is one path segment, never a way out of the repo."""
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True, mirror_dir_name=r"..\..\elsewhere")
    mirror_project_audits(config, project, src, _handoff(src))
    assert (project / "audit" / "1.md").is_file()
    assert not (tmp_path.parent / "elsewhere").exists()


def test_a_project_with_no_source_path_is_skipped(tmp_path):
    config = _config(tmp_path, mirror_into_project=True)
    src = _audit_dir(tmp_path)
    assert mirror_project_audits(config, "", src, _handoff(src)) == []


def test_the_settings_survive_a_config_round_trip(tmp_path, monkeypatch):
    from audapack.config import load_config, save_config

    monkeypatch.setenv("AUDAPACK_STATE_DIR", str(tmp_path / "state"))
    config = load_config()
    config.audits.mirror_into_project = True
    config.audits.mirror_dir_name = "audit"
    config.audits.mirror_include_waves = True
    save_config(config)

    reloaded = load_config()
    assert reloaded.audits.mirror_into_project is True
    assert reloaded.audits.mirror_dir_name == "audit"
    assert reloaded.audits.mirror_include_waves is True


def test_the_mirror_excludes_itself_from_the_repository(tmp_path):
    """Audits are generated output; a `git add -A` must not sweep them in.

    Dot-prefixed, so the inbox counts it as infrastructure rather than residue.
    """
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))
    assert (project / "audit" / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_an_existing_gitignore_is_left_alone(tmp_path):
    project = tmp_path / "proj"
    (project / "audit").mkdir(parents=True)
    (project / "audit" / ".gitignore").write_text("# mine\n", encoding="utf-8")
    src = _audit_dir(tmp_path)
    mirror_project_audits(_config(tmp_path, mirror_into_project=True), project, src, _handoff(src))
    assert (project / "audit" / ".gitignore").read_text(encoding="utf-8") == "# mine\n"


def test_two_writers_never_lose_a_layer_to_the_same_number(tmp_path):
    """W2-002 (audit/2.md): the project audit inbox has multiple writers.

    `next_layer_number()` OBSERVES the free number, it does not reserve it, and
    publication then used plain `write_bytes()`. Two finalizations -- or one
    racing an INAUDIT assignment, a genuinely independent writer into the same
    folder -- both picked the same number and the second silently truncated the
    first. Measured: both reported delivering `1.md`, one payload survived, and
    the other audit was gone from the project's queue with both producers
    claiming success.
    """
    import threading

    from audapack import agent_inbox

    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True)

    first = src / "PROJ__00_AUDIT_ALL_3.md"
    second = src / "PROJ__00_COMPRESS_AUDIT.md"
    first.write_text("AUDIT_ONE", encoding="utf-8")
    second.write_text("AUDIT_TWO", encoding="utf-8")

    # Both writers observe the free number, then both publish: the exact window
    # the reproduction used.
    gate = threading.Barrier(2)
    real_next = agent_inbox.next_layer_number
    seen = []

    def synchronized_next(*args, **kwargs):
        number = real_next(*args, **kwargs)
        if len(seen) < 2:
            seen.append(number)
            try:
                gate.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        return number

    agent_inbox.next_layer_number = synchronized_next
    try:
        results: dict[str, list] = {}
        threads = [
            threading.Thread(target=lambda: results.__setitem__(
                "a", mirror_project_audits(config, project, src, first))),
            threading.Thread(target=lambda: results.__setitem__(
                "b", mirror_project_audits(config, project, src, second))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
    finally:
        agent_inbox.next_layer_number = real_next

    assert seen[:2] == [seen[0], seen[0]], f"the race never happened: {seen}"
    layers = sorted(
        path for path in (project / "audit").iterdir()
        if agent_inbox.layer_number(path.name) is not None
    )
    payloads = sorted(path.read_text(encoding="utf-8") for path in layers)
    assert payloads == ["AUDIT_ONE", "AUDIT_TWO"], f"a delivery was overwritten: {payloads}"
    assert len(layers) == 2, [path.name for path in layers]
    assert results["a"] and results["b"], results


def test_a_layer_taken_between_look_and_write_is_stepped_over(tmp_path):
    """The loser of the race takes the NEXT free number, not a failure."""
    from audapack import agent_inbox

    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path, "MINE")
    config = _config(tmp_path, mirror_into_project=True)

    real_next = agent_inbox.next_layer_number
    stolen = {"done": False}

    def steal_then_answer(root, directory, *args, **kwargs):
        number = real_next(root, directory, *args, **kwargs)
        if not stolen["done"]:
            stolen["done"] = True
            (Path(directory) / f"{number}.md").write_text("SOMEONE ELSE", encoding="utf-8")
        return number

    agent_inbox.next_layer_number = steal_then_answer
    try:
        copied = mirror_project_audits(config, project, src, _handoff(src))
    finally:
        agent_inbox.next_layer_number = real_next

    assert copied, "publication gave up instead of taking the next number"
    assert copied[0].read_text(encoding="utf-8") == "MINE"
    assert (project / "audit" / "1.md").read_text(encoding="utf-8") == "SOMEONE ELSE"


def test_redelivering_identical_bytes_still_adds_no_layer(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True)

    first = mirror_project_audits(config, project, src, _handoff(src))
    again = mirror_project_audits(config, project, src, _handoff(src))
    assert first and again == []
