"""Tests for audit profiles manifest, schema validation, and widget synchronization."""

from __future__ import annotations

import json
import re
from pathlib import Path

from audapack.campaign import (
    compute_manifest_hash,
    get_canonical_manifest_path,
    get_profile,
    load_profiles,
    validate_profile_manifest,
)


def test_manifest_loads_and_validates():
    profiles = load_profiles(force_reload=True)
    assert "quick3" in profiles
    assert "super10" in profiles


def test_quick3_profile_structure():
    p = get_profile("quick3")
    assert p.profile_id == "quick3"
    assert p.wave_count == 3
    assert p.finalizer_wave_id == "performance"

    core = p.get_wave_by_id("core")
    second = p.get_wave_by_id("second")
    perf = p.get_wave_by_id("performance")

    assert core is not None and core.ordinal == 1
    assert second is not None and second.ordinal == 2
    assert perf is not None and perf.ordinal == 3

    assert core.ticket_prefix == "CORE-"
    assert second.ticket_prefix == "W2-"
    assert perf.ticket_prefix == "PERF-"

    assert perf.finalizer is True


def test_super10_profile_structure():
    p = get_profile("super10")
    assert p.profile_id == "super10"
    assert p.wave_count == 10
    assert p.finalizer_wave_id == "redteam"

    expected_ids = [
        "architecture",
        "correctness",
        "state",
        "recovery",
        "security",
        "integration",
        "verification",
        "performance",
        "operator",
        "redteam",
    ]
    expected_prefixes = [
        "ARCH-",
        "CORR-",
        "STATE-",
        "REC-",
        "SEC-",
        "INT-",
        "TEST-",
        "PERF-",
        "UX-",
        "RED-",
    ]

    for idx, (wid, pfx) in enumerate(zip(expected_ids, expected_prefixes, strict=True), 1):
        w = p.get_wave_by_id(wid)
        assert w is not None, f"Missing wave {wid}"
        assert w.ordinal == idx
        assert w.ticket_prefix == pfx
        assert w.number == str(idx).zfill(2)
        assert w.done_marker != ""
        assert w.wave_header != ""
        assert w.terminal_status_key != ""

    red = p.get_wave_by_id("redteam")
    assert red is not None
    assert red.finalizer is True
    assert red.synthesis_role == "finalizer"


def test_invalid_manifest_rejected():
    invalid_data = {
        "schema_version": 2,  # wrong version
        "profiles": {},
    }
    valid, err = validate_profile_manifest(invalid_data)
    assert not valid
    assert "schema_version" in err

    invalid_data2 = {
        "schema_version": 1,
        "profiles": {
            "p1": {
                "profile_id": "p1",
                "waves": [
                    {
                        "id": "w1",
                        "ordinal": 1,
                        "ticket_prefix": "W1-",
                        "wave_header": "W1",
                        "done_marker": "DONE:",
                        "terminal_status_key": "W1",
                        "status_line": "STATUS: W1: COMPLETE",
                        "number": "01",
                        "slug": "W1",
                        "depends_on": ["non_existent"],
                    }
                ],
            }
        },
    }
    valid, err = validate_profile_manifest(invalid_data2)
    assert not valid
    assert "depends on unknown wave" in err


def test_widget_embedded_manifest_hash_matches_python():
    """Fail if Widget embedded profiles have drifted from canonical manifest."""
    root = Path(__file__).resolve().parent.parent
    manifest_path = get_canonical_manifest_path()
    widget_path = root / "resources" / "AUDAPACK_WIDGET.user.js"

    raw_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_data = json.loads(raw_manifest)
    python_hash = compute_manifest_hash(manifest_data)

    widget_text = widget_path.read_text(encoding="utf-8")
    match = re.search(
        r"const AUDIT_PROFILES_MANIFEST_SHA256\s*=\s*['\"]([0-9a-fA-F]+)['\"]",
        widget_text,
    )
    assert match is not None, "AUDIT_PROFILES_MANIFEST_SHA256 not found in AUDAPACK_WIDGET.user.js"
    widget_hash = match.group(1)

    assert widget_hash == python_hash, (
        f"Widget embedded manifest hash ({widget_hash}) != Python canonical manifest hash ({python_hash}). "
        "Run 'python tools/sync_audit_profiles.py' to synchronize."
    )


def test_bridge_parse_wave_matches_canonical_manifest_matrix():
    """Verify that Python parse_wave accepts canonical COMPLETE handoffs across all profiles and waves."""
    from audapack.bridge.storage import parse_wave

    profiles = load_profiles(force_reload=True)
    for prof_id, prof in profiles.items():
        for wave_def in prof.waves:
            pfx = wave_def.ticket_prefix.rstrip("-")
            term_key = wave_def.terminal_status_key or wave_def.slug
            done_marker = wave_def.done_marker

            handoff_md = f"""
PROJECT_NAME: AUDAPACK
DATE_TIME: 2026-08-27T16:03:00+03:00
CAMPAIGN_PROFILE: {prof_id}
WAVE_ID: {wave_def.id}
WAVE_INDEX: {wave_def.ordinal}
WAVE_COUNT: {prof.wave_count}
WAVE: {wave_def.wave_header}
STATUS: {term_key}: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [{pfx}-001] Sample defect title
EVIDENCE: In codebase.
DEFECT: Sample defect.
REPAIR: Fix defect.
OPTIMIZE: Optimize code.
ISSUE: Issue description.
GUARDRAIL: Guardrail.
VERIFY: Run tests.

{done_marker} All tickets verified.
"""
            valid, meta, err = parse_wave(handoff_md, wave_def.id, prof)
            assert valid, f"parse_wave failed for {prof_id}/{wave_def.id}: {err}"
            assert meta is not None
            assert meta["status"] == f"{term_key}: COMPLETE"
            assert meta["profile_id"] == prof_id
            assert meta["wave_id"] == wave_def.id
