"""CORE-003: campaign artifact identity is enforced, not nominal.

A v3 artifact must declare CAMPAIGN_PROFILE, CAMPAIGN_PROFILE_VERSION and
CAMPAIGN_MANIFEST_SHA256; the profile id/version must match the selected
profile and the manifest hash must match exactly unless an explicit
compatibility-ledger edge proves historical equivalence. Parser, resolver and
server must agree: an artifact cannot be accepted by one path and rejected by
another.
"""
from __future__ import annotations

from pathlib import Path

from audapack.bridge.storage import parse_wave
from audapack.campaign import (
    STATUS_CAMPAIGN_MANIFEST_MISMATCH,
    get_canonical_manifest_hash,
    get_profile,
    manifest_hash_is_compatible,
    register_manifest_compatibility,
    resolve_audit_campaign_entrypoint,
)


def _compress_wave(
    *,
    profile: str = "compress",
    profile_version: str | None = None,
    manifest: str | None = None,
    include_profile: bool = True,
    include_version: bool = True,
    include_manifest: bool = True,
) -> str:
    p = get_profile(profile)
    wave = p.waves[0]
    if profile_version is None:
        profile_version = p.profile_version
    if manifest is None:
        manifest = p.manifest_hash or get_canonical_manifest_hash()
    lines = [
        "PROJECT_NAME: AUDAPACK",
        "DATE_TIME: 2026-09-01T18:00:00+03:00",
    ]
    if include_profile:
        lines.append(f"CAMPAIGN_PROFILE: {profile}")
    if include_version:
        lines.append(f"CAMPAIGN_PROFILE_VERSION: {profile_version}")
    lines.append("CAMPAIGN_RUN_ID: acb-identity-0001")
    if include_manifest:
        lines.append(f"CAMPAIGN_MANIFEST_SHA256: {manifest}")
    lines.extend([
        "WAVE_ID: compress",
        f"WAVE: {wave.wave_header}",
        "TARGET: AUDAPACK repo",
        "BASELINE: main@abc1234",
        wave.status_line,
        "TICKETS: 0",
        "HANDOFF: IMPLEMENTATION_AGENT",
        "",
        wave.no_findings_marker,
        f"{wave.done_marker} nothing to compress.",
    ])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# manifest_hash_is_compatible
# --------------------------------------------------------------------------- #

def test_manifest_hash_helper_rejects_missing_and_arbitrary_by_default():
    profile = get_profile("quick3")
    wave = profile.waves[0]
    assert manifest_hash_is_compatible(get_canonical_manifest_hash(), profile, wave)
    assert not manifest_hash_is_compatible("", profile, wave)
    assert not manifest_hash_is_compatible("0" * 64, profile, wave)
    assert not manifest_hash_is_compatible("deadbeef" * 8, profile, wave)


def test_manifest_hash_helper_accepts_missing_only_for_the_explicit_legacy_boundary():
    profile = get_profile("quick3")
    wave = profile.waves[0]
    assert manifest_hash_is_compatible("", profile, wave, allow_missing=True)


def test_manifest_hash_helper_accepts_a_proven_ledger_edge():
    profile = get_profile("quick3")
    wave = profile.waves[0]
    historical = "ab" * 32
    register_manifest_compatibility(historical, get_canonical_manifest_hash())
    try:
        assert manifest_hash_is_compatible(historical, profile, wave)
    finally:
        from audapack import campaign as campaign_mod

        campaign_mod._COMPATIBILITY_LEDGER.pop(historical, None)


# --------------------------------------------------------------------------- #
# parse_wave identity gate (v3 / require_identity=True)
# --------------------------------------------------------------------------- #

def test_exact_current_identity_is_accepted():
    valid, meta, err = parse_wave(_compress_wave(), "compress", get_profile("compress"), require_identity=True)
    assert valid, err
    assert meta["profile_id"] == "compress"


