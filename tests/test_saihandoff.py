from datetime import datetime
from pathlib import Path

import pytest

import audapack.saihandoff as saihandoff_module
from audapack.saihandoff import (
    SAIHandoffError,
    SAIHandoffTitleError,
    canonical_handoff_filename,
    canonical_handoff_title,
    canonicalize_handoff_body,
    format_saihandoff_v1,
    handoff_content_sha256,
    is_numeric_saipen_audit_filename,
    normalized_legacy_title,
    parse_canonical_handoff_title,
    parse_saihandoff_v1,
    validate_handoff_title_identity,
    validate_saihandoff_v1,
)


def test_canonical_title_is_project_first_and_preserves_spelling():
    assert canonical_handoff_title("SAITULS", "T-144") == "SAITULS — SAIHANDOFF — T-144"
    assert canonical_handoff_title("FastPrompter", "P0-SOUND") == (
        "FastPrompter — SAIHANDOFF — P0-SOUND"
    )


def test_title_separator_is_the_exact_literal_protocol_string():
    assert saihandoff_module.TITLE_SEPARATOR == " — "


def test_parser_returns_machine_identity_without_case_normalization():
    parsed = parse_canonical_handoff_title("SaItUlS — SAIHANDOFF — T-144")
    assert parsed.project_name == "SaItUlS"
    assert parsed.topic == "T-144"
    assert parsed.text == "SaItUlS — SAIHANDOFF — T-144"


@pytest.mark.parametrize(
    "legacy",
    [
        "SAIHANDOFF_BEGIN project=SAITULS",
        "[SAIPEN_HANDOFF] project=SAITULS",
        "<!-- SAIHANDOFF-BEGIN project=SAITULS -->",
        "[SAIHANDOFF] project: SAITULS",
        "SAIHANDOFF_V1 PROJECT_NAME: SAITULS",
    ],
)
def test_historical_and_prefix_first_forms_are_noncanonical(legacy):
    with pytest.raises(SAIHandoffTitleError) as error:
        parse_canonical_handoff_title(legacy)
    assert error.value.code == "noncanonical_title"


def test_title_identity_conflict_fails_closed():
    with pytest.raises(SAIHandoffTitleError) as error:
        validate_handoff_title_identity(
            "SAITULS — SAIHANDOFF — T-144",
            project_name="AUDAPACK",
            topic="T-144",
        )
    assert error.value.code == "title_identity_conflict"

    with pytest.raises(SAIHandoffTitleError) as error:
        validate_handoff_title_identity(
            "SAITULS — SAIHANDOFF — T-144",
            project_name="SAITULS",
            topic="T-185",
        )
    assert error.value.code == "title_identity_conflict"


def test_title_builder_rejects_ambiguous_or_unbounded_fields():
    for project, topic in (("", "T-1"), ("SAITULS — other", "T-1"), ("SAITULS", "")):
        with pytest.raises(SAIHandoffTitleError):
            canonical_handoff_title(project, topic)
    with pytest.raises(SAIHandoffTitleError):
        canonical_handoff_title("SAITULS", "x" * 97)


def test_legacy_normalization_requires_explicit_machine_identity():
    assert normalized_legacy_title("SAITULS", "T-144") == "SAITULS — SAIHANDOFF — T-144"
    with pytest.raises(SAIHandoffTitleError) as error:
        normalized_legacy_title(None, "T-144")
    assert error.value.code == "ambiguous_legacy_identity"


def test_named_filename_is_project_first_and_numeric_audit_names_are_untouched():
    created = datetime(2026, 9, 10, 18, 30)
    assert canonical_handoff_filename("SAITULS", "T-144", created) == (
        "SAITULS — SAIHANDOFF — T-144_20260910_1830.md"
    )
    assert is_numeric_saipen_audit_filename("1.md")
    assert is_numeric_saipen_audit_filename("12.md")
    assert not is_numeric_saipen_audit_filename("0.md")
    assert not is_numeric_saipen_audit_filename("SAITULS — SAIHANDOFF — T-144.md")


def test_title_parser_does_not_trim_or_guess_display_identity():
    with pytest.raises(SAIHandoffTitleError):
        parse_canonical_handoff_title(" SAITULS — SAIHANDOFF — T-144")
    with pytest.raises(SAIHandoffTitleError):
        parse_canonical_handoff_title("SAITULS — SAIHANDOFF — T-144 ")


