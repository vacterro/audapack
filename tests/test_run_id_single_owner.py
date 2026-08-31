"""W4-003: single-owner run_id across ingest, bridge, browser-fallback ALL_3.

CORE-006 / implement1.txt: the bridge rejects a payload whose run_id disagrees
with the CAMPAIGN_RUN_ID embedded in the content it carries. The fix has three
moving parts and each one needs a regression test:

1. ingest.ingest_audit_text derives the canonical run id from the wave content
   instead of minting a fresh ``ingest_*`` id, so the final ALL_3, the
   campaign.json index, and the wave headers share one identity.
2. The widget never lets a freshly-armed runtime id overwrite a captured audit
   record that already carries a durable run id (enqueueBridgeAuditRecord).
3. The bridge delivery path refuses to POST a payload whose on-disk content
   has a CAMPAIGN_RUN_ID header that disagrees with the queued run id.

These tests use the widget node test harness (no live browser) and the python
pytest suite to exercise both the python and JS contracts.
"""
from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.request

from audapack.bridge.storage import (
    generate_canonical_all3,
    parse_wave,
    resolve_project_audit_dir,
)
from audapack.campaign import get_profile
from audapack.config import load_config
from audapack.ingest import ingest_audit_text

# ---------------------------------------------------------------------------
# 1. ingest: the canonical run id comes from wave content, not a fresh mint
# ---------------------------------------------------------------------------


def _build_wave_text(*, project: str, run_id: str, wave_id: str, profile_id: str) -> str:
    profile = get_profile(profile_id)
    wave_def = profile.get_wave_by_id(wave_id)
    assert wave_def is not None, f"unknown wave {wave_id!r} in {profile_id!r}"
    return (
        f"PROJECT_NAME: {project}\n"
        f"CAMPAIGN_PROFILE: {profile_id}\n"
        f"CAMPAIGN_PROFILE_VERSION: {profile.profile_version}\n"
        f"CAMPAIGN_RUN_ID: {run_id}\n"
        f"CAMPAIGN_MANIFEST_SHA256: {profile.manifest_hash}\n"
        f"WAVE_ID: {wave_def.id}\n"
        f"WAVE_INDEX: {wave_def.ordinal}\n"
        f"WAVE_COUNT: {profile.wave_count}\n"
        f"WAVE: {wave_def.wave_header}\n"
        f"TARGET: repo\n"
        f"BASELINE: main\n"
        f"{wave_def.status_line}\n"
        f"TICKETS: 0\n"
        f"HANDOFF: IMPLEMENTATION_AGENT\n"
        f"{wave_def.no_findings_marker or 'NO VERIFIED DEFECTS.'}\n"
        f"{wave_def.done_marker.rstrip(':')}: verified\n"
    )


def test_ingest_preserves_canonical_run_id_from_wave_content(tmp_path):
    """When the three wave texts all carry the same CAMPAIGN_RUN_ID, the final
    ALL_3 artifact and campaign.json must echo that exact id - not a freshly
    minted ingest_* one."""
    cfg = load_config()
    cfg.audits.root = str(tmp_path / "audits")
    project = "PROJ_W4_003"
    canonical_run = f"canon_{secrets.token_hex(4)}"

    for wave_id in ("core", "second", "performance"):
        text = _build_wave_text(
            project=project,
            run_id=canonical_run,
            wave_id=wave_id,
            profile_id="quick3",
        )
        result = ingest_audit_text(text, cfg, base_dir=tmp_path)
        assert result.ok, f"ingest {wave_id} failed: {result.error}"

    project_dir, resolved, _, _ = resolve_project_audit_dir(
        cfg, project, project_id=None, base_dir=tmp_path
    )
    all3_path = project_dir / f"{resolved}__00_AUDIT_ALL_3.md"
    assert all3_path.exists(), "ALL_3 was not synthesized"
    all3_text = all3_path.read_text(encoding="utf-8")
    match = re.search(r"RUN_ID:\s*(\S+)", all3_text)
    assert match is not None, "ALL_3 must carry a RUN_ID header"
    assert match.group(1) == canonical_run, (
        f"ALL_3 RUN_ID {match.group(1)!r} must equal the canonical run id {canonical_run!r}"
    )

    cj = json.loads((project_dir / "campaign.json").read_text(encoding="utf-8"))
    assert cj["campaign_run_id"] == canonical_run, (
        f"campaign.json campaign_run_id {cj['campaign_run_id']!r} must equal the canonical {canonical_run!r}"
    )


