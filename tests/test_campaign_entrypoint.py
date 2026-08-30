"""Comprehensive test suite for A10 campaign discovery and any-file entrypoint resolution."""

import json
from pathlib import Path
from typing import Optional

import pytest

from audapack.campaign import (
    POLICY_CAMPAIGN_DIRECTORY_READ_ALLOWED,
    STATUS_CAMPAIGN_COMPLETE,
    STATUS_CAMPAIGN_DEPENDENCY_GAP,
    STATUS_CAMPAIGN_NOT_FOUND,
    STATUS_CAMPAIGN_PARTIAL,
    STATUS_CAMPAIGN_PROJECT_CONFLICT,
    STATUS_CAMPAIGN_READY_FOR_WAVE,
    STATUS_CAMPAIGN_RUN_CONFLICT,
    build_campaign_context_header,
    get_profile,
    load_profiles,
    resolve_audit_campaign_entrypoint,
)
from audapack.saipen import saipen_gg_entrypoint


@pytest.fixture
def manifest():
    return load_profiles()


@pytest.fixture
def super10_profile():
    return get_profile("super10")


@pytest.fixture
def quick3_profile():
    return get_profile("quick3")


def create_wave_file(
    folder: Path,
    filename: str,
    project: str = "SAIPEN",
    profile: str = "super10",
    run_id: str = "test-run-100",
    manifest_sha: str = "c01ec812cf5952fdab101d5e7bc83c8251e0d9c21bb0ab06e426f5d58baedff2",
    wave_id: str = "architecture",
    wave_index: int = 1,
    status_line: Optional[str] = None,
    tickets: int = 2,
    done_marker: Optional[str] = None,
) -> Path:
    p = get_profile(profile)
    w_def = p.get_wave_by_id(wave_id) or p.waves[0]
    if status_line is None:
        status_line = w_def.status_line
    if done_marker is None:
        done_marker = w_def.done_marker
    folder.mkdir(parents=True, exist_ok=True)
    content = f"""```markdown
PROJECT_NAME: {project}
DATE_TIME: 2026-08-27T18:00:00+03:00
CAMPAIGN_PROFILE: {profile}
CAMPAIGN_PROFILE_VERSION: 1.0.0
CAMPAIGN_RUN_ID: {run_id}
CAMPAIGN_MANIFEST_SHA256: {manifest_sha}
WAVE_ID: {wave_id}
WAVE_INDEX: {wave_index}
WAVE_COUNT: 10
WAVE: AUDIT {wave_id.upper()}
TARGET: {project}.zip
BASELINE: main@abc1234
TEST_STATUS: TEST_PASSED
TEST_LIMITATION: NONE
VERIFIED_INSTEAD: NONE
{status_line}
TICKETS: {tickets}
HANDOFF: IMPLEMENTATION_AGENT

[P1] [T-001] path/to/file.py: sample defect

{done_marker.rstrip(':')}: verification completed.
```
"""
    file_path = folder / filename
    file_path.write_text(content, encoding="utf-8")
    return file_path


def test_scenario_01_empty_campaign_entry_at_wave_1(tmp_path, super10_profile):
    """Scenario 1: Entrypoint at Wave 1 in brand-new campaign -> ready for wave 1."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    # Empty entrypoint placeholder or directory
    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["active_wave_id"] == "architecture"
    assert res["active_wave_index"] == 1
    assert res["completed_count"] == 0
    assert len(res["prerequisite_artifacts"]) == 0
    assert res["context_policy"] == POLICY_CAMPAIGN_DIRECTORY_READ_ALLOWED


def test_scenario_02_entry_at_wave_1_when_1_to_6_complete(tmp_path, super10_profile, manifest):
    """Scenario 2: Entrypoint at Wave 1 file when waves 1-6 complete -> advances to wave 7."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    wave_files = []
    for idx, wave in enumerate(super10_profile.waves[:6], start=1):
        fn = f"SAIPEN__0{idx}_AUDIT_{wave.id.upper()}__test-run.md"
        p = create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )
        wave_files.append(p)

    # Point entrypoint specifically to wave 1 file
    entry_file = wave_files[0]
    res = resolve_audit_campaign_entrypoint(entry_file)

    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["active_wave_id"] == "verification"
    assert res["active_wave_index"] == 7
    assert res["completed_count"] == 6
    assert len(res["prerequisite_artifacts"]) == len(super10_profile.get_prerequisites("verification"))
    assert wave_files[0] in res["prerequisite_artifacts"]
    assert wave_files[1] in res["prerequisite_artifacts"]


