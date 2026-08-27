r"""Configuration management, schema validation, and migration for AUDAPACK.

Ensures user runtime and secrets are isolated to %LOCALAPPDATA%\AUDAPACK,
never leaving mutable tokens or state in the source repository.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # Win32 byte-range locks (stdlib, Windows only)
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

try:  # POSIX advisory locks
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from audapack.models import CANONICAL_GROUPS, SLOTS_PER_GROUP, PriorityGroup, Project

CONFIG_FILE_NAME = "config.json"
LEGACY_REPO_CONFIG_NAME = "audapack.json"
SCHEMA_VERSION = 2

DEFAULT_AUDIT_ROOT = r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\__TO_AUDIT\AUDITING_IMPLEMENTATION"

MANDATORY_EXCLUDES = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pycx",
    ".pytest_cache",
    ".workbuddy-ai",
    "*.pre-redact",
    "*.secret",
    "*.secrets",
    "token.txt",
    "*.token",
    "*.pid",
    "secrets",
    "_AUDAPACK_MANIFEST.json",
]

DEFAULT_EXCLUDES = [
    ".claude",
    ".codenomad",
    ".freebuff",
    ".saiwork",
    "node_modules",
    "dist",
    "build",
    "target",
    ".cargo",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "*.egg-info",
    ".venv",
    ".build-venv",
    "logs",
    "*.log",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.zip",
    "*.part",
    "*.tmp",
    "token.txt",
    "*.token",
    "*.pid",
    "secrets",
    "*.pre-redact",
    "*.secret",
    "*.secrets",
]

DEFAULT_PROJECT_TEMPLATES = [
    # MAIN0
    {"name": "FastPrompter", "path": r"v:\___VAC\__K\__CODE\_PY\_FastPrompter", "group": "MAIN0", "slot": 1},
    {"name": "SAIAUDIT", "path": r"v:\___VAC\__K\__CODE\_PY\_SAIAUDIT", "group": "MAIN0", "slot": 2},
    {"name": "SAIPEN", "path": r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_SAIPEN", "group": "MAIN0", "slot": 3},
    {"name": "SAIPET", "path": r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_SAIPET", "group": "MAIN0", "slot": 4},
    {"name": "SAIWORK2", "path": r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_SAIWORK2", "group": "MAIN0", "slot": 5},
    {"name": "saipenview", "path": r"v:\___VAC\__K\__CODE\_PY\_SAIPENVIEW", "group": "MAIN0", "slot": 6},
    # MAIN1
    {"name": "SAIPLAN", "path": r"v:\___VAC\__K\__CODE\_PY\_SAIPLAN", "group": "MAIN1", "slot": 1},
    {"name": "SAITALK", "path": r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_SAITALK", "group": "MAIN1", "slot": 2},
    {"name": "Smart VAC Cleaner", "path": r"v:\___VAC\__K\__CODE\_PY\_SMART_VAC_CLEANER", "group": "MAIN1", "slot": 3},
    {"name": "Video Downloader (_yt_cm)", "path": r"v:\___VAC\__K\__CODE\_PY\_yt_cm", "group": "MAIN1", "slot": 4},
    {"name": "Wintage", "path": r"v:\___VAC\__K\__CODE\_TAMPERMONKEY\_WIN95THEME\Wintage", "group": "MAIN1", "slot": 5},
    {"name": "_PR Video Random Cut", "path": r"v:\___VAC\__K\__CODE\_PY\_PR_Video_Random_Cut", "group": "MAIN1", "slot": 6},
    # SIDE0
    {"name": "VACZEN Calendar (CalendarTask)", "path": r"v:\___VAC\__K\__CODE\_PY\_VACZEN_CALENDAR", "group": "SIDE0", "slot": 1},
    {"name": "VacWPlayer", "path": r"v:\___VAC\__K\__CODE\_PY\_VacWPlayer", "group": "SIDE0", "slot": 2},
    {"name": "SAISENT", "path": r"v:\___VAC\__K\__CODE\_PY\_SAISENT", "group": "SIDE0", "slot": 3},
    # SIDE1
    {"name": "TERMISAI", "path": r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\__TERMISAI", "group": "SIDE1", "slot": 1},
]


def app_dir() -> Path:
    """Return root directory of AUDAPACK source installation."""
    return Path(__file__).resolve().parent.parent


def get_user_runtime_dir() -> Path:
    r"""Returns canonical %LOCALAPPDATA%\AUDAPACK directory, respecting AUDAPACK_RUNTIME_DIR."""
    test_env = os.environ.get("AUDAPACK_RUNTIME_DIR")
    if test_env:
        target = Path(test_env)
        target.mkdir(parents=True, exist_ok=True)
        return target

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / ".local" / "share"
    target = base / "AUDAPACK"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_config_dir() -> Path:
    p = get_user_runtime_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_state_dir() -> Path:
    p = get_user_runtime_dir() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_secrets_dir() -> Path:
    p = get_user_runtime_dir() / "secrets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_logs_dir() -> Path:
    p = get_user_runtime_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_bridge_runtime_dir() -> Path:
    p = get_user_runtime_dir() / "bridge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file_path() -> Path:
    r"""Returns canonical path to configuration file in %LOCALAPPDATA%\AUDAPACK\config\config.json."""
    return get_config_dir() / CONFIG_FILE_NAME


def config_path(base_dir: Optional[Path] = None) -> Path:
    """Returns configuration file path, supporting custom base_dir for testing."""
    if base_dir:
        base_dir = Path(base_dir)
        primary = base_dir / CONFIG_FILE_NAME
        if primary.exists():
            return primary
        fallback_json = base_dir / "audapack.json"
        if fallback_json.exists():
            return fallback_json
        fallback_old = base_dir / "pack_all_audit_gui.json"
        if fallback_old.exists():
            return fallback_old
        fallback = base_dir / "config" / CONFIG_FILE_NAME
        if fallback.exists():
            return fallback
        return primary
    return config_file_path()


def safe_slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", text.strip().lower())
    return re.sub(r"_+", "_", cleaned).strip("_") or "project"


def normalize_native_path(value: str) -> str:
    r"""Return ``value`` with OS-native separators and collapsed redundancy.

    ``tkinter.filedialog.askdirectory`` returns forward slashes on Windows
    (``V:/___VAC/...``); legacy JSON may carry either style. We always persist
    the platform-native form (``V:\___VAC\...``) so a path round-trips
    identically across sessions instead of flipping slashes on every save.
    """
    if not value:
        return value
    try:
        norm = os.path.normpath(value)
    except (OSError, ValueError):
        return value
    return norm


REGISTRY_LOCK_NAME = "registry.lock"
_REGISTRY_LOCK_TIMEOUT = 15.0


def get_registry_lock_path(base_dir: Optional[Path] = None) -> Path:
    """Canonical cross-process registry lock file location."""
    if base_dir:
        return Path(base_dir) / REGISTRY_LOCK_NAME
    return get_config_dir() / REGISTRY_LOCK_NAME


class _CrossProcessLock:
    """Advisory exclusive lock usable across processes on Windows and POSIX.

    Windows: msvcrt.locking with retry until timeout. POSIX: fcntl.flock.
    Fallback everywhere else: O_CREAT|O_EXCL sentinel spin with stale break.
    """

    def __init__(self, path: Path, timeout: float = _REGISTRY_LOCK_TIMEOUT):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_lock():
                return self
            if time.monotonic() >= deadline:
                self._fh.close()
                self._fh = None
                raise TimeoutError(f"registry lock busy: {self.path}")
            time.sleep(0.05)

    def _try_lock(self) -> bool:
        if sys.platform == "win32" and msvcrt is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        if fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        # Last-resort sentinel spin (single lock byte via read/rewrite)
        try:
            self._fh.seek(0)
            if self._fh.read(1):
                return False
            self._fh.seek(0)
            self._fh.write(b"L")
            self._fh.flush()
            return True
        except OSError:
            return False

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh is not None:
                if sys.platform == "win32" and msvcrt is not None:
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                elif fcntl is not None:
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                else:
                    try:
                        self._fh.seek(0)
                        self._fh.truncate(0)
                        self._fh.flush()
                    except OSError:
                        pass
                self._fh.close()
        finally:
            self._fh = None
        return False


@contextmanager
def cross_process_lock(path: Path, timeout: float = _REGISTRY_LOCK_TIMEOUT):
    """Context manager around _CrossProcessLock (see there for semantics)."""
    lock = _CrossProcessLock(path, timeout=timeout)
    with lock:
        yield


# Output layout modes (CORE-009 / T-26):
#   single_folder      -- every archive is written to PackingConfig.output_dir
#                         (or the app runtime dir if empty). This is the legacy
#                         single-folder behaviour.
#   alongside_projects -- each archive is written as a SIBLING of its project
#                         folder, i.e. to source_path.parent. The archive is
#                         NOT placed inside the project (which would make it
#                         a self-referential pack, W2-003). This keeps each
#                         project's archive next to the project on disk.
OUTPUT_LAYOUT_SINGLE_FOLDER = "single_folder"
OUTPUT_LAYOUT_ALONGSIDE_PROJECTS = "alongside_projects"
OUTPUT_LAYOUT_CHOICES = (
    OUTPUT_LAYOUT_SINGLE_FOLDER,
    OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
)
DEFAULT_OUTPUT_LAYOUT = OUTPUT_LAYOUT_SINGLE_FOLDER


def normalize_output_layout(value: object) -> str:
    """Coerce a persisted/imported output_layout value to a known enum member.

    Unknown / missing / empty values fall back to the legacy single-folder
    behaviour so an older config.json keeps working unchanged.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in OUTPUT_LAYOUT_CHOICES:
            return v
    return DEFAULT_OUTPUT_LAYOUT


