"""Canonical SAIHANDOFF display identity and protocol helpers.

This module owns the stable human-title contract and the pure executable
SAIHANDOFF_V1 envelope parser, validator, hasher, and canonical serializer.
It deliberately contains no browser DOM, persistence, routing, or Bridge queue logic.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

TITLE_MARKER = "SAIHANDOFF"
TITLE_SEPARATOR = " — "
MAX_TITLE_LENGTH = 240
MAX_TOPIC_LENGTH = 96

PROTOCOL_VERSION = "SAIHANDOFF_V1"
VALID_DELIVERY = frozenset({"manual", "auto_fill", "auto_submit"})
REQUIRED_HEADERS = (
    "HANDOFF_ID",
    "PROJECT_ID",
    "PROJECT_NAME",
    "TOPIC",
    "KIND",
    "ROLE",
    "TARGET_POLICY",
    "DELIVERY",
    "CONTENT_SHA256",
)

_TITLE_RE = re.compile(
    rf"^(?P<project>[^\r\n]+){re.escape(TITLE_SEPARATOR)}"
    rf"{re.escape(TITLE_MARKER)}{re.escape(TITLE_SEPARATOR)}"
    rf"(?P<topic>[^\r\n]+)$"
)
_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SAIHandoffError(ValueError):
    """A supplied SAIHANDOFF envelope or title violates protocol invariants."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


SAIHandoffTitleError = SAIHandoffError


@dataclass(frozen=True)
class SAIHandoffTitle:
    """Parsed canonical display identity, preserving project/topic spelling."""

    project_name: str
    topic: str

    @property
    def text(self) -> str:
        return canonical_handoff_title(self.project_name, self.topic)


@dataclass(frozen=True)
class SAIHandoffRecord:
    """Parsed and validated canonical SAIHANDOFF_V1 record."""

    title: SAIHandoffTitle
    version: str
    handoff_id: str
    project_id: str
    project_name: str
    topic: str
    kind: str
    role: str
    target_policy: str
    delivery: str
    content_sha256: str
    body: str


def handoff_id_content_compatible(
    left: SAIHandoffRecord,
    right: SAIHandoffRecord,
) -> bool:
    """Return whether two records reuse one HANDOFF_ID for identical content."""
    return (
        isinstance(left, SAIHandoffRecord)
        and isinstance(right, SAIHandoffRecord)
        and left.handoff_id == right.handoff_id
        and left == right
    )


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise SAIHandoffError("invalid_title_identity", f"{field} must be text")
    value = value.strip()
    if not value:
        raise SAIHandoffError("invalid_title_identity", f"{field} must not be empty")
    if any(char in value for char in "\r\n"):
        raise SAIHandoffError("invalid_title_identity", f"{field} must be one line")
    return value


def canonical_handoff_title(project_name: str, topic: str) -> str:
    """Return the sole canonical human-visible SAIHANDOFF title."""
    project = _required_text(project_name, "PROJECT_NAME")
    top = _required_text(topic, "TOPIC")
    if TITLE_SEPARATOR in project:
        raise SAIHandoffError(
            "invalid_title_identity",
            "PROJECT_NAME contains the title separator",
        )
    if TITLE_SEPARATOR in top:
        raise SAIHandoffError(
            "invalid_title_identity",
            "TOPIC contains the title separator",
        )
    if len(top) > MAX_TOPIC_LENGTH:
        raise SAIHandoffError("invalid_title_identity", "TOPIC is too long")
    title = f"{project}{TITLE_SEPARATOR}{TITLE_MARKER}{TITLE_SEPARATOR}{top}"
    if len(title) > MAX_TITLE_LENGTH:
        raise SAIHandoffError("invalid_title_identity", "SAIHANDOFF title is too long")
    return title


def parse_canonical_handoff_title(title: str) -> SAIHandoffTitle:
    """Parse one canonical title; legacy/decorative forms fail closed."""
    if not isinstance(title, str):
        raise SAIHandoffError("invalid_title_identity", "title must be text")
    if title != title.strip():
        raise SAIHandoffError(
            "noncanonical_title",
            "title must not have leading or trailing whitespace",
        )
    match = _TITLE_RE.fullmatch(title)
    if not match:
        raise SAIHandoffError(
            "noncanonical_title",
            "title must be '<PROJECT_NAME> — SAIHANDOFF — <TOPIC>'",
        )
    project = _required_text(match.group("project"), "PROJECT_NAME")
    topic = _required_text(match.group("topic"), "TOPIC")
    canonical_handoff_title(project, topic)
    return SAIHandoffTitle(project_name=project, topic=topic)