def test_scenario_03_entry_at_wave_4_when_1_to_9_complete(tmp_path, super10_profile):
    """Scenario 3: Entrypoint at Wave 4 file when waves 1-9 complete -> advances to Red Team (10)."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    wave_files = []
    for idx, wave in enumerate(super10_profile.waves[:9], start=1):
        num = f"0{idx}" if idx < 10 else str(idx)
        fn = f"SAIPEN__{num}_AUDIT_{wave.id.upper()}__test-run.md"
        p = create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )
        wave_files.append(p)

    entry_file = wave_files[3] # 04_AUDIT_RECOVERY
    res = resolve_audit_campaign_entrypoint(entry_file)

    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["active_wave_id"] == "redteam"
    assert res["active_wave_index"] == 10
    assert res["completed_count"] == 9
    assert len(res["prerequisite_artifacts"]) == 9


def test_scenario_04_entry_at_wave_8_when_wave_8_partial(tmp_path, super10_profile):
    """Scenario 4: Entrypoint when wave 8 is PARTIAL -> returns CAMPAIGN_PARTIAL, resume wave 8."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    for idx, wave in enumerate(super10_profile.waves[:7], start=1):
        fn = f"SAIPEN__0{idx}_AUDIT_{wave.id.upper()}__test-run.md"
        create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )

    w8 = super10_profile.waves[7]
    w8_file = create_wave_file(
        camp_dir,
        "SAIPEN__08_AUDIT_PERFORMANCE__test-run.md",
        wave_id=w8.id,
        wave_index=8,
        status_line="STATUS: AUDIT_PERFORMANCE: PARTIAL",
        done_marker="PERF_DONE_WHEN"
    )

    res = resolve_audit_campaign_entrypoint(w8_file)
    assert res["status"] == STATUS_CAMPAIGN_PARTIAL
    assert res["active_wave_id"] == "performance"
    assert res["active_wave_index"] == 8
    assert res["next_action"] == "resume_wave"
    assert res["completed_count"] == 7


def test_scenario_05_entry_at_wave_8_when_1_to_8_complete(tmp_path, super10_profile):
    """Scenario 5: Entrypoint at complete Wave 8 when 1-8 complete -> advances to wave 9."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    for idx, wave in enumerate(super10_profile.waves[:8], start=1):
        num = f"0{idx}" if idx < 10 else str(idx)
        fn = f"SAIPEN__{num}_AUDIT_{wave.id.upper()}__test-run.md"
        create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )

    w8_path = camp_dir / "SAIPEN__08_AUDIT_PERFORMANCE__test-run.md"
    res = resolve_audit_campaign_entrypoint(w8_path)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["active_wave_id"] == "operator"
    assert res["active_wave_index"] == 9
    assert res["completed_count"] == 8


def test_scenario_06_all_10_waves_complete(tmp_path, super10_profile):
    """Scenario 6: All 10 complete -> returns CAMPAIGN_COMPLETE and final handoff."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    for idx, wave in enumerate(super10_profile.waves, start=1):
        num = f"0{idx}" if idx < 10 else str(idx)
        fn = f"SAIPEN__{num}_AUDIT_{wave.id.upper()}__test-run.md"
        create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )

    # Synthetic final file
    final_file = camp_dir / "SAIPEN__00_SUPER_AUDIT_FINAL.md"
    final_file.write_text("# Final Deduplicated Handoff", encoding="utf-8")

    res = resolve_audit_campaign_entrypoint(final_file)
    assert res["status"] == STATUS_CAMPAIGN_COMPLETE
    assert res["active_wave_id"] is None
    assert res["completed_count"] == 10
    assert res["final_handoff_path"] == final_file


