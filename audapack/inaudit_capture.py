"""Durable global INAUDIT capture inbox.

The capture store is deliberately filesystem-backed: one Markdown body and one
JSON sidecar per capture.  It owns validation, crash recovery, deterministic
project suggestions, assignment, archive/delete, and conversation affinity.
Browser, Bridge, and desktop UI all use this same implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from audapack.bridge.storage import atomic_write
from audapack.config import AppConfig, cross_process_lock, get_user_runtime_dir
from audapack.models import Project

CAPTURE_SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 5 * 1024 * 1024
MAX_METADATA_TEXT = 4096
MAX_PROJECT_HINTS = 32
MAX_INDEX_FILES = 400
MAX_INDEX_AGE_SECONDS = 24 * 60 * 60
CAPTURE_STATUSES = {"NEW", "SUGGESTED", "ASSIGNED", "ARCHIVED", "DUPLICATE", "RECOVERY"}
CAPTURE_KINDS = {"response", "block", "clipboard", "audit", "roadmap", "handoff", "instructions"}
CLASSIFICATION_STATES = {"STRONG", "SUGGESTED", "UNASSIGNED"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_LAYER_RE = re.compile(r"^[1-9][0-9]*\.md$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_LOCAL_LOCK = threading.RLock()


class InauditCaptureError(RuntimeError):
    """Typed domain failure safe to expose through the loopback API."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_capture_text(text: str) -> str:
    """Normalize only newlines; semantic content remains byte-for-byte otherwise."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def body_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_title(text: str) -> str:
    lines = text.splitlines()
    candidates: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,2}\s+\S", stripped):
            candidates.append(re.sub(r"^#{1,2}\s+", "", stripped))
    if not candidates:
        for line in lines:
            stripped = line.strip().strip("=-")
            if stripped and not stripped.startswith("```") and len(stripped) >= 4:
                candidates.append(stripped)
                break
    title = re.sub(r"\s+", " ", candidates[0] if candidates else "INAUDIT Capture").strip()
    return title[:120] or "INAUDIT Capture"


def valid_source_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else ""


def _bounded_text(value: Any, name: str, *, maximum: int = MAX_METADATA_TEXT) -> str:
    text = str(value or "")
    if _CONTROL_RE.search(text):
        raise InauditCaptureError("invalid_metadata", f"{name} contains control characters")
    if len(text.encode("utf-8")) > maximum:
        raise InauditCaptureError("invalid_metadata", f"{name} exceeds {maximum} bytes")
    return text.strip()


def _safe_uuid(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _UUID_RE.fullmatch(text):
        raise InauditCaptureError("invalid_capture_id", "capture_id must be a canonical UUID")
    return str(uuid.UUID(text))


def _project_tokens(project: Project) -> set[str]:
    values = [
        project.id,
        project.display_name,
        project.audit_project_name,
        Path(project.source_path).name if project.source_path else "",
        *getattr(project, "inaudit_aliases", []),
    ]
    normalized = {str(value).strip() for value in values if value is not None}
    return {value for value in normalized if len(value) >= 3}


def _representative_identity(project: Project) -> tuple[set[str], set[str]]:
    """Bounded shallow identity scan; never walks an enormous tree per capture."""
    if not project.source_path:
        return set(), set()
    root = Path(project.source_path)
    if not root.is_dir():
        return set(), set()
    found: set[str] = set()
    symbols: set[str] = set()
    try:
        for current, dirs, files in os.walk(root):
            relative = Path(current).relative_to(root)
            if len(relative.parts) >= 3:
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs if not name.startswith(".") and name not in {"node_modules", "venv", ".venv"}]
            for name in files:
                if name.startswith("."):
                    continue
                found.add(name)
                if Path(name).suffix.lower() in {".py", ".pyw", ".js", ".ts", ".tsx", ".jsx"} and len(symbols) < 800:
                    try:
                        sample = (Path(current) / name).read_text(encoding="utf-8", errors="ignore")[:65536]
                    except OSError:
                        sample = ""
                    symbols.update(
                        match.group(1)
                        for match in re.finditer(
                            r"(?m)^\s*(?:class|def|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
                            sample,
                        )
                        if len(match.group(1)) >= 4
                    )
                if len(found) >= MAX_INDEX_FILES:
                    return found, symbols
    except OSError:
        return found, symbols
    return found, symbols


class InauditCaptureStore:
    def __init__(self, base_dir: Path | str | None = None):
        runtime = Path(base_dir) if base_dir is not None else get_user_runtime_dir()
        self.root = runtime / "inaudit"
        self.inbox_dir = self.root / "inbox"
        self.archive_dir = self.root / "archive"
        self.recovery_dir = self.root / "recovery"
        self.transactions_dir = self.root / "transactions"
        self.lock_path = self.root / "inaudit.lock"
        self.generation_path = self.root / "inaudit_generation.json"
        self.affinity_path = self.root / "conversation_affinity.json"
        self.index_path = self.root / "project_identity_index.json"
        self._ensure_dirs()
        self.recover_partial_records()
        self.recover_assignment_transactions()

    def _ensure_dirs(self) -> None:
        for path in (self.inbox_dir, self.archive_dir, self.recovery_dir, self.transactions_dir):
            path.mkdir(parents=True, exist_ok=True)

    def _body_path(self, capture_id: str, directory: Path | None = None) -> Path:
        return (directory or self.inbox_dir) / f"{capture_id}.md"

    def _meta_path(self, capture_id: str, directory: Path | None = None) -> Path:
        return (directory or self.inbox_dir) / f"{capture_id}.json"

    @staticmethod
    def _json_text(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        atomic_write(path, self._json_text(value))

    def _signal(self, capture_id: str, event: str) -> None:
        previous = self._read_json(self.generation_path) or {}
        generation = int(previous.get("generation") or 0) + 1
        self._atomic_json(
            self.generation_path,
            {"generation": generation, "capture_id": capture_id, "event": event, "updated_at": utc_now()},
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def recover_partial_records(self) -> list[dict[str, Any]]:
        """Move unmatched/corrupt pairs to recovery and preserve useful bytes."""
        self._ensure_dirs()
        recovered: list[dict[str, Any]] = []
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            ids = {path.stem for path in self.inbox_dir.glob("*.md")} | {path.stem for path in self.inbox_dir.glob("*.json")}
            for raw_id in sorted(ids):
                body = self._body_path(raw_id)
                meta = self._meta_path(raw_id)
                parsed = self._read_json(meta) if meta.is_file() else None
                complete = body.is_file() and parsed is not None and parsed.get("capture_id") == raw_id
                if complete:
                    try:
                        text = body.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        complete = False
                    else:
                        complete = parsed.get("content_sha256") == body_sha256(text)
                if complete:
                    continue
                recovery_id = raw_id if _UUID_RE.fullmatch(raw_id) else str(uuid.uuid4())
                moved_body = self.recovery_dir / f"{recovery_id}.md"
                moved_meta = self.recovery_dir / f"{recovery_id}.json"
                if body.exists():
                    os.replace(body, moved_body)
                if meta.exists():
                    os.replace(meta, self.recovery_dir / f"{recovery_id}.broken.json")
                recovery_meta = {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "capture_id": recovery_id,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "status": "RECOVERY",
                    "capture_kind": "instructions",
                    "source": "recovery",
                    "title": "Recovered partial INAUDIT capture",
                    "content_sha256": body_sha256(moved_body.read_text(encoding="utf-8")) if moved_body.is_file() else "",
                    "recovery_reason": "partial or corrupt body/metadata pair",
                }
                self._atomic_json(moved_meta, recovery_meta)
                recovered.append(recovery_meta)
        return recovered

    def recover_assignment_transactions(self) -> list[str]:
        """Finish or safely discard crash-interrupted assignment journals."""
        completed: list[str] = []
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            for journal_path in sorted(self.transactions_dir.glob("assign-*.json")):
                journal = self._read_json(journal_path)
                if not journal:
                    os.replace(journal_path, self.recovery_dir / f"{journal_path.stem}.broken.json")
                    continue
                capture_id = str(journal.get("capture_id") or "")
                target = Path(str(journal.get("target") or ""))
                temp = Path(str(journal.get("temp") or ""))
                digest = str(journal.get("content_sha256") or "")
                stage = str(journal.get("stage") or "")
                prepared_link = False
                if stage == "prepared" and target.is_file() and temp.is_file():
                    try:
                        prepared_link = os.path.samefile(temp, target)
                    except OSError:
                        prepared_link = False
                if (stage != "published" and not prepared_link) or not target.is_file():
                    if temp.is_file():
                        temp.unlink()
                    journal_path.unlink()
                    continue
                try:
                    target_digest = body_sha256(target.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    target_digest = ""
                meta_path = self._meta_path(capture_id)
                record = self._read_json(meta_path)
                if target_digest != digest or record is None or record.get("content_sha256") != digest:
                    journal["recovery_error"] = "published assignment or Inbox metadata failed digest verification"
                    self._atomic_json(self.recovery_dir / journal_path.name, journal)
                    journal_path.unlink()
                    continue
                record.update(
                    {
                        "status": "ASSIGNED",
                        "updated_at": utc_now(),
                        "assigned_project_id": str(journal.get("project_id") or ""),
                        "assigned_path": str(target.resolve()),
                        "assigned_at": str(journal.get("assigned_at") or utc_now()),
                    }
                )
                self._atomic_json(meta_path, record)
                if temp.is_file():
                    temp.unlink()
                journal_path.unlink()
                self._signal(capture_id, "assign_recovered")
                completed.append(capture_id)
        return completed

    def _load_affinity(self) -> dict[str, dict[str, Any]]:
        raw = self._read_json(self.affinity_path) or {}
        entries = raw.get("conversations", {})
        return entries if isinstance(entries, dict) else {}

    def _save_affinity(self, entries: dict[str, dict[str, Any]]) -> None:
        self._atomic_json(self.affinity_path, {"schema_version": 1, "conversations": entries})

    def _identity_index(self, projects: Iterable[Project]) -> list[dict[str, Any]]:
        project_list = list(projects)
        signature_rows: list[dict[str, Any]] = []
        for project in project_list:
            root_mtime = 0
            if project.source_path:
                try:
                    root_mtime = Path(project.source_path).stat().st_mtime_ns
                except OSError:
                    pass
            signature_rows.append(
                {
                    "project_id": project.id,
                    "display_name": project.display_name,
                    "audit_project_name": project.audit_project_name,
                    "source_path": project.source_path,
                    "aliases": list(getattr(project, "inaudit_aliases", [])),
                    "root_mtime": root_mtime,
                }
            )
        signature = hashlib.sha256(
            json.dumps(signature_rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cached = self._read_json(self.index_path) or {}
        if (
            cached.get("registry_signature") == signature
            and isinstance(cached.get("projects"), list)
            and time.time() - float(cached.get("generated_at_epoch") or 0) < MAX_INDEX_AGE_SECONDS
        ):
            return list(cached["projects"])
        items: list[dict[str, Any]] = []
        for project in project_list:
            root = ""
            try:
                root = str(Path(project.source_path).resolve()) if project.source_path else ""
            except OSError:
                root = project.source_path
            files, symbols = _representative_identity(project)
            items.append(
                {
                    "project_id": project.id,
                    "display_name": project.display_name,
                    "root": root,
                    "aliases": sorted(_project_tokens(project), key=str.casefold),
                    "representative_filenames": sorted(files, key=str.casefold),
                    "representative_symbols": sorted(symbols, key=str.casefold),
                }
            )
        payload = {
            "schema_version": 1,
            "updated_at": utc_now(),
            "generated_at_epoch": time.time(),
            "registry_signature": signature,
            "projects": items,
        }
        self._atomic_json(self.index_path, payload)
        return items

    def classify(
        self,
        text: str,
        projects: Iterable[Project],
        *,
        conversation_fingerprint: str = "",
        project_hints: Iterable[str] = (),
    ) -> dict[str, Any]:
        entries = self._identity_index(projects)
        folded = text.casefold()
        words = {word.casefold() for word in _WORD_RE.findall(text)}
        affinities = self._load_affinity()
        affinity = affinities.get(conversation_fingerprint, {}) if conversation_fingerprint else {}
        hints = {str(value).strip().casefold() for value in project_hints if str(value).strip()}
        scores: list[tuple[float, dict[str, Any], list[str], float]] = []
        filename_owners: dict[str, set[str]] = {}
        symbol_owners: dict[str, set[str]] = {}
        for entry in entries:
            for filename in entry["representative_filenames"]:
                filename_owners.setdefault(filename.casefold(), set()).add(entry["project_id"])
            for symbol in entry.get("representative_symbols", []):
                symbol_owners.setdefault(symbol.casefold(), set()).add(entry["project_id"])
        for entry in entries:
            base = 0.0
            bonus = 0.0
            evidence: list[str] = []
            root = str(entry.get("root") or "")
            if root and root.casefold() in folded:
                base = 1.0
                evidence.append(f'exact path "{root}"')
            for alias in entry["aliases"]:
                alias_fold = alias.casefold()
                if alias_fold in words or (" " in alias_fold and alias_fold in folded):
                    if base < 0.94:
                        base = 0.94
                    evidence.append(f'exact project token "{alias}"')
            for filename in entry["representative_filenames"]:
                fn = filename.casefold()
                if fn in folded and len(filename_owners.get(fn, ())) == 1:
                    base = max(base, 0.86)
                    evidence.append(f'unique file "{filename}"')
                    if len(evidence) >= 6:
                        break
            for symbol in entry.get("representative_symbols", []):
                symbol_fold = symbol.casefold()
                if symbol_fold in words and len(symbol_owners.get(symbol_fold, ())) == 1:
                    base = max(base, 0.88)
                    evidence.append(f'unique symbol "{symbol}"')
                    if len(evidence) >= 6:
                        break
            if entry["project_id"].casefold() in hints or any(alias.casefold() in hints for alias in entry["aliases"]):
                bonus += 0.10
                evidence.append("explicit project hint")
            if affinity.get("last_confirmed_project_id") == entry["project_id"]:
                bonus += min(0.12, 0.04 + 0.02 * int(affinity.get("confirmed_count") or 0))
                evidence.append("confirmed conversation affinity")
            score = min(1.0, base + bonus) if base else min(0.59, bonus)
            scores.append((score, entry, evidence, base))
        scores.sort(key=lambda row: (-row[0], row[1]["project_id"].casefold()))
        if not scores or scores[0][0] < 0.60:
            return {
                "project_id": "",
                "project_name": "",
                "confidence": round(scores[0][0], 3) if scores else 0.0,
                "state": "UNASSIGNED",
                "evidence": scores[0][2] if scores else [],
            }
        top = scores[0]
        if len(scores) > 1 and abs(top[0] - scores[1][0]) < 0.05 and top[3] < 0.94:
            return {"project_id": "", "project_name": "", "confidence": round(top[0], 3), "state": "UNASSIGNED", "evidence": ["ambiguous project evidence"]}
        state = "STRONG" if top[0] >= 0.90 else "SUGGESTED"
        return {
            "project_id": top[1]["project_id"],
            "project_name": top[1]["display_name"],
            "confidence": round(top[0], 3),
            "state": state,
            "evidence": top[2],
        }

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> tuple[str, str, str, list[str]]:
        schema_version = payload.get("schema_version", CAPTURE_SCHEMA_VERSION)
        if schema_version != CAPTURE_SCHEMA_VERSION:
            raise InauditCaptureError(
                "unsupported_schema_version",
                f"schema_version must be {CAPTURE_SCHEMA_VERSION}",
            )
        capture_id = _safe_uuid(payload.get("capture_id"))
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise InauditCaptureError("invalid_capture", "text must be a string")
        text = normalize_capture_text(raw_text)
        if not text.strip():
            raise InauditCaptureError("empty_capture", "capture text is empty")
        if len(text.encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise InauditCaptureError("capture_too_large", f"capture exceeds {MAX_CAPTURE_BYTES} bytes", status=413)
        kind = _bounded_text(payload.get("capture_kind") or "response", "capture_kind", maximum=64).lower()
        if kind not in CAPTURE_KINDS:
            raise InauditCaptureError("invalid_capture_kind", f"unsupported capture_kind: {kind}")
        raw_hints = payload.get("project_hints") or []
        if not isinstance(raw_hints, list) or len(raw_hints) > MAX_PROJECT_HINTS:
            raise InauditCaptureError("invalid_metadata", "project_hints must be a bounded list")
        hints = [_bounded_text(value, "project_hint", maximum=256) for value in raw_hints]
        return capture_id, text, kind, hints

    def capture(self, payload: dict[str, Any], projects: Iterable[Project]) -> dict[str, Any]:
        capture_id, text, kind, hints = self._validate_payload(payload)
        digest = body_sha256(text)
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            body_path = self._body_path(capture_id)
            meta_path = self._meta_path(capture_id)
            if body_path.exists() or meta_path.exists():
                meta = self._read_json(meta_path)
                if meta and body_path.is_file() and meta.get("content_sha256") == digest:
                    return {"record": meta, "duplicate": True, "durable": True}
                raise InauditCaptureError("capture_id_conflict", "capture_id already exists with different content", status=409)
            fingerprint = _bounded_text(payload.get("conversation_fingerprint"), "conversation_fingerprint", maximum=512)
            classification = self.classify(text, projects, conversation_fingerprint=fingerprint, project_hints=hints)
            duplicate_of = ""
            for existing in self.list_records(include_archived=True, include_recovery=False):
                if existing.get("content_sha256") == digest:
                    duplicate_of = str(existing.get("capture_id") or "")
                    break
            now = utc_now()
            status = "DUPLICATE" if duplicate_of else ("SUGGESTED" if classification["project_id"] else "NEW")
            record: dict[str, Any] = {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "capture_id": capture_id,
                "created_at": _bounded_text(payload.get("captured_at"), "captured_at", maximum=64) or now,
                "updated_at": now,
                "status": status,
                "capture_kind": kind,
                "source": _bounded_text(payload.get("source") or "browser", "source", maximum=128),
                "content_sha256": digest,
                "title": extract_title(text),
                "source_url": valid_source_url(_bounded_text(payload.get("source_url"), "source_url")),
                "source_title": _bounded_text(payload.get("source_title"), "source_title"),
                "browser_name": _bounded_text(payload.get("browser_name"), "browser_name", maximum=128),
                "conversation_fingerprint": fingerprint,
                "suggested_project_id": classification["project_id"],
                "suggested_project_name": classification["project_name"],
                "classification_confidence": classification["confidence"],
                "classification_state": classification["state"],
                "classification_evidence": classification["evidence"],
                "assigned_project_id": "",
                "assigned_path": "",
                "assigned_at": "",
                "duplicate_of": duplicate_of,
            }
            try:
                atomic_write(body_path, text)
                if body_path.read_text(encoding="utf-8") != text:
                    raise OSError("body verification failed")
                self._atomic_json(meta_path, record)
                verified = self._read_json(meta_path)
                if verified != record or body_sha256(body_path.read_text(encoding="utf-8")) != digest:
                    raise OSError("capture pair verification failed")
                self._signal(capture_id, "capture")
            except Exception:
                # Preserve any durable half-record for startup recovery.
                raise
            return {"record": record, "duplicate": False, "durable": True}

    def list_records(self, *, include_archived: bool = False, include_recovery: bool = True) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        directories = [self.inbox_dir]
        if include_archived:
            directories.append(self.archive_dir)
        if include_recovery:
            directories.append(self.recovery_dir)
        for directory in directories:
            for path in sorted(directory.glob("*.json")):
                if path.name.endswith(".broken.json"):
                    continue
                value = self._read_json(path)
                if value and value.get("status") in CAPTURE_STATUSES:
                    records.append(value)
        records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("capture_id") or "")), reverse=True)
        return records

    def get(self, capture_id: str) -> dict[str, Any]:
        capture_id = _safe_uuid(capture_id)
        for directory in (self.inbox_dir, self.archive_dir, self.recovery_dir):
            meta = self._read_json(self._meta_path(capture_id, directory))
            body = self._body_path(capture_id, directory)
            if meta is not None:
                text = body.read_text(encoding="utf-8") if body.is_file() else ""
                return {"record": meta, "text": text}
        raise InauditCaptureError("capture_not_found", "capture does not exist", status=404)

    @staticmethod
    def _registered_project(projects: Iterable[Project], project_id: str) -> Project:
        matches = [project for project in projects if project.id == project_id]
        if len(matches) != 1:
            raise InauditCaptureError("unknown_project", "target must be one registered project", status=404)
        project = matches[0]
        if not project.source_path:
            raise InauditCaptureError("invalid_project", "registered project has no source path")
        return project

    @staticmethod
    def _safe_audit_dir(project: Project) -> tuple[Path, Path]:
        root = Path(project.source_path).resolve()
        audit_dir = (root / "audit").resolve()
        try:
            audit_dir.relative_to(root)
        except ValueError as exc:
            raise InauditCaptureError("invalid_project_path", "audit directory escapes project root") from exc
        return root, audit_dir

    def assign(
        self,
        capture_id: str,
        project_id: str,
        projects: Iterable[Project],
        *,
        action: str = "",
        after_assign: Callable[[str, Path], Any] | None = None,
    ) -> dict[str, Any]:
        capture_id = _safe_uuid(capture_id)
        project = self._registered_project(projects, _bounded_text(project_id, "project_id", maximum=256))
        action = action.upper().strip()
        if action not in {"", "GG", "CC"}:
            raise InauditCaptureError("invalid_action", "action must be GG, CC, or empty")
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            current = self.get(capture_id)
            record = dict(current["record"])
            text = current["text"]
            if record.get("status") == "ARCHIVED":
                raise InauditCaptureError("capture_archived", "restore archived capture before assignment", status=409)
            if record.get("assigned_path"):
                existing = Path(str(record["assigned_path"]))
                if existing.is_file() and body_sha256(existing.read_text(encoding="utf-8")) == record.get("content_sha256"):
                    command = f'saipen {action.lower()} "{existing}"' if action else ""
                    if after_assign and action:
                        after_assign(action, existing)
                    return {"record": record, "assigned_path": str(existing), "command": command, "duplicate": True}
                raise InauditCaptureError("assignment_conflict", "record claims an invalid assigned path", status=409)
            _root, audit_dir = self._safe_audit_dir(project)
            audit_dir.mkdir(parents=True, exist_ok=True)
            numbers = [int(path.stem) for path in audit_dir.iterdir() if path.is_file() and _LAYER_RE.fullmatch(path.name)]
            number = max(numbers, default=0) + 1
            temp = audit_dir / f".inaudit-{capture_id}.tmp"
            atomic_write(temp, text)
            if body_sha256(temp.read_text(encoding="utf-8")) != record.get("content_sha256"):
                temp.unlink(missing_ok=True)
                raise InauditCaptureError("assignment_verify_failed", "temporary assigned layer digest does not match capture")
            journal_path = self.transactions_dir / f"assign-{capture_id}.json"
            assigned_at = utc_now()
            target: Path | None = None
            while target is None:
                candidate = audit_dir / f"{number}.md"
                journal = {
                    "schema_version": 1,
                    "capture_id": capture_id,
                    "project_id": project.id,
                    "content_sha256": record.get("content_sha256"),
                    "temp": str(temp.resolve()),
                    "target": str(candidate.resolve()),
                    "stage": "prepared",
                    "assigned_at": assigned_at,
                }
                self._atomic_json(journal_path, journal)
                try:
                    os.link(temp, candidate)
                except FileExistsError:
                    number += 1
                    continue
                except OSError:
                    # Cross-volume or unsupported hardlink; fall back to a
                    # verified content copy so the assignment still lands
                    # without leaving the journal to be silently discarded
                    # on the next recovery pass.
                    try:
                        shutil.copyfile(temp, candidate)
                    except OSError as copy_exc:
                        if copy_exc.errno != 17:
                            raise InauditCaptureError(
                                "assignment_publish_failed",
                                f"could not hardlink or copy capture to {candidate}: {copy_exc}",
                            ) from copy_exc
                        number += 1
                        continue
                target = candidate.resolve()
                journal["stage"] = "published"
                journal["target"] = str(target)
                self._atomic_json(journal_path, journal)
            temp.unlink(missing_ok=True)
            digest = body_sha256(target.read_text(encoding="utf-8"))
            if digest != record.get("content_sha256"):
                target.unlink(missing_ok=True)
                journal_path.unlink(missing_ok=True)
                raise InauditCaptureError("assignment_verify_failed", "assigned layer digest does not match capture")
            previous = dict(record)
            record.update(
                {
                    "status": "ASSIGNED",
                    "updated_at": utc_now(),
                    "assigned_project_id": project.id,
                    "assigned_path": str(target),
                    "assigned_at": assigned_at,
                }
            )
            try:
                self._atomic_json(self._meta_path(capture_id), record)
                if self._read_json(self._meta_path(capture_id)) != record:
                    raise OSError("assignment metadata verification failed")
            except Exception:
                if target.is_file() and body_sha256(target.read_text(encoding="utf-8")) == digest:
                    target.unlink()
                self._atomic_json(self._meta_path(capture_id), previous)
                journal_path.unlink(missing_ok=True)
                raise
            fingerprint = str(record.get("conversation_fingerprint") or "")
            if fingerprint:
                affinities = self._load_affinity()
                affinity = affinities.get(fingerprint, {})
                count = int(affinity.get("confirmed_count") or 0) + 1
                affinities[fingerprint] = {
                    "last_confirmed_project_id": project.id,
                    "confirmed_count": count,
                    "updated_at": utc_now(),
                }
                self._save_affinity(affinities)
            self._signal(capture_id, "assign")
            journal_path.unlink(missing_ok=True)
            command = f'saipen {action.lower()} "{target}"' if action else ""
            if after_assign and action:
                after_assign(action, target)
            return {"record": record, "assigned_path": str(target), "command": command, "duplicate": False}

    def archive(self, capture_id: str) -> dict[str, Any]:
        return self._move_to_status(capture_id, self.archive_dir, "ARCHIVED", "archive")

    def restore(self, capture_id: str) -> dict[str, Any]:
        return self._move_to_status(capture_id, self.inbox_dir, "NEW", "restore", source=self.archive_dir)

    def _move_to_status(
        self,
        capture_id: str,
        destination: Path,
        status: str,
        event: str,
        *,
        source: Path | None = None,
    ) -> dict[str, Any]:
        capture_id = _safe_uuid(capture_id)
        source = source or self.inbox_dir
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            body = self._body_path(capture_id, source)
            meta = self._meta_path(capture_id, source)
            record = self._read_json(meta)
            if record is None or not body.is_file():
                raise InauditCaptureError("capture_not_found", "capture does not exist", status=404)
            record["status"] = status
            record["updated_at"] = utc_now()
            destination.mkdir(parents=True, exist_ok=True)
            self._atomic_json(meta, record)
            os.replace(body, self._body_path(capture_id, destination))
            os.replace(meta, self._meta_path(capture_id, destination))
            self._signal(capture_id, event)
            return record

    def delete(self, capture_id: str) -> None:
        capture_id = _safe_uuid(capture_id)
        with _LOCAL_LOCK, cross_process_lock(self.lock_path):
            found = False
            for directory in (self.inbox_dir, self.archive_dir, self.recovery_dir):
                for path in (self._body_path(capture_id, directory), self._meta_path(capture_id, directory)):
                    if path.is_file():
                        path.unlink()
                        found = True
            if not found:
                raise InauditCaptureError("capture_not_found", "capture does not exist", status=404)
            self._signal(capture_id, "delete")


def store_for_config(config: AppConfig, base_dir: Path | str | None = None) -> InauditCaptureStore:
    del config  # reserved for future store policy settings; root ownership is runtime-scoped.
    return InauditCaptureStore(base_dir=base_dir)