def validate_handoff_title_identity(
    title: str,
    project_name: str,
    topic: str,
) -> SAIHandoffTitle:
    """Validate presentation against machine fields, failing closed on conflict."""
    parsed = parse_canonical_handoff_title(title)
    expected_project = _required_text(project_name, "PROJECT_NAME")
    expected_topic = _required_text(topic, "TOPIC")
    if parsed.project_name != expected_project or parsed.topic != expected_topic:
        raise SAIHandoffError(
            "title_identity_conflict",
            "display title identity does not match machine PROJECT_NAME/TOPIC",
        )
    return parsed


def canonical_handoff_filename(
    project_name: str,
    topic: str,
    created_at: datetime,
) -> str:
    """Return the project-first named artifact filename outside numeric inboxes."""
    if not isinstance(created_at, datetime):
        raise SAIHandoffError("invalid_title_identity", "created_at must be a datetime")
    title = canonical_handoff_title(project_name, topic)
    stamp = created_at.strftime("%Y%m%d_%H%M")
    return f"{title}_{stamp}.md"


def is_numeric_saipen_audit_filename(path: str | Path) -> bool:
    """Recognize unchanged numeric SAIPEN audit-layer names such as ``12.md``."""
    return bool(re.fullmatch(r"[1-9][0-9]*\.md", Path(path).name))


def normalized_legacy_title(project_name: Optional[str], topic: Optional[str]) -> str:
    """Normalize known legacy metadata only when ownership is already explicit.

    This helper intentionally does not accept a legacy marker as proof of
    ownership. Callers must supply unambiguous machine metadata; missing fields
    fail closed instead of guessing from prose.
    """
    if not project_name or not topic:
        raise SAIHandoffError(
            "ambiguous_legacy_identity",
            "legacy handoff lacks unambiguous PROJECT_NAME/TOPIC metadata",
        )
    return canonical_handoff_title(project_name, topic)


def canonicalize_handoff_body(text: str) -> str:
    """Deterministically normalize handoff body text: CRLF->LF and single trailing LF."""
    if not isinstance(text, str):
        raise SAIHandoffError("empty_body", "body must be text")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise SAIHandoffError("empty_body", "body must not be empty")
    return normalized.rstrip("\n") + "\n"