def test_scenario_07_dependency_gap_detection(tmp_path, super10_profile):
    """Scenario 7: Dependency gap (e.g. 01, 02, 04 complete, 03 missing) -> CAMPAIGN_DEPENDENCY_GAP."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    # Wave 4 (recovery) depends on state (wave 3). Missing wave 3 triggers gap on wave 4.
    for idx, wave in [(1, super10_profile.waves[0]), (2, super10_profile.waves[1]), (4, super10_profile.waves[3])]:
        num = f"0{idx}" if idx < 10 else str(idx)
        fn = f"SAIPEN__{num}_AUDIT_{wave.id.upper()}__test-run.md"
        create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )

    w4_file = camp_dir / f"SAIPEN__04_AUDIT_{super10_profile.waves[3].id.upper()}__test-run.md"
    res = resolve_audit_campaign_entrypoint(w4_file)
    assert res["status"] == STATUS_CAMPAIGN_DEPENDENCY_GAP
    assert res["active_wave_id"] == "state" # wave 3 missing
    assert res["active_wave_index"] == 3


def test_scenario_08_mixed_runs_conflict(tmp_path, super10_profile):
    """Scenario 8: Mixed runs in same folder -> CAMPAIGN_RUN_CONFLICT."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    create_wave_file(
        camp_dir,
        "SAIPEN__01_AUDIT_ARCHITECTURE__runA.md",
        run_id="run-A",
        wave_id="architecture",
        wave_index=1,
        status_line=super10_profile.waves[0].status_line
    )
    create_wave_file(
        camp_dir,
        "SAIPEN__02_AUDIT_CORRECTNESS__runB.md",
        run_id="run-B",
        wave_id="correctness",
        wave_index=2,
        status_line=super10_profile.waves[1].status_line
    )

    res = resolve_audit_campaign_entrypoint(camp_dir / "SAIPEN__01_AUDIT_ARCHITECTURE__runA.md")
    assert res["status"] == STATUS_CAMPAIGN_RUN_CONFLICT
    assert "run-A" in res["error"] and "run-B" in res["error"]


def test_scenario_09_adjacent_mixed_project_waves_conflict(tmp_path, super10_profile):
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    create_wave_file(
        camp_dir,
        "SAIPEN__01_AUDIT_ARCHITECTURE__run.md",
        project="SAIPEN",
        wave_id="architecture",
        wave_index=1,
        status_line=super10_profile.waves[0].status_line,
    )
    create_wave_file(
        camp_dir,
        "OTHER__02_AUDIT_CORRECTNESS__run.md",
        project="OTHER",
        wave_id="correctness",
        wave_index=2,
        status_line=super10_profile.waves[1].status_line,
    )

    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_PROJECT_CONFLICT
    assert res["ok"] is False
    assert res["project_names"] == ["OTHER", "SAIPEN"]
    assert "OTHER" in res["error"] and "SAIPEN" in res["error"]
    assert "active_wave_id" not in res


def test_scenario_10_complete_mixed_project_campaign_conflict(tmp_path, super10_profile):
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    for idx, wave in enumerate(super10_profile.waves, start=1):
        create_wave_file(
            camp_dir,
            f"PROJECT_{idx}__{idx:02d}_AUDIT_{wave.id.upper()}__run.md",
            project="SAIPEN" if idx % 2 else "OTHER",
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker,
        )
    (camp_dir / "SAIPEN__00_SUPER_AUDIT_FINAL.md").write_text("# Final", encoding="utf-8")

    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_PROJECT_CONFLICT
    assert res["ok"] is False
    assert res["project_names"] == ["OTHER", "SAIPEN"]


def test_scenario_11_campaign_json_project_disagreement_preserves_bytes(tmp_path, super10_profile):
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)
    create_wave_file(
        camp_dir,
        "SAIPEN__01_AUDIT_ARCHITECTURE__run.md",
        project="SAIPEN",
        wave_id="architecture",
        wave_index=1,
        status_line=super10_profile.waves[0].status_line,
    )
    index = camp_dir / "campaign.json"
    index.write_bytes(b'{"project_name":"OTHER","keep":"bytes"}\r\n')
    original = index.read_bytes()

    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_PROJECT_CONFLICT
    assert res["ok"] is False
    assert res["project_name"] == "SAIPEN"
    assert res["campaign_json_project_name"] == "OTHER"
    assert index.read_bytes() == original


def test_scenario_12_moved_or_renamed_campaign_directory(tmp_path, super10_profile):
    """Scenario 9: Moved/renamed campaign directory -> resolves correctly relative to new path."""
    camp_dir = tmp_path / "ORIGINAL_CAMPAIGN"
    camp_dir.mkdir(parents=True)

    create_wave_file(
        camp_dir,
        "SAIPEN__01_AUDIT_ARCHITECTURE__run1.md",
        wave_id="architecture",
        wave_index=1,
        status_line=super10_profile.waves[0].status_line
    )

    # Rename directory
    new_dir = tmp_path / "MOVED_CAMPAIGN_DIR"
    camp_dir.rename(new_dir)

    entry = new_dir / "SAIPEN__01_AUDIT_ARCHITECTURE__run1.md"
    res = resolve_audit_campaign_entrypoint(entry)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["campaign_root"] == new_dir
    assert res["active_wave_id"] == "correctness"