@dataclass
class PackingConfig:
    output_dir: str = ""
    delete_old: bool = True
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    manifest_enabled: bool = True
    # CORE-009: archive output layout. Legacy configs without this field are
    # treated as single_folder (the old behaviour) so migration is a no-op.
    output_layout: str = DEFAULT_OUTPUT_LAYOUT


@dataclass
class AuditsConfig:
    root: str = DEFAULT_AUDIT_ROOT
    hot_seconds: int = 6 * 3600           # <= 6 hours
    warm_seconds: int = 24 * 3600         # <= 24 hours
    cool_seconds: int = 72 * 3600         # <= 72 hours
    cold_seconds: int = 7 * 86400         # <= 7 days


_LOOPBACK_HOST_ALIASES = {"127.0.0.1", "::1", "localhost"}


def normalize_bridge_host(value: str) -> str:
    """Reject every non-loopback Bridge bind target (CORE-002).

    A local control service must never bind to ``0.0.0.0``, ``::``, a LAN IP, or
    an externally resolving hostname. Anything unsafe is coerced to the canonical
    loopback ``127.0.0.1`` rather than silently exposing the service.
    """
    host = (value or "").strip().lower()
    if host in _LOOPBACK_HOST_ALIASES:
        return "127.0.0.1" if host in ("127.0.0.1", "localhost") else "::1"
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback:
            return host
    except ValueError:
        pass
    logging.warning("Bridge host %r is not loopback; coercing to 127.0.0.1", value)
    return "127.0.0.1"