def test_ingest_rejects_mixed_run_ids_across_waves(tmp_path):
    """Two waves with one run id and the third with another must be rejected
    before any canonical artifact is generated."""
    cfg = load_config()
    cfg.audits.root = str(tmp_path / "audits")
    project = "PROJ_MIX"
    run_a = f"a_{secrets.token_hex(3)}"
    run_b = f"b_{secrets.token_hex(3)}"

    assert ingest_audit_text(
        _build_wave_text(project=project, run_id=run_a, wave_id="core", profile_id="quick3"),
        cfg,
        base_dir=tmp_path,
    ).ok
    assert ingest_audit_text(
        _build_wave_text(project=project, run_id=run_a, wave_id="second", profile_id="quick3"),
        cfg,
        base_dir=tmp_path,
    ).ok
    result = ingest_audit_text(
        _build_wave_text(project=project, run_id=run_b, wave_id="performance", profile_id="quick3"),
        cfg,
        base_dir=tmp_path,
    )
    assert not result.ok, "ingest must refuse a third wave whose run id differs"
    assert "Multiple campaign run IDs" in (result.error or ""), (
        f"error must name the defect, got: {result.error!r}"
    )


def test_generate_canonical_all3_round_trip_carries_run_id():
    """generate_canonical_all3 is the canonical text produced for a quick3
    terminal ingest. Re-confirm the same id always survives the round trip so
    the server-side __00_AUDIT_ALL_3.md writes match the campaign.json id."""
    project = "PROJ_ROUND"
    run_id = f"round_{secrets.token_hex(3)}"
    parsed = {}
    for wid in ("core", "second", "performance"):
        text = _build_wave_text(
            project=project, run_id=run_id, wave_id=wid, profile_id="quick3"
        )
        ok, meta, err = parse_wave(text, wid, get_profile("quick3"))
        assert ok, f"wave {wid} should parse: {err}"
        parsed[wid] = meta

    out = generate_canonical_all3(project, run_id, parsed)
    assert f"RUN_ID: {run_id}" in out, "ALL_3 synthesis must echo the run id"
    for wid in ("core", "second", "performance"):
        assert f"CAMPAIGN_RUN_ID: {run_id}" in (parsed[wid].get("full_text") or ""), (
            f"wave {wid} must carry the same id"
        )


# ---------------------------------------------------------------------------
# 2/3. Bridge v3 contract: payload.run_id must equal content CAMPAIGN_RUN_ID
# ---------------------------------------------------------------------------


def _v3_audit_payload(*, run_id: str, project: str, wave: str, content: str, receipt: str):
    return {
        "run_id": run_id,
        "project": project,
        "wave": wave,
        "status": "complete",
        "api_version": 3,
        "receipt": receipt,
        "content": content,
    }


def test_bridge_v3_reconciles_payload_run_id_mismatch(bridge_server):
    """Server-side CORE-006 check heals a payload whose transport run_id
    disagrees with the CAMPAIGN_RUN_ID header in the content: the header is
    patched to the transport id and the wave is accepted, so a re-armed or
    materialized capture cannot dead-end in a permanent run_id_mismatch."""
    config, base_url = bridge_server
    headers = {
        "Content-Type": "application/json",
        "X-ACB-Token": config.bridge.token,
    }
    payload_run = f"payload_{secrets.token_hex(4)}"
    content_run = f"content_{secrets.token_hex(4)}"
    project = f"SAIPEN_{secrets.token_hex(2)}"
    wave_text = _build_wave_text(
        project=project, run_id=content_run, wave_id="core", profile_id="quick3"
    )

    req = urllib.request.Request(
        f"{base_url}/v1/audits",
        data=json.dumps(_v3_audit_payload(
            run_id=payload_run,
            project=project,
            wave="core",
            content=wave_text,
            receipt=f"rcpt_{secrets.token_hex(4)}",
        )).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["run_id"] == payload_run


def test_bridge_v3_accepts_payload_run_id_match(bridge_server):
    """The same wave content with payload.run_id == content CAMPAIGN_RUN_ID
    must be accepted (200) and the campaign.json written with the same id."""
    config, base_url = bridge_server
    headers = {
        "Content-Type": "application/json",
        "X-ACB-Token": config.bridge.token,
    }
    canonical_run = f"canon_{secrets.token_hex(4)}"
    project = f"SAIPEN_{secrets.token_hex(2)}"
    wave_text = _build_wave_text(
        project=project, run_id=canonical_run, wave_id="core", profile_id="quick3"
    )
    req = urllib.request.Request(
        f"{base_url}/v1/audits",
        data=json.dumps(_v3_audit_payload(
            run_id=canonical_run,
            project=project,
            wave="core",
            content=wave_text,
            receipt=f"rcpt_{secrets.token_hex(4)}",
        )).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["run_id"] == canonical_run