def test_missing_profile_is_rejected():
    valid, _meta, err = parse_wave(
        _compress_wave(include_profile=False), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid and "CAMPAIGN_PROFILE" in err


def test_wrong_profile_is_rejected():
    body = _compress_wave().replace("CAMPAIGN_PROFILE: compress", "CAMPAIGN_PROFILE: quick3")
    valid, _meta, err = parse_wave(body, "compress", get_profile("compress"), require_identity=True)
    assert not valid and "CAMPAIGN_PROFILE" in err


def test_missing_version_is_rejected():
    valid, _meta, err = parse_wave(
        _compress_wave(include_version=False), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid and "CAMPAIGN_PROFILE_VERSION" in err


def test_wrong_version_is_rejected():
    valid, _meta, err = parse_wave(
        _compress_wave(profile_version="9.9.9"), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid and "CAMPAIGN_PROFILE_VERSION" in err


def test_missing_manifest_is_rejected():
    valid, _meta, err = parse_wave(
        _compress_wave(include_manifest=False), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid and "CAMPAIGN_MANIFEST_SHA256" in err


def test_arbitrary_manifest_hash_is_rejected():
    valid, _meta, err = parse_wave(
        _compress_wave(manifest="0" * 64), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid and "Manifest hash mismatch" in err


def test_historical_manifest_is_accepted_only_through_the_ledger():
    from audapack import campaign as campaign_mod

    historical = "cd" * 32
    valid, _meta, _err = parse_wave(
        _compress_wave(manifest=historical), "compress", get_profile("compress"), require_identity=True
    )
    assert not valid

    register_manifest_compatibility(historical, get_canonical_manifest_hash())
    try:
        valid, _meta, err = parse_wave(
            _compress_wave(manifest=historical), "compress", get_profile("compress"), require_identity=True
        )
        assert valid, err
    finally:
        campaign_mod._COMPATIBILITY_LEDGER.pop(historical, None)


def test_legacy_parser_still_accepts_a_headerless_wave():
    """The require_identity=False default is the deliberate v1/v2 boundary."""
    valid, _meta, err = parse_wave(
        _compress_wave(include_profile=False, include_version=False, include_manifest=False),
        "compress",
        get_profile("compress"),
    )
    assert valid, err


# --------------------------------------------------------------------------- #
# resolver agreement
# --------------------------------------------------------------------------- #

def _write_campaign_wave(folder: Path, manifest_sha: str) -> Path:
    profile = get_profile("super10")
    wave = profile.waves[0]
    folder.mkdir(parents=True, exist_ok=True)
    content = (
        "PROJECT_NAME: SAIPEN\n"
        "DATE_TIME: 2026-08-27T18:00:00+03:00\n"
        "CAMPAIGN_PROFILE: super10\n"
        f"CAMPAIGN_PROFILE_VERSION: {profile.profile_version}\n"
        "CAMPAIGN_RUN_ID: run-identity\n"
        f"CAMPAIGN_MANIFEST_SHA256: {manifest_sha}\n"
        f"WAVE_ID: {wave.id}\n"
        f"WAVE: {wave.wave_header}\n"
        f"{wave.status_line}\n"
        "TICKETS: 0\n"
        "HANDOFF: IMPLEMENTATION_AGENT\n\n"
        f"{wave.no_findings_marker}\n"
        f"{wave.done_marker} nothing found.\n"
    )
    path = folder / f"SAIPEN__{wave.number}_{wave.slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_resolver_rejects_an_arbitrary_manifest_hash(tmp_path):
    camp = tmp_path / "SAIPEN__CAMP"
    _write_campaign_wave(camp, "0" * 64)
    result = resolve_audit_campaign_entrypoint(camp)
    assert result["status"] == STATUS_CAMPAIGN_MANIFEST_MISMATCH, result


def test_resolver_accepts_the_exact_current_identity(tmp_path):
    camp = tmp_path / "SAIPEN__CAMP"
    profile = get_profile("super10")
    _write_campaign_wave(camp, profile.manifest_hash or get_canonical_manifest_hash())
    result = resolve_audit_campaign_entrypoint(camp)
    assert result["status"] != STATUS_CAMPAIGN_MANIFEST_MISMATCH, result