@dataclass
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 17843
    token: str = ""
    autostart: bool = True
    max_request_bytes: int = 10 * 1024 * 1024
    history_retention_days: int = 30

    def to_safe_dict(self) -> dict[str, Any]:
        """Serializable view for portable config: never contains the production token.

        The token lives only in %LOCALAPPDATA%\\AUDAPACK\\secrets\\token.txt; ``token``
        stays an in-memory runtime field filled by ensure_token().
        """
        return {
            "host": self.host,
            "port": self.port,
            "autostart": self.autostart,
            "max_request_bytes": self.max_request_bytes,
            "history_retention_days": self.history_retention_days,
        }


@dataclass
class UIConfig:
    window_size: list[int] = field(default_factory=lambda: [760, 680])
    reply_language: str = "et"
    ui_language: str = "ru"  # UI label language: 'ru' (default) or 'en'
    preferred_browser: str = ""  # Path to preferred browser executable (optional)


@dataclass
class AppConfig:
    schema_version: int = SCHEMA_VERSION
    initialized: bool = False
    projects: list[Project] = field(default_factory=list)
    packing: PackingConfig = field(default_factory=PackingConfig)
    audits: AuditsConfig = field(default_factory=AuditsConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    def normalize_paths(self) -> bool:
        """Normalize every persisted path string to OS-native separators in place.

        Returns True when any value actually changed (so callers can decide to
        re-save). Covers project source/archive paths and global path fields.
        """
        changed = False

        out = normalize_native_path(self.packing.output_dir)
        if out != self.packing.output_dir:
            self.packing.output_dir = out
            changed = True

        root = normalize_native_path(self.audits.root)
        if root != self.audits.root:
            self.audits.root = root
            changed = True

        browser = normalize_native_path(self.ui.preferred_browser)
        if browser != self.ui.preferred_browser:
            self.ui.preferred_browser = browser
            changed = True

        for p in self.projects:
            src = normalize_native_path(p.source_path)
            if src != p.source_path:
                p.source_path = src
                changed = True
            arc = normalize_native_path(p.last_copied_archive_path)
            if arc != p.last_copied_archive_path:
                p.last_copied_archive_path = arc
                changed = True

        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "initialized": self.initialized,
            "projects": [p.to_dict() for p in self.projects],
            "packing": asdict(self.packing),
            "audits": asdict(self.audits),
            "bridge": self.bridge.to_safe_dict(),
            "ui": asdict(self.ui),
        }

    def get_project_by_id(self, project_id: str) -> Optional[Project]:
        p_id = project_id.strip().lower()
        for p in self.projects:
            if p.id.lower() == p_id:
                return p
        return None

    def get_project_by_name_or_audit(self, name: str) -> Optional[Project]:
        target = name.strip().lower()
        for p in self.projects:
            if p.display_name.strip().lower() == target or (p.audit_project_name and p.audit_project_name.strip().lower() == target):
                return p
        return None

    def get_project_in_slot(self, group: str, slot: int) -> Optional[Project]:
        for p in self.projects:
            if p.priority_group.upper() == group.upper() and p.slot == slot:
                return p
        return None