def test_scenario_10_campaign_context_header():
    """Scenario 10: Campaign context header generation."""
    prof = get_profile("super10")
    header = build_campaign_context_header(prof)
    assert "CAMPAIGN CONTEXT" in header
    assert "AUDIT_CAMPAIGN_CONTEXT_POLICY: CAMPAIGN_DIRECTORY_READ_ALLOWED" in header
    assert "CAMPAIGN_PROFILE: super10" in header
    assert "WAVE_COUNT: 10" in header


def test_scenario_11_containment_fence_rejection(tmp_path):
    """Scenario 11: Containment guard rejects escaping paths."""
    non_existent = tmp_path / "non_existent_folder" / "fake_file.md"
    res = resolve_audit_campaign_entrypoint(non_existent)
    assert res["status"] == STATUS_CAMPAIGN_NOT_FOUND


def test_scenario_12_crash_recovery_index_repair(tmp_path, super10_profile):
    """Scenario 12: Index crash recovery: disk wave complete, campaign.json stale -> repairs campaign.json."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    # Create stale index indicating wave 0
    stale_index = {
        "campaign_root": str(camp_dir),
        "profile_id": "super10",
        "run_id": "test-run-100",
        "completed_count": 0,
        "completed_waves": []
    }
    (camp_dir / "campaign.json").write_text(json.dumps(stale_index), encoding="utf-8")

    # Create 3 real complete wave files on disk
    for idx, wave in enumerate(super10_profile.waves[:3], start=1):
        fn = f"SAIPEN__0{idx}_AUDIT_{wave.id.upper()}__test-run.md"
        create_wave_file(
            camp_dir,
            fn,
            wave_id=wave.id,
            wave_index=idx,
            status_line=wave.status_line,
            done_marker=wave.done_marker
        )

    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["completed_count"] == 3
    assert res["active_wave_id"] == "recovery"

    # Check repaired campaign.json
    repaired = json.loads((camp_dir / "campaign.json").read_text(encoding="utf-8"))
    assert repaired["completed_count"] == 3
    assert len(repaired["completed_waves"]) == 3


def test_scenario_13_saipen_gg_entrypoint_wrapper(tmp_path, super10_profile):
    """Scenario 13: saipen_gg_entrypoint exposes resolve_audit_campaign_entrypoint for SAIPEN gg."""
    camp_dir = tmp_path / "SAIPEN__2026-08-27__test-run-100"
    camp_dir.mkdir(parents=True)

    create_wave_file(
        camp_dir,
        "SAIPEN__01_AUDIT_ARCHITECTURE__test-run.md",
        wave_id="architecture",
        wave_index=1,
        status_line=super10_profile.waves[0].status_line
    )

    res = saipen_gg_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["active_wave_id"] == "correctness"
    assert res["project_name"] == "SAIPEN"


def test_scenario_14_quick3_profile_non_regression(tmp_path, quick3_profile):
    """Scenario 14: Quick3 3-wave campaign resolution non-regression."""
    camp_dir = tmp_path / "AUDAPACK__quick3__run"
    camp_dir.mkdir(parents=True)

    # Create Core and Second wave
    create_wave_file(
        camp_dir,
        "AUDAPACK__01_AUDIT_CORE__run.md",
        project="AUDAPACK",
        profile="quick3",
        wave_id="core",
        wave_index=1,
        status_line=quick3_profile.waves[0].status_line,
        done_marker=quick3_profile.waves[0].done_marker
    )
    create_wave_file(
        camp_dir,
        "AUDAPACK__02_AUDIT_SECOND_WAVE__run.md",
        project="AUDAPACK",
        profile="quick3",
        wave_id="second",
        wave_index=2,
        status_line=quick3_profile.waves[1].status_line,
        done_marker=quick3_profile.waves[1].done_marker
    )

    res = resolve_audit_campaign_entrypoint(camp_dir)
    assert res["status"] == STATUS_CAMPAIGN_READY_FOR_WAVE
    assert res["profile_id"] == "quick3"
    assert res["active_wave_id"] == "performance"
    assert res["active_wave_index"] == 3
    assert res["completed_count"] == 2