# ---------------------------------------------------------------------------
# SAIHANDOFF_V1 Envelope, Body Canonicalization, and Validator Tests
# ---------------------------------------------------------------------------


def test_v1_envelope_roundtrip_and_validation():
    body = "# Audit Report\nAll tests passed.\n"
    rendered = format_saihandoff_v1(
        handoff_id="hand-001",
        project_id="audapack",
        project_name="AUDAPACK",
        topic="T-186",
        body=body,
        kind="implementation",
        role="implementation",
        target_policy="reuse",
        delivery="manual",
    )
    record = parse_saihandoff_v1(rendered)
    assert record.version == "SAIHANDOFF_V1"
    assert record.handoff_id == "hand-001"
    assert record.project_id == "audapack"
    assert record.project_name == "AUDAPACK"
    assert record.topic == "T-186"
    assert record.kind == "implementation"
    assert record.role == "implementation"
    assert record.target_policy == "reuse"
    assert record.delivery == "manual"
    assert record.body == canonicalize_handoff_body(body)
    assert record.content_sha256 == handoff_content_sha256(body)

    # Calling validate_saihandoff_v1 on valid record succeeds
    validate_saihandoff_v1(record)


def test_v1_envelope_crlf_lf_canonicalization():
    lf_body = "Line 1\nLine 2\n"
    crlf_body = "Line 1\r\nLine 2\r\n\r\n"
    sha_lf = handoff_content_sha256(lf_body)
    sha_crlf = handoff_content_sha256(crlf_body)
    assert sha_lf == sha_crlf

    rendered_crlf = (
        "AUDAPACK — SAIHANDOFF — M1\r\n"
        "SAIHANDOFF_V1\r\n"
        "HANDOFF_ID: h-1\r\n"
        "PROJECT_ID: audapack\r\n"
        "PROJECT_NAME: AUDAPACK\r\n"
        "TOPIC: M1\r\n"
        "KIND: implementation\r\n"
        "ROLE: implementation\r\n"
        "TARGET_POLICY: reuse\r\n"
        "DELIVERY: manual\r\n"
        f"CONTENT_SHA256: {sha_crlf}\r\n"
        "END_HEADER\r\n"
        "\r\n"
        f"{crlf_body}"
    )
    parsed = parse_saihandoff_v1(rendered_crlf)
    assert parsed.body == "Line 1\nLine 2\n"
    assert parsed.content_sha256 == sha_lf


def test_v1_envelope_duplicate_header_fails_closed():
    body = "Test body\n"
    sha = handoff_content_sha256(body)
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        "TOPIC: M2\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        f"CONTENT_SHA256: {sha}\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "duplicate_field"


@pytest.mark.parametrize(
    "missing_key",
    [
        "HANDOFF_ID",
        "PROJECT_ID",
        "PROJECT_NAME",
        "TOPIC",
        "KIND",
        "ROLE",
        "TARGET_POLICY",
        "DELIVERY",
        "CONTENT_SHA256",
    ],
)
def test_v1_envelope_missing_required_headers_fail_closed(missing_key):
    body = "Test body\n"
    sha = handoff_content_sha256(body)
    headers = {
        "HANDOFF_ID": "h-1",
        "PROJECT_ID": "audapack",
        "PROJECT_NAME": "AUDAPACK",
        "TOPIC": "M1",
        "KIND": "implementation",
        "ROLE": "implementation",
        "TARGET_POLICY": "reuse",
        "DELIVERY": "manual",
        "CONTENT_SHA256": sha,
    }
    del headers[missing_key]
    lines = [
        "AUDAPACK — SAIHANDOFF — M1",
        "SAIHANDOFF_V1",
        *[f"{k}: {v}" for k, v in headers.items()],
        "END_HEADER",
        "",
        body,
    ]
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1("\n".join(lines))
    assert err.value.code == "missing_field"


def test_v1_envelope_missing_end_header_fails_closed():
    body = "Test body\n"
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        f"CONTENT_SHA256: {handoff_content_sha256(body)}\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "missing_field"


def test_v1_envelope_unsupported_version_fails_closed():
    body = "Test body\n"
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V2\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        f"CONTENT_SHA256: {handoff_content_sha256(body)}\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "unsupported_version"


def test_v1_envelope_invalid_project_id_fails_closed():
    body = "Test body\n"
    with pytest.raises(SAIHandoffError) as err:
        format_saihandoff_v1(
            handoff_id="h-1",
            project_id="bad id with spaces!",
            project_name="AUDAPACK",
            topic="M1",
            body=body,
        )
    assert err.value.code == "invalid_project_id"