def get_token_file_path() -> Path:
    return get_secrets_dir() / "token.txt"


LEGACY_ACCEPTANCE_MARKER_NAME = "legacy_token_acceptance.revoked"


def legacy_token_acceptance_revoked() -> bool:
    """True once verified takeover/rotation retired the migration-scoped legacy tokens."""
    return (get_secrets_dir() / LEGACY_ACCEPTANCE_MARKER_NAME).exists()


def revoke_legacy_token_acceptance() -> bool:
    """Writes the marker that stops migration-scoped legacy token acceptance. Returns True on success."""
    try:
        marker = get_secrets_dir() / LEGACY_ACCEPTANCE_MARKER_NAME
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("legacy ACBBridge token acceptance revoked\n", encoding="utf-8")
        return True
    except Exception:
        return False


def redact_legacy_source_config(source_path: Path) -> bool:
    """Removes bridge.token from a migrated legacy source config file in place.

    The runtime copy is already verified when this runs; the source file stays as a
    safe reference but must not remain a valid production secret copy.
    """
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
        changed = False
        if isinstance(data.get("bridge"), dict) and data["bridge"].get("token"):
            data["bridge"]["token"] = ""
            changed = True
        if isinstance(data.get("bridge_token"), str) and data["bridge_token"]:
            data["bridge_token"] = ""
            changed = True
        if not changed:
            return True
        source_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def ensure_token(bridge_cfg: BridgeConfig, base_dir: Optional[Path] = None) -> str:
    """
    Ensures bridge token is generated and persisted under secrets/token.txt.
    Migrates legacy token from repository root if present.
    """
    if base_dir:
        token_file = Path(base_dir) / "token.txt"
    else:
        token_file = get_token_file_path()

    # Check for legacy repo token to migrate
    repo_token_file = app_dir() / "token.txt"
    if repo_token_file.exists() and not base_dir:
        try:
            tok = repo_token_file.read_text(encoding="utf-8").strip()
            if tok:
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(tok, encoding="utf-8")
                bridge_cfg.token = tok
                try:
                    repo_token_file.unlink()
                except OSError:
                    pass
                return tok
        except Exception:
            pass

    if token_file.exists():
        try:
            tok = token_file.read_text(encoding="utf-8").strip()
            if tok and len(tok) >= 16:
                bridge_cfg.token = tok
                return tok
        except Exception:
            pass

    if bridge_cfg.token and len(bridge_cfg.token) >= 16:
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(bridge_cfg.token, encoding="utf-8")
        except Exception:
            pass
        return bridge_cfg.token

    new_token = secrets.token_urlsafe(32)
    bridge_cfg.token = new_token
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(new_token, encoding="utf-8")
    except Exception:
        pass
    return new_token


