"""COMPRESS AUDIT (CM): a third first-class profile.

Adding one is the real test. Artifact behaviour used to be chosen with
`if profile_id == "quick3": ... else: SUPER10`, which silently made every new
profile a Super10 campaign, and profile detection read every non-super10
directory as quick3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audapack.bridge.storage import generate_canonical_campaign, parse_wave
from audapack.campaign import (
    ARTIFACT_KIND_DIRECT_HANDOFF,
    ARTIFACT_KIND_QUICK3_COMBINED,
    ARTIFACT_KIND_SUPER10_SYNTHESIS,
    get_profile,
    load_profiles,
)

RUN_ID = "acb-compress-0001"


def compress_handoff(tickets: int = 1) -> str:
    wave = get_profile("compress").waves[0]
    lines = [
        "PROJECT_NAME: AUDAPACK",
        "DATE_TIME: 2026-09-01T18:00:00+03:00",
        "CAMPAIGN_PROFILE: compress",
        f"CAMPAIGN_RUN_ID: {RUN_ID}",
        "WAVE_ID: compress",
        f"WAVE: {wave.wave_header}",
        "TARGET: AUDAPACK repo",
        "BASELINE: main@abc1234",
        wave.status_line,
        f"TICKETS: {tickets}",
        "HANDOFF: IMPLEMENTATION_AGENT",
        "",
    ]
    if tickets == 0:
        lines.append(wave.no_findings_marker)
    for index in range(1, tickets + 1):
        lines.append(f"[P1] [CMP-{index:03d}] DELETE audapack/dead.py")
        lines.extend(f"{field}: sample {field.lower()}." for field in wave.ticket_fields)
        lines.append("")
    lines.append(f"{wave.done_marker} the deletions land and the suite stays green.")
    return "\n".join(lines)


def test_the_registry_exposes_compress_as_a_peer_profile():
    profiles = load_profiles(force_reload=True)
    assert set(profiles) >= {"quick3", "super10", "compress"}


def test_compress_is_one_read_only_finalizer_wave():
    profile = get_profile("compress")
    assert profile.wave_count == 1
    wave = profile.waves[0]
    assert (wave.id, wave.ordinal, wave.number) == ("compress", 1, "01")
    assert wave.ticket_prefix == "CMP-"
    assert wave.wave_header == "COMPRESS AUDIT"
    assert wave.status_line == "STATUS: COMPRESS: COMPLETE"
    assert wave.done_marker == "COMPRESS_DONE_WHEN:"
    assert wave.finalizer is True
    assert wave.ticket_fields == ["EVIDENCE", "WASTE", "ACTION", "BEHAVIOR_GUARD", "IMPACT", "VERIFY"]
    assert wave.no_findings_marker == "NO VERIFIED SAFE COMPRESSION OPPORTUNITIES."


def test_each_profile_declares_its_own_artifact_behaviour():
    assert get_profile("quick3").canonical_artifact_kind == ARTIFACT_KIND_QUICK3_COMBINED
    assert get_profile("super10").canonical_artifact_kind == ARTIFACT_KIND_SUPER10_SYNTHESIS
    assert get_profile("compress").canonical_artifact_kind == ARTIFACT_KIND_DIRECT_HANDOFF


def test_the_canonical_compress_handoff_is_named_for_the_profile():
    assert get_profile("compress").final_handoff_name("DEMO") == "DEMO__00_COMPRESS_AUDIT.md"
    # Backward compatibility: the existing names are unchanged.
    assert get_profile("quick3").final_handoff_name("DEMO") == "DEMO__00_AUDIT_ALL_3.md"
    assert get_profile("super10").final_handoff_name("DEMO") == "DEMO__00_SUPER_AUDIT_FINAL.md"


def test_parse_wave_validates_a_compress_handoff():
    valid, meta, err = parse_wave(compress_handoff(2), "compress", get_profile("compress"))
    assert valid, err
    assert meta["profile_id"] == "compress"
    assert meta["wave_id"] == "compress"
    assert meta["tickets"] == 2
    assert meta["campaign_run_id"] == RUN_ID


def test_a_zero_ticket_compress_handoff_is_valid():
    valid, meta, err = parse_wave(compress_handoff(0), "compress", get_profile("compress"))
    assert valid, err
    assert meta["tickets"] == 0


def test_a_compress_ticket_missing_a_required_field_is_refused():
    body = compress_handoff(1).replace("IMPACT: sample impact.\n", "")
    valid, _meta, err = parse_wave(body, "compress", get_profile("compress"))
    assert not valid
    assert "IMPACT" in err


def test_a_compress_handoff_with_the_wrong_terminal_status_is_refused():
    body = compress_handoff(1).replace("STATUS: COMPRESS: COMPLETE", "STATUS: COMPRESS: PARTIAL")
    valid, _meta, err = parse_wave(body, "compress", get_profile("compress"))
    assert not valid
    assert "STATUS" in err


def test_compress_synthesis_is_the_wave_itself_and_never_a_super_audit():
    profile = get_profile("compress")
    _valid, meta, _err = parse_wave(compress_handoff(1), "compress", profile)
    result = generate_canonical_campaign(profile, RUN_ID, {"compress": meta}, "AUDAPACK")

    assert set(result) == {"handoff"}, "compress must not generate SUPER_AUDIT_* artifacts"
    text = result["handoff"]
    assert "CAMPAIGN_PROFILE: compress" in text
    assert f"CAMPAIGN_RUN_ID: {RUN_ID}" in text
    assert "[CMP-001]" in text
    assert "SUPER_AUDIT" not in text


def test_quick3_and_super10_synthesis_are_unchanged():
    quick3 = get_profile("quick3")
    parsed = {}
    for wave in quick3.waves:
        body = "\n".join([
            "PROJECT_NAME: AUDAPACK",
            f"WAVE: {wave.wave_header}",
            wave.status_line,
            "TICKETS: 0",
            wave.no_findings_marker,
            f"{wave.done_marker} done.",
        ])
        ok, meta, err = parse_wave(body, wave.id, quick3)
        assert ok, err
        parsed[wave.id] = meta
    assert set(generate_canonical_campaign(quick3, "acb-q3", parsed, "AUDAPACK")) == {"all3"}

    super10 = get_profile("super10")
    assert set(generate_canonical_campaign(super10, "acb-s10", {}, "AUDAPACK")) == {
        "super_all", "super_final", "super_index",
    }


def test_ingest_prefers_the_declared_profile_over_legacy_heuristics():
    from audapack.ingest import detect_wave_and_profile

    wave_id, profile_id = detect_wave_and_profile(compress_handoff(1))
    assert (wave_id, profile_id) == ("compress", "compress")


def test_the_indexer_recognises_a_compress_directory(tmp_path):
    """Every non-super10 directory used to be read as quick3."""
    from audapack.audits import AuditIndexer
    from audapack.config import AppConfig, AuditsConfig
    from audapack.models import Project

    project_dir = tmp_path / "MAIN0" / "AUDAPACK"
    project_dir.mkdir(parents=True)
    (project_dir / "AUDAPACK__01_AUDIT_COMPRESS.md").write_text(compress_handoff(1), encoding="utf-8")
    (project_dir / "AUDAPACK__00_COMPRESS_AUDIT.md").write_text(compress_handoff(1), encoding="utf-8")

    config = AppConfig(audits=AuditsConfig(root=str(tmp_path)))
    project = Project(id="audapack", display_name="AUDAPACK",
                      source_path=str(tmp_path / "src"), priority_group="MAIN0", slot=1)
    snapshot = AuditIndexer(config).scan_project(project)

    assert snapshot.audit_profile_id == "compress"
    assert snapshot.total_waves == 1


def test_compact_labels_come_from_the_manifest():
    """`"A10" if super10 else "A3"` labels every future profile A3."""
    from audapack.campaign import profile_choices, profile_short_label

    assert profile_short_label("compress") == "CM"
    assert profile_short_label("quick3") == "A3"
    assert profile_short_label("super10") == "A10"
    assert profile_short_label("") == "A3"
    assert dict(profile_choices()) == {"quick3": "A3", "super10": "A10", "compress": "CM"}


def test_the_selected_profile_survives_a_config_round_trip(tmp_path):
    """START AUDIT used to fall back to quick3 with no way to choose."""
    from audapack.config import AppConfig, load_config, save_config

    config = AppConfig()
    config.audits.profile = "compress"
    save_config(config, base_dir=str(tmp_path))
    assert load_config(base_dir=str(tmp_path)).audits.profile == "compress"


def test_an_unknown_persisted_profile_falls_back_instead_of_breaking(tmp_path):
    from audapack.config import AppConfig, load_config, save_config

    config = AppConfig()
    config.audits.profile = "retired-profile"
    save_config(config, base_dir=str(tmp_path))
    assert load_config(base_dir=str(tmp_path)).audits.profile == "quick3"


def test_the_desktop_exposes_all_three_profiles_as_peers(tmp_path, qapp):
    from audapack.config import AppConfig, AuditsConfig
    from audapack.services.project_service import ProjectService
    from audapack.ui_qt.main_window import MainWindow

    config = AppConfig(audits=AuditsConfig(root=str(tmp_path / "audits"), profile="compress"))
    window = MainWindow(ProjectService(config, base_dir=tmp_path))
    try:
        assert set(window.profile_actions) == {"quick3", "super10", "compress"}
        assert [a.text() for a in window.profile_actions.values()] == ["A3", "A10", "CM"]
        assert window.profile_actions["compress"].isChecked() is True
        assert window.profile_actions["quick3"].isChecked() is False

        window._on_select_audit_profile("super10")
        assert window._service.config.audits.profile == "super10"
        assert window.profile_actions["super10"].isChecked() is True
        assert window.profile_actions["compress"].isChecked() is False
    finally:
        window.close()