def test_v1_envelope_invalid_sha256_fails_closed():
    body = "Test body\n"
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        "CONTENT_SHA256: not-a-valid-sha-digest\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "invalid_sha256"


def test_v1_envelope_empty_body_fails_closed():
    with pytest.raises(SAIHandoffError) as err:
        canonicalize_handoff_body("   \n\n  ")
    assert err.value.code == "empty_body"

    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        "CONTENT_SHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        "END_HEADER\n\n"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "empty_body"


def test_v1_envelope_content_sha_mismatch_fails_closed():
    body = "Actual body content\n"
    wrong_sha = "0" * 64
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M1\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        f"CONTENT_SHA256: {wrong_sha}\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "content_sha_mismatch"


def test_v1_envelope_title_header_identity_mismatch_fails_closed():
    body = "Test body\n"
    sha = handoff_content_sha256(body)
    # Title topic M1 vs header topic M2
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: AUDAPACK\n"
        "TOPIC: M2\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        f"CONTENT_SHA256: {sha}\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "title_identity_conflict"


def test_v1_envelope_project_header_mismatch_fails_closed():
    body = "Test body\n"
    envelope = (
        "AUDAPACK — SAIHANDOFF — M1\n"
        "SAIHANDOFF_V1\n"
        "HANDOFF_ID: h-1\n"
        "PROJECT_ID: audapack\n"
        "PROJECT_NAME: OTHER\n"
        "TOPIC: M1\n"
        "KIND: implementation\n"
        "ROLE: implementation\n"
        "TARGET_POLICY: reuse\n"
        "DELIVERY: manual\n"
        f"CONTENT_SHA256: {handoff_content_sha256(body)}\n"
        "END_HEADER\n\n"
        f"{body}"
    )
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(envelope)
    assert err.value.code == "title_identity_conflict"


def test_same_handoff_id_requires_identical_content():
    kwargs = {
        "handoff_id": "hand-001",
        "project_id": "audapack",
        "project_name": "AUDAPACK",
        "topic": "T-186",
    }
    original = parse_saihandoff_v1(format_saihandoff_v1(**kwargs, body="Original\n"))
    identical = parse_saihandoff_v1(format_saihandoff_v1(**kwargs, body="Original\n"))
    incompatible = parse_saihandoff_v1(format_saihandoff_v1(**kwargs, body="Replacement\n"))
    other_id = parse_saihandoff_v1(
        format_saihandoff_v1(**{**kwargs, "handoff_id": "hand-002"}, body="Original\n")
    )

    assert saihandoff_module.handoff_id_content_compatible(original, identical) is True
    assert saihandoff_module.handoff_id_content_compatible(original, incompatible) is False
    assert saihandoff_module.handoff_id_content_compatible(original, other_id) is False


def test_v1_envelope_ordinary_prose_fails_closed():
    prose = "Hey model, please follow the SAIHANDOFF specification for AUDAPACK!"
    with pytest.raises(SAIHandoffError) as err:
        parse_saihandoff_v1(prose)
    assert err.value.code in ("missing_field", "noncanonical_title")


def test_single_protocol_authority_enforced():
    """Exactly one authority may define the SAIHANDOFF_V1 machine envelope.

    The authority is ``audapack.saihandoff``. The hazard this guards is a SECOND,
    hand-maintained definition drifting from it -- a prose document or a
    leftover implementation bundle that spells the header block out
    independently and is then trusted by an operator. So the invariant is
    negative: nothing in the tree may restate the envelope, anywhere.

    This previously asserted a root document that exists in no commit of this
    repository, so the guard could never pass and proved nothing; the real
    invariant was untested while the declared gate stayed red.
    """
    repo_root = Path(__file__).resolve().parent.parent

    assert saihandoff_module.REQUIRED_HEADERS, "the module is the authority and must declare the envelope"

    matches = []
    for md_file in repo_root.rglob("*.md"):
        # .saipen/ is SAIPEN's own machine memory (intake receipts, journals),
        # not documentation an operator would read as the envelope definition.
        if {".git", ".saipen", "node_modules"} & set(md_file.parts):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "HANDOFF_ID:" in content:
            matches.append(str(md_file.relative_to(repo_root)))

    assert matches == [], (
        f"only audapack/saihandoff.py may define the envelope; also defined in: {matches}"
    )