KNOWN_SEARCH_ROOTS = [
    Path(r"v:\___VAC\__K\__CODE\_PY"),
    Path(r"v:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC"),
    Path(r"v:\___VAC\__K\__CODE\_TAMPERMONKEY\_WIN95THEME"),
    Path(r"v:\___VAC\__K\__CODE\_TAMPERMONKEY"),
]


def normalize_lookup_key(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[\s_\-]+", "", text.strip().lower())


def auto_heal_project_path(name: str, given_path: str = "") -> str:
    """Discovers or repairs a project source_path using disk lookup across known roots."""
    given_str = str(given_path or "").strip()
    if given_str:
        try:
            gp = Path(given_str)
            if gp.exists() and gp.is_dir():
                return str(gp)
            parent = gp.parent
            if parent.exists() and parent.is_dir():
                target_k = normalize_lookup_key(gp.name)
                for child in parent.iterdir():
                    if child.is_dir() and normalize_lookup_key(child.name) == target_k:
                        return str(child)
        except Exception:
            pass

    if not name:
        return given_str

    target_norm = normalize_lookup_key(name)
    for root in KNOWN_SEARCH_ROOTS:
        try:
            if not root.exists() or not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and normalize_lookup_key(child.name) == target_norm:
                    return str(child)
        except Exception:
            pass

    return given_str


def create_default_projects() -> list[Project]:
    projects: list[Project] = []
    for tmpl in DEFAULT_PROJECT_TEMPLATES:
        p_id = safe_slug(tmpl["name"])
        healed = auto_heal_project_path(tmpl["name"], tmpl["path"])
        projects.append(
            Project(
                id=p_id,
                display_name=tmpl["name"],
                source_path=healed,
                enabled=True,
                priority_group=tmpl["group"],
                slot=tmpl["slot"],
                archive_name=tmpl["name"],
                audit_project_name=tmpl["name"],
            )
        )
    return projects


def migrate_legacy_data(data: dict[str, Any]) -> AppConfig:
    """Migrates schema v1 / legacy GUI data format into AppConfig."""
    projects: list[Project] = []
    if "repos" in data and isinstance(data["repos"], list):
        for idx, r in enumerate(data["repos"]):
            name = str(r.get("name", f"Proj_{idx+1}"))
            p_id = safe_slug(name)
            projects.append(
                Project(
                    id=p_id,
                    display_name=name,
                    source_path=str(r.get("path", "")),
                    enabled=bool(r.get("enabled", True)),
                    priority_group="MAIN0" if idx < 6 else "MAIN1",
                    slot=(idx % 6) + 1,
                    archive_name=name,
                    audit_project_name=name,
                )
            )
    elif "projects" in data and isinstance(data["projects"], list):
        for p_data in data["projects"]:
            if isinstance(p_data, dict):
                p_id = str(p_data.get("id", "")).strip() or safe_slug(str(p_data.get("display_name", "proj")))
                projects.append(
                    Project(
                        id=p_id,
                        display_name=str(p_data.get("display_name", "Untitled")),
                        source_path=str(p_data.get("source_path", "")),
                        enabled=bool(p_data.get("enabled", True)),
                        ignored=bool(p_data.get("ignored", False)),
                        priority_group=str(p_data.get("priority_group", "MAIN0")),
                        slot=int(p_data.get("slot", 1)),
                        archive_name=str(p_data.get("archive_name", "")),
                        audit_project_name=str(p_data.get("audit_project_name", "")),
                        last_pack_time=str(p_data.get("last_pack_time", "")),
                        last_package_hash=str(p_data.get("last_package_hash", "")),
                        last_copied_audit_hash=str(p_data.get("last_copied_audit_hash", "")),
                        last_copied_at=str(p_data.get("last_copied_at", "")),
                        last_copied_archive_path=str(p_data.get("last_copied_archive_path", "")),
                        last_copied_archive_at=str(p_data.get("last_copied_archive_at", "")),
                    )
                )

    packing_raw = data.get("packing", {})
    packing_cfg = PackingConfig(
        output_dir=str(packing_raw.get("output_dir") or data.get("output_dir", "")),
        delete_old=bool(packing_raw.get("delete_old", data.get("delete_old", True))),
        excludes=list(packing_raw.get("excludes", data.get("excludes", DEFAULT_EXCLUDES))),
        manifest_enabled=bool(packing_raw.get("manifest_enabled", data.get("manifest_enabled", True))),
        output_layout=normalize_output_layout(packing_raw.get("output_layout", DEFAULT_OUTPUT_LAYOUT)),
    )

    audits_raw = data.get("audits", {})
    audits_cfg = AuditsConfig(
        root=str(audits_raw.get("root") or data.get("audit_root", DEFAULT_AUDIT_ROOT)),
        hot_seconds=int(audits_raw.get("hot_seconds", 6 * 3600)),
        warm_seconds=int(audits_raw.get("warm_seconds", 24 * 3600)),
        cool_seconds=int(audits_raw.get("cool_seconds", 72 * 3600)),
        cold_seconds=int(audits_raw.get("cold_seconds", 7 * 86400)),
    )

    bridge_raw = data.get("bridge", {})
    bridge_cfg = BridgeConfig(
        host=normalize_bridge_host(str(bridge_raw.get("host", "127.0.0.1"))),
        port=int(bridge_raw.get("port", 17843)),
        token=str(bridge_raw.get("token") or data.get("bridge_token", "")),
        autostart=bool(bridge_raw.get("autostart", True)),
        max_request_bytes=int(bridge_raw.get("max_request_bytes", 10 * 1024 * 1024)),
        history_retention_days=int(bridge_raw.get("history_retention_days", 30)),
    )

    ui_raw = data.get("ui", {})
    ui_cfg = UIConfig(
        window_size=list(ui_raw.get("window_size", data.get("window_size", [760, 680]))),
        reply_language=str(ui_raw.get("reply_language", "et")),
        ui_language=str(ui_raw.get("ui_language", "ru")).lower().strip() or "ru",
        preferred_browser=str(ui_raw.get("preferred_browser", "")),
    )

    return AppConfig(
        schema_version=SCHEMA_VERSION,
        projects=projects,
        packing=packing_cfg,
        audits=audits_cfg,
        bridge=bridge_cfg,
        ui=ui_cfg,
    )


def load_config(base_dir: Optional[Path] = None) -> AppConfig:
    r"""Loads configuration from user runtime directory (%LOCALAPPDATA%\AUDAPACK\config\config.json)."""
    cfg_file = config_path(base_dir)

    # Auto-migration from legacy repo location if needed
    if not cfg_file.exists() and not base_dir:
        legacy_repo_json = app_dir() / LEGACY_REPO_CONFIG_NAME
        legacy_old_json = app_dir() / "pack_all_audit_gui.json"
        source_legacy = legacy_repo_json if legacy_repo_json.exists() else (legacy_old_json if legacy_old_json.exists() else None)
        if source_legacy:
            try:
                raw = source_legacy.read_text(encoding="utf-8")
                data = json.loads(raw)
                cfg = migrate_legacy_data(data)
                ensure_token(cfg.bridge)
                if save_config(cfg):
                    # Runtime copy verified; the source file must not keep a live secret.
                    redact_legacy_source_config(source_legacy)
                return cfg
            except Exception:
                pass

        if not cfg_file.exists():
            cfg = AppConfig()
            cfg.projects = create_default_projects()
            ensure_token(cfg.bridge, base_dir)
            save_config(cfg, base_dir)
            return cfg

    if not cfg_file.exists():
        cfg = AppConfig()
        cfg.projects = create_default_projects()
        ensure_token(cfg.bridge, base_dir)
        save_config(cfg, base_dir)
        return cfg

    try:
        raw = cfg_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        legacy_token_present = (
            (isinstance(data.get("bridge"), dict) and bool(data["bridge"].get("token")))
            or bool(str(data.get("bridge_token") or "").strip())
        )
        cfg = migrate_legacy_data(data)
        ensure_token(cfg.bridge, base_dir)
        # CORE-008: a saved registry with projects (or an explicit marker) is an
        # intentional/user-curated state and must never be auto-slimmed/resurrected.
        cfg.initialized = bool(data.get("initialized", len(data.get("projects") or []) > 0))

        # Heal any paths that point to missing or un-prefixed folder names
        healed_any = False
        for p in cfg.projects:
            healed = auto_heal_project_path(p.display_name, p.source_path)
            if healed and healed != p.source_path:
                p.source_path = healed
                healed_any = True

        # Guarantee native separators on every loaded path (reliable round-trip)
        if cfg.normalize_paths():
            healed_any = True

        # Resilient recovery: if projects list is empty in production, restore from backup or defaults
        if not cfg.projects and not base_dir:
            backup_file = cfg_file.with_name("config.backup_latest.json")
            bak2 = cfg_file.with_name("config.json.bak")
            recovered = False
            for bk in [backup_file, bak2]:
                if bk.exists():
                    try:
                        bk_data = json.loads(bk.read_text(encoding="utf-8"))
                        bk_cfg = migrate_legacy_data(bk_data)
                        if bk_cfg.projects:
                            cfg.projects = bk_cfg.projects
                            recovered = True
                            save_config(cfg, base_dir)
                            break
                    except Exception:
                        pass
            if not recovered and not cfg.projects:
                cfg.projects = create_default_projects()
                save_config(cfg, base_dir)
        elif not base_dir and not cfg.initialized:
            # CORE-008: only merge default templates for a never-initialized
            # config (fresh install / genuine corruption). An intentional sparse
            # registry (even 0 projects) must stay exactly as the user saved it.
            # If user has sparse projects, merge default templates
            existing_names = {normalize_lookup_key(p.display_name) for p in cfg.projects}
            occupied = {(p.priority_group.upper(), p.slot) for p in cfg.projects}
            for tmpl in DEFAULT_PROJECT_TEMPLATES:
                if normalize_lookup_key(tmpl["name"]) not in existing_names:
                    grp = tmpl["group"]
                    s = tmpl["slot"]
                    if (grp, s) not in occupied:
                        healed_p = auto_heal_project_path(tmpl["name"], tmpl["path"])
                        cfg.projects.append(
                            Project(
                                id=safe_slug(tmpl["name"]),
                                display_name=tmpl["name"],
                                source_path=healed_p,
                                enabled=True,
                                priority_group=grp,
                                slot=s,
                                archive_name=tmpl["name"],
                                audit_project_name=tmpl["name"],
                            )
                        )
                        occupied.add((grp, s))
                        healed_any = True
            if healed_any:
                save_config(cfg, base_dir)
        elif healed_any and not base_dir:
            save_config(cfg, base_dir)

        if legacy_token_present:
            # One-time sanitize: the secret now lives in canonical secret storage;
            # rewrite the portable config without it and never re-import stale
            # project-local token state on later launches.
            save_config(cfg, base_dir)
        return cfg
    except Exception as exc:
        if not base_dir:
            for bk in [cfg_file.with_name("config.backup_latest.json"), cfg_file.with_name("config.json.bak")]:
                if bk.exists():
                    try:
                        bk_data = json.loads(bk.read_text(encoding="utf-8"))
                        cfg = migrate_legacy_data(bk_data)
                        ensure_token(cfg.bridge, base_dir)
                        save_config(cfg, base_dir)
                        return cfg
                    except Exception:
                        pass
        raise ValueError(f"Corrupted configuration file '{cfg_file}': {exc}") from exc


def save_config(config: AppConfig, base_dir: Optional[Path] = None) -> bool:
    """Atomically saves configuration to disk with durable backup preservation."""
    cfg_file = config_path(base_dir)
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = cfg_file.with_name(f"{cfg_file.name}.tmp.{os.getpid()}")

    try:
        # Pre-save backup of healthy existing configuration (for user runtime)
        if base_dir is None and cfg_file.exists():
            try:
                raw_existing = cfg_file.read_text(encoding="utf-8")
                existing_data = json.loads(raw_existing)
                if existing_data.get("projects"):
                    bak_file = cfg_file.with_name("config.backup_latest.json")
                    bak_file.write_text(raw_existing, encoding="utf-8")
                    bak_file2 = cfg_file.with_name("config.json.bak")
                    bak_file2.write_text(raw_existing, encoding="utf-8")
            except Exception:
                pass

        # Normalize paths before persisting so the on-disk form always uses
        # native separators, independent of where the value originated
        # (file dialog, manual paste, legacy migration).
        config.normalize_paths()
        config.initialized = True

        data = config.to_dict()
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_file.replace(cfg_file)

        # Update backup if current save has projects
        if base_dir is None and data.get("projects"):
            try:
                bak_file = cfg_file.with_name("config.backup_latest.json")
                bak_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except Exception:
                pass

        return True
    except Exception:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        return False