def handoff_content_sha256(body: str) -> str:
    """Compute the lowercase hex SHA-256 digest over the canonicalized body only."""
    canonical = canonicalize_handoff_body(body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest().lower()


def format_saihandoff_v1(
    *,
    handoff_id: str,
    project_id: str,
    project_name: str,
    topic: str,
    body: str,
    kind: str = "implementation",
    role: str = "implementation",
    target_policy: str = "reuse",
    delivery: str = "manual",
) -> str:
    """Serialize canonical SAIHANDOFF_V1 text representation."""
    title = canonical_handoff_title(project_name, topic)
    canonical_body = canonicalize_handoff_body(body)
    sha = handoff_content_sha256(canonical_body)
    if delivery not in VALID_DELIVERY:
        raise SAIHandoffError("missing_field", f"invalid delivery: {delivery}")
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise SAIHandoffError("invalid_project_id", f"invalid project_id: {project_id}")

    lines = [
        title,
        PROTOCOL_VERSION,
        f"HANDOFF_ID: {handoff_id}",
        f"PROJECT_ID: {project_id}",
        f"PROJECT_NAME: {project_name}",
        f"TOPIC: {topic}",
        f"KIND: {kind}",
        f"ROLE: {role}",
        f"TARGET_POLICY: {target_policy}",
        f"DELIVERY: {delivery}",
        f"CONTENT_SHA256: {sha}",
        "END_HEADER",
        "",
        canonical_body,
    ]
    return "\n".join(lines[:-1]) + lines[-1]


def parse_saihandoff_v1(text: str) -> SAIHandoffRecord:
    """Parse and validate one canonical SAIHANDOFF_V1 envelope, failing closed."""
    if not isinstance(text, str):
        raise SAIHandoffError("noncanonical_title", "handoff envelope must be text")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    if "\nEND_HEADER\n" in normalized:
        header_part, body_part = normalized.split("\nEND_HEADER\n", 1)
    elif normalized.endswith("\nEND_HEADER"):
        header_part = normalized[: -len("\nEND_HEADER")]
        body_part = ""
    else:
        raise SAIHandoffError("missing_field", "END_HEADER marker missing")

    header_lines = header_part.split("\n")
    if len(header_lines) < 2:
        raise SAIHandoffError("noncanonical_title", "header block is too short")

    # Line 0: display title
    title = parse_canonical_handoff_title(header_lines[0])

    # Line 1: version marker
    version = header_lines[1].strip()
    if version != PROTOCOL_VERSION:
        raise SAIHandoffError(
            "unsupported_version",
            f"unsupported envelope version: {version}",
        )

    headers: dict[str, str] = {}
    for line in header_lines[2:]:
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise SAIHandoffError("missing_field", f"malformed header line: {line}")
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if key in headers:
            raise SAIHandoffError("duplicate_field", f"duplicate header: {key}")
        headers[key] = val

    for key in REQUIRED_HEADERS:
        if key not in headers or not headers[key]:
            raise SAIHandoffError("missing_field", f"missing required header: {key}")

    if title.project_name != headers["PROJECT_NAME"] or title.topic != headers["TOPIC"]:
        raise SAIHandoffError(
            "title_identity_conflict",
            "display title does not match machine PROJECT_NAME/TOPIC",
        )

    project_id = headers["PROJECT_ID"]
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise SAIHandoffError("invalid_project_id", f"invalid project_id: {project_id}")

    sha = headers["CONTENT_SHA256"]
    if not _SHA256_RE.fullmatch(sha) or sha != sha.lower():
        raise SAIHandoffError("invalid_sha256", f"invalid CONTENT_SHA256: {sha}")

    delivery = headers["DELIVERY"]
    if delivery not in VALID_DELIVERY:
        raise SAIHandoffError("missing_field", f"invalid delivery: {delivery}")

    if body_part.startswith("\n"):
        body_part = body_part[1:]
    if not body_part.strip():
        raise SAIHandoffError("empty_body", "handoff body must not be empty")

    canonical_body = canonicalize_handoff_body(body_part)
    computed_sha = handoff_content_sha256(canonical_body)
    if computed_sha != sha:
        raise SAIHandoffError(
            "content_sha_mismatch",
            f"CONTENT_SHA256 mismatch: expected {sha}, got {computed_sha}",
        )

    record = SAIHandoffRecord(
        title=title,
        version=version,
        handoff_id=headers["HANDOFF_ID"],
        project_id=project_id,
        project_name=headers["PROJECT_NAME"],
        topic=headers["TOPIC"],
        kind=headers["KIND"],
        role=headers["ROLE"],
        target_policy=headers["TARGET_POLICY"],
        delivery=delivery,
        content_sha256=sha,
        body=canonical_body,
    )
    return record


def validate_saihandoff_v1(record: SAIHandoffRecord) -> None:
    """Validate all invariants of an existing SAIHandoffRecord."""
    if not isinstance(record, SAIHandoffRecord):
        raise SAIHandoffError("missing_field", "record must be a SAIHandoffRecord")
    if record.version != PROTOCOL_VERSION:
        raise SAIHandoffError("unsupported_version", f"unsupported version: {record.version}")
    if not record.handoff_id or not record.handoff_id.strip():
        raise SAIHandoffError("missing_field", "HANDOFF_ID must not be empty")
    if not _PROJECT_ID_RE.fullmatch(record.project_id):
        raise SAIHandoffError("invalid_project_id", f"invalid project_id: {record.project_id}")
    if not record.project_name or not record.project_name.strip():
        raise SAIHandoffError("missing_field", "PROJECT_NAME must not be empty")
    if not record.topic or not record.topic.strip() or len(record.topic) > MAX_TOPIC_LENGTH:
        raise SAIHandoffError("missing_field", "TOPIC must not be empty or too long")
    if (
        record.title.project_name != record.project_name
        or record.title.topic != record.topic
    ):
        raise SAIHandoffError("title_identity_conflict", "title does not match record fields")
    if not _SHA256_RE.fullmatch(record.content_sha256) or record.content_sha256 != record.content_sha256.lower():
        raise SAIHandoffError("invalid_sha256", f"invalid sha256: {record.content_sha256}")
    if record.delivery not in VALID_DELIVERY:
        raise SAIHandoffError("missing_field", f"invalid delivery: {record.delivery}")
    if not record.body or not record.body.strip():
        raise SAIHandoffError("empty_body", "body must not be empty")
    computed_sha = handoff_content_sha256(record.body)
    if computed_sha != record.content_sha256:
        raise SAIHandoffError("content_sha_mismatch", "body SHA256 does not match record digest")
