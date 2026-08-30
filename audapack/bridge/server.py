"""AUDAPACK Loopback HTTP Bridge server implementation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from audapack import __version__
from audapack.bridge.lifecycle import INSTANCE_NONCE, remove_pid, write_pid
from audapack.bridge.state import (
    GenerationPersistenceError,
    RunStateCorruptionError,
    RunStatePersistenceError,
    get_run_state,
    run_transaction,
    save_run_state,
)
from audapack.bridge.storage import (
    InvalidProjectPathError,
    atomic_write,
    capture_file_snapshots,
    generate_canonical_campaign,
    parse_wave,
    resolve_project_audit_dir,
    restore_file_snapshots,
)
from audapack.campaign import (
    STATUS_CAMPAIGN_COMPLETE,
    STATUS_CAMPAIGN_READY_FOR_WAVE,
    get_canonical_manifest_hash,
    get_profile,
    load_profiles,
    save_live_campaign_index,
)
from audapack.components.widget import get_bundled_widget_path
from audapack.config import AppConfig, legacy_token_acceptance_revoked, load_config, normalize_bridge_host
from audapack.projects import ProjectRegistry, RegistrySaveError

logger = logging.getLogger("audapack.bridge")

# Canonical API contract version. Advertised in /health; supports v2 and v3.
BRIDGE_API_VERSION = 3
SUPPORTED_API_VERSIONS = (2, 3)

# Global callback for notifying UI of new audits or auto-registered projects
_ON_AUDIT_WRITTEN: Optional[Callable[[str, str], None]] = None


def set_audit_written_callback(cb: Optional[Callable[[str, str], None]]):
    global _ON_AUDIT_WRITTEN
    _ON_AUDIT_WRITTEN = cb


def _write_final_artifacts(prof, synth_result, target_dir, history_dir, dt_str, state, resolved_name):
    """Synthesizes and durably writes the canonical campaign final artifacts.

    Raises on any write failure so the caller can roll back. History side is
    written before the canonical latest so a partial failure never leaves the
    authoritative file mutated without durable state agreeing.
    """
    if prof.profile_id == "quick3":
        all3_content = synth_result.get("all3", "")
        all3_latest = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
        all3_hist = history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md"
        atomic_write(all3_hist, all3_content)
        atomic_write(all3_latest, all3_content)
        state["all3_complete"] = True
        state["all3_path"] = str(all3_latest)
    else:
        super_all = synth_result.get("super_all", "")
        super_final = synth_result.get("super_final", "")
        super_index = synth_result.get("super_index", "")
        all_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md"
        final_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md"
        index_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json"
        all_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL__{dt_str}.md"
        final_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL__{dt_str}.md"
        index_hist = history_dir / "manifest.json"
        atomic_write(all_hist, super_all)
        atomic_write(all_latest, super_all)
        atomic_write(final_hist, super_final)
        atomic_write(final_latest, super_final)
        atomic_write(index_hist, super_index)
        atomic_write(index_latest, super_index)
        state["campaign_complete"] = True
        state["final_handoff_path"] = str(final_latest)
        state["canonical_campaign_path"] = str(all_latest)


def _get_final_handoff_path(prof, state) -> Optional[Path]:
    if prof.profile_id == "quick3":
        return Path(state["all3_path"]) if state.get("all3_path") else None
    return Path(state["final_handoff_path"]) if state.get("final_handoff_path") else None


def _get_canonical_path(prof, state) -> Optional[Path]:
    if prof.profile_id == "quick3":
        return Path(state["all3_path"]) if state.get("all3_path") else None
    return Path(state["canonical_campaign_path"]) if state.get("canonical_campaign_path") else None


class AudapackBridgeHandler(BaseHTTPRequestHandler):
    config: AppConfig

    def log_message(self, format: str, *args):
        # Override to prevent default console spam; use logger
        pass

    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin")
        if origin:
            return origin
        return "null"

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ACB-Token, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "false")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ACB-Token, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "false")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # W2-001: explicit base directory for test isolation. When set on the
    # handler (or its subclass), registry and run-state operations use this
    # directory instead of the canonical %LOCALAPPDATA% path. Production
    # callers must NEVER set this; the port number must NOT be used as a
    # proxy for test isolation.
    test_base_dir: Optional[Path] = None

    def get_custom_base_dir(self) -> Optional[Path]:
        return self.test_base_dir

    def get_live_config(self) -> AppConfig:
        if self.test_base_dir:
            return self.config
        try:
            cfg = load_config()
            if cfg and cfg.audits and cfg.audits.root:
                return cfg
        except Exception:
            pass
        return self.config

    def _legacy_token_candidates(self) -> list[Path]:
        if legacy_token_acceptance_revoked():
            return []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return []
        base = Path(local_app_data)
        return [
            base / "ACBBridge" / "token.txt",
            base / "AUDAPACK" / "migration_backup" / "ACBBridge" / "token.txt",
        ]

    def check_auth(self) -> bool:
        token = self.headers.get("X-ACB-Token")
        if not token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()

        if not token:
            self.send_json(403, {"ok": False, "error": {"code": "invalid_auth", "message": "Authentication failed: token missing", "retriable": False}})
            return False

        valid_tokens = set()
        if self.config and self.config.bridge and self.config.bridge.token:
            valid_tokens.add(self.config.bridge.token)
        try:
            live = load_config()
            if live and live.bridge and live.bridge.token:
                valid_tokens.add(live.bridge.token)
        except Exception:
            pass

        for candidate_path in self._legacy_token_candidates():
            if candidate_path.exists():
                try:
                    c_tok = candidate_path.read_text(encoding="utf-8").strip()
                    if c_tok and len(c_tok) >= 16:
                        valid_tokens.add(c_tok)
                except Exception:
                    pass

        for exp in valid_tokens:
            if secrets.compare_digest(token, exp):
                return True

        self.send_json(403, {"ok": False, "error": {"code": "invalid_auth", "message": "Authentication failed: invalid token", "retriable": False}})
        return False

    def is_valid_loopback_host(self) -> bool:
        host_hdr = self.headers.get("Host", "")
        host = host_hdr.split(":")[0].strip().lower()
        return host in ["127.0.0.1", "localhost", "::1", "[::1]"]

    def _peer_is_loopback(self) -> bool:
        try:
            peer = self.client_address[0].split("%")[0].strip()
            return ipaddress.ip_address(peer).is_loopback
        except (ValueError, AttributeError, TypeError):
            return False

    def do_GET(self):
        if not self.is_valid_loopback_host() or not self._peer_is_loopback():
            self.send_response(400)
            self.end_headers()
            return

        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/v1/health"):
            live_cfg = self.get_live_config()
            profs = load_profiles()
            self.send_json(200, {
                "ok": True,
                "service": "AUDAPACK Bridge",
                "version": __version__,
                "api_version": BRIDGE_API_VERSION,
                "supported_api_versions": list(SUPPORTED_API_VERSIONS),
                "profiles": list(profs.keys()),
                "manifest_hash": get_canonical_manifest_hash(),
                "instance_id": f"audapack_{os.getpid()}",
                "instance_nonce": INSTANCE_NONCE,
                "registry_revision": len(live_cfg.projects),
            })
        elif parsed.path == "/widget.user.js":
            w_path = get_bundled_widget_path()
            if w_path.exists():
                content = w_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", self._cors_origin())
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_json(404, {"ok": False, "error": "Widget resource not found"})
        elif parsed.path == "/v1/status":
            if not self.check_auth():
                return
            live_cfg = self.get_live_config()
            out_root = Path(live_cfg.audits.root)
            out_exists = out_root.exists()
            out_writable = False
            if out_exists:
                try:
                    test_file = out_root / f".test_{os.getpid()}"
                    test_file.touch()
                    test_file.unlink()
                    out_writable = True
                except Exception:
                    pass

            self.send_json(200, {
                "ok": True,
                "version": __version__,
                "pid": os.getpid(),
                "output_root": str(out_root),
                "output_exists": out_exists,
                "output_writable": out_writable,
            })
        elif parsed.path in ["/v1/projects", "/v1/registry"]:
            if not self.check_auth():
                return
            live_cfg = self.get_live_config()
            registry = ProjectRegistry(live_cfg)
            active_groups = registry.get_active_groups()
            self.send_json(200, {
                "ok": True,
                "revision": int(time.time()),
                "groups": active_groups,
                "projects": [
                    {
                        "project_id": p.id,
                        "display_name": p.display_name,
                        "audit_name": p.audit_project_name or p.display_name,
                        "group": p.priority_group,
                        "slot": p.slot,
                        "enabled": p.enabled,
                    }
                    for p in live_cfg.projects
                ],
            })
        elif parsed.path == "/v1/profiles":
            if not self.check_auth():
                return
            profs = load_profiles()
            self.send_json(200, {
                "ok": True,
                "manifest_hash": get_canonical_manifest_hash(),
                "profiles": {
                    pid: p.to_dict() for pid, p in profs.items()
                },
            })
        else:
            self.send_json(404, {"ok": False, "error": "Endpoint not found"})

    def do_POST(self):
        if not self.is_valid_loopback_host() or not self._peer_is_loopback():
            self.send_response(400)
            self.end_headers()
            return

        if not self.check_auth():
            return

        parsed = urlparse(self.path)
        if parsed.path == "/v1/shutdown":
            self.send_json(200, {"ok": True, "message": "Shutting down bridge"})
            threading.Thread(target=self.server.shutdown).start()
            return

        if parsed.path in ["/v1/projects/resolve", "/v1/projects"]:
            self.handle_project_resolve()
            return

        if parsed.path == "/v1/audits":
            self.handle_audit_submission()
            return

        self.send_json(404, {"ok": False, "error": "Endpoint not found"})

    def _read_json_body(self) -> Optional[dict]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "Content-Length must be a non-negative integer", "retriable": False}})
            return None
        if length < 0:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "Content-Length must be non-negative", "retriable": False}})
            return None
        max_bytes = int(getattr(self.config.bridge, "max_request_bytes", 10 * 1024 * 1024))
        if length > max_bytes:
            self.send_json(413, {"ok": False, "error": {"code": "payload_too_large", "retriable": False}})
            return None
        try:
            self.connection.settimeout(5.0)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("request body ended before Content-Length")
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data
        except Exception:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_json", "retriable": False}})
            return None

    def handle_project_resolve(self):
        data = self._read_json_body()
        if data is None:
            return

        raw_name = str(data.get("project_name") or data.get("name") or data.get("project_id") or "").strip()
        if not raw_name:
            self.send_json(400, {"ok": False, "error": {"code": "missing_project_name", "message": "project_name is required", "retriable": False}})
            return

        live_cfg = self.get_live_config()
        registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        try:
            proj, was_created = registry.resolve_or_register_project(raw_name)
        except RegistrySaveError as exc:
            self.send_json(503, {"ok": False, "error": {"code": "configuration_error", "message": str(exc), "retriable": True}})
            return

        if was_created:
            from audapack.bridge.state import increment_audit_generation
            increment_audit_generation(proj.display_name, "registered", project_id=proj.id)
            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(proj.display_name, "registered")
                except Exception:
                    pass

        self.send_json(200, {
            "ok": True,
            "status": "registered" if was_created else "existing",
            "project_id": proj.id,
            "display_name": proj.display_name,
            "audit_name": proj.audit_project_name or proj.display_name,
            "group": proj.priority_group,
            "slot": proj.slot,
            "registry_revision": len(live_cfg.projects),
            "created": was_created,
        })

    def handle_audit_submission(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.send_json(415, {"ok": False, "error": {"code": "unsupported_media_type", "retriable": False}})
            return

        data = self._read_json_body()
        if data is None:
            return

        # API version contract: support 2 and 3
        try:
            client_api = int(data.get("api_version", 0))
        except Exception:
            client_api = 0
        if client_api not in SUPPORTED_API_VERSIONS:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_api_version",
                    "message": f"Bridge speaks API versions {list(SUPPORTED_API_VERSIONS)}; payload declared v{client_api}",
                    "retriable": False,
                }
            })
            return

        run_id = str(data.get("run_id", "")).strip()
        project = str(data.get("project") or data.get("project_name") or "").strip()
        project_id = str(data.get("project_id", "")).strip() or None
        wave_raw = str(data.get("wave_id") or data.get("wave") or "").strip().lower()
        status = str(data.get("status", "complete")).strip().lower()
        receipt = str(data.get("receipt", "")).strip()
        content = str(data.get("content", ""))
        predecessor_sha = str(data.get("predecessor_sha256", "")).strip()

        # Profile resolution: if profile_id omitted, default to quick3 for v2 compatibility
        profile_id_req = str(data.get("profile_id", "")).strip().lower()
        if not profile_id_req:
            # Try to detect from wave name or default to quick3
            if wave_raw in ["core", "second", "performance"]:
                profile_id_req = "quick3"
            else:
                try:
                    all_profs = load_profiles()
                    for pid, pobj in all_profs.items():
                        if pobj.get_wave_by_id(wave_raw):
                            profile_id_req = pid
                            break
                except Exception:
                    pass
            if not profile_id_req:
                profile_id_req = "quick3"

        try:
            prof = get_profile(profile_id_req)
        except KeyError:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_profile",
                    "message": f"Unknown campaign profile: '{profile_id_req}'",
                    "retriable": False,
                }
            })
            return

        if not all([run_id, (project or project_id), wave_raw, receipt, content]):
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "missing_fields",
                    "message": "run_id, project/project_id, wave/wave_id, receipt, and content are required",
                    "retriable": False,
                }
            })
            return

        wave_def = prof.get_wave_by_id(wave_raw) or prof.get_wave_by_number(wave_raw)
        if not wave_def:
            valid_waves = [w.id for w in prof.waves]
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "unsupported_wave",
                    "message": f"Wave '{wave_raw}' is not valid for profile '{prof.profile_id}'. Valid waves: {valid_waves}",
                    "retriable": False,
                }
            })
            return

        if status != "complete":
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "invalid_status",
                    "message": "Only complete waves can be delivered",
                    "retriable": False,
                }
            })
            return

        live_cfg = self.get_live_config()
        out_root = Path(live_cfg.audits.root).resolve()
        if not out_root.exists():
            self.send_json(503, {
                "ok": False,
                "error": {
                    "code": "output_unavailable",
                    "message": f"Audit root unavailable: {out_root}",
                    "retriable": True,
                }
            })
            return

        # Validate wave content structure against wave_def and profile
        valid, wave_meta, parse_err = parse_wave(content, wave_def.id, prof)
        if not valid:
            self.send_json(400, {
                "ok": False,
                "error": {
                    "code": "invalid_wave_structure",
                    "message": parse_err,
                    "retriable": False,
                }
            })
            return

        # CORE-006: enforce equality between transport run_id and content
        # CAMPAIGN_RUN_ID for v3 contracts. Placeholder/missing values are
        # rejected so the durable run identity is provably bound to the
        # delivered artifact. v2 is allowed the historical relaxation.
        if client_api >= 3:
            crid_raw = (wave_meta or {}).get("campaign_run_id", "")
            crid = (crid_raw or "").strip()
            is_placeholder = (not crid) or ("<" in crid and ">" in crid) or crid.lower() in {
                "<run-id>", "<run_id>", "<campaign_run_id>", "placeholder", "n/a", "tbd",
            }
            if is_placeholder:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "invalid_run_id",
                        "message": "v3 contract requires a non-placeholder CAMPAIGN_RUN_ID header",
                        "retriable": False,
                    }
                })
                return
            if crid != run_id:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "run_id_mismatch",
                        "message": f"Payload run_id '{run_id}' does not match content CAMPAIGN_RUN_ID '{crid}'",
                        "retriable": False,
                    }
                })
                return

        # CORE-005: two explicit project identities (transport payload vs parsed
        # handoff PROJECT_NAME) must be reconciled before any registration, file,
        # or state mutation. Aliases that resolve to the same canonical project
        # are accepted; a genuine conflict is a hard, non-retriable 409.
        requested_project = project
        handoff_project = (wave_meta or {}).get("project_name") or ""
        if handoff_project and handoff_project.strip().lower() != requested_project.strip().lower():
            check_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)

            def _resolve_canonical(pid: Optional[str], name: str):
                if pid:
                    p = check_registry.get_project_by_id(pid)
                    if p:
                        return p
                if name:
                    p = check_registry.get_project_by_name(name)
                    if p:
                        return p
                return None

            p_req = _resolve_canonical(project_id, requested_project)
            p_hand = _resolve_canonical(None, handoff_project)
            if p_req and p_hand and p_req.id != p_hand.id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' resolves to '{p_req.id}' "
                            f"but handoff PROJECT_NAME '{handoff_project}' resolves to '{p_hand.id}'"
                        ),
                        "retriable": False,
                    }
                })
                return
            if p_hand and not p_req:
                # Payload names nothing known but the handoff resolves to an existing
                # project: refusing prevents handoff metadata from silently stealing
                # routing/registration for a project the transport did not address.
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' is unknown but handoff "
                            f"PROJECT_NAME '{handoff_project}' resolves to '{p_hand.id}'; refusing reroute"
                        ),
                        "retriable": False,
                    }
                })
                return
            if not p_req and not p_hand:
                # Two different unknown names: ambiguous auto-registration.
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": (
                            f"Payload project '{requested_project}' and handoff PROJECT_NAME "
                            f"'{handoff_project}' identify different unknown projects; refusing ambiguous registration"
                        ),
                        "retriable": False,
                    }
                })
                return
            # Both resolve to the same canonical project (or payload resolves and
            # handoff is a harmless formatting alias): accept the canonical name.
            project = handoff_project

        if wave_meta and wave_meta.get("project_name"):
            project = wave_meta["project_name"]

        # Canonical project resolution
        live_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        target_proj = None
        if project_id:
            target_proj = live_registry.get_project_by_id(project_id)
            if target_proj is None:
                self.send_json(400, {
                    "ok": False,
                    "error": {
                        "code": "invalid_project_id",
                        "message": f"Unknown project_id: {project_id}",
                        "retriable": False,
                    }
                })
                return

        if project:
            name_proj = live_registry.get_project_by_name(project)
            if target_proj and name_proj and name_proj.id != target_proj.id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": f"Payload project_id '{project_id}' resolves to '{target_proj.id}' but handoff name '{project}' resolves to '{name_proj.id}'",
                        "retriable": False,
                    }
                })
                return
            if target_proj is None and name_proj is not None:
                target_proj = name_proj
            if target_proj is not None:
                project = target_proj.audit_project_name or target_proj.display_name

        with run_transaction(run_id):
            try:
                state = get_run_state(run_id)
            except RunStateCorruptionError as exc:
                self.send_json(503, {
                    "ok": False,
                    "error": {"code": "run_state_corrupt", "message": str(exc), "retriable": True}
                })
                return
            existing_project = state.get("project")

            # Validate immutable run -> project_id binding
            bound_pid = state.get("project_id") or ""
            if not bound_pid and existing_project:
                legacy_bound = live_registry.get_project_by_name(existing_project)
                bound_pid = legacy_bound.id if legacy_bound else ""
                if not legacy_bound:
                    if project and existing_project and existing_project.lower() != project.lower():
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "project_identity_conflict",
                                "message": f"Run {run_id} belongs to project '{existing_project}', cannot accept '{project}'",
                                "retriable": False,
                            }
                        })
                        return

            if bound_pid:
                bound_project = live_registry.get_project_by_id(bound_pid)
                bound_names = {
                    str(getattr(bound_project, "display_name", "")).strip().lower(),
                    str(getattr(bound_project, "audit_project_name", "")).strip().lower(),
                }
                if project and project.strip().lower() not in bound_names:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "project_identity_conflict",
                            "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot accept '{project}'",
                            "retriable": False,
                        }
                    })
                    return
            if target_proj is not None and bound_pid and target_proj.id != bound_pid:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "project_identity_conflict",
                        "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot accept '{target_proj.id}'",
                        "retriable": False,
                    }
                })
                return

            # Validate immutable run -> profile_id binding
            bound_profile = state.get("profile_id") or ""
            if bound_profile and bound_profile != prof.profile_id:
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "campaign_profile_conflict",
                        "message": f"Run {run_id} is bound to profile '{bound_profile}', cannot accept '{prof.profile_id}'",
                        "retriable": False,
                    }
                })
                return

            if target_proj is not None:
                state["project_id"] = target_proj.id
                state["project_display_name"] = target_proj.display_name
            elif not bound_pid:
                state["project"] = project or existing_project or ""
            if project or existing_project:
                state["project"] = project or existing_project

            state["profile_id"] = prof.profile_id
            state["profile_version"] = prof.profile_version
            state["manifest_hash"] = prof.manifest_hash or get_canonical_manifest_hash()
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
            dt_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            wave_state = state.get("waves", {}).get(wave_def.id, {})

            # Receipt idempotency check
            if wave_state.get("receipt") == receipt:
                if wave_state.get("sha256") == content_hash:
                    files_written = []
                    if wave_state.get("latest_path"):
                        files_written.append(str(wave_state["latest_path"]))
                    if wave_state.get("history_path"):
                        files_written.append(str(wave_state["history_path"]))
                    is_ready = state.get("campaign_complete", False) or state.get("all3_complete", False)
                    generation_pending = bool(state.get("generation_pending", False))
                    if generation_pending:
                        try:
                            from audapack.bridge.state import increment_audit_generation
                            increment_audit_generation(project, wave_def.id, project_id=state.get("project_id") or None)
                            state["generation_pending"] = False
                            save_run_state(run_id, state)
                            generation_pending = False
                        except GenerationPersistenceError:
                            pass
                    completed_count = len([w for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")])
                    # CORE-002: a same-receipt retry must repair incomplete
                    # finalization (crash/partial write) instead of contradicting
                    # the first response.
                    all_waves_complete = all(
                        w.id in state.get("waves", {}) and state["waves"][w.id].get("complete")
                        for w in prof.waves if w.required
                    )
                    if not is_ready and all_waves_complete:
                        try:
                            target_dir, resolved_name, proj, _created = resolve_project_audit_dir(
                                live_cfg, project, project_id, base_dir=self.get_custom_base_dir()
                            )
                            target_dir.mkdir(parents=True, exist_ok=True)
                            parsed_dict = {
                                w.id: state["waves"][w.id].get("meta", {})
                                for w in prof.waves if w.id in state.get("waves", {})
                            }
                            history_dir = Path(state["history_dir"]) if state.get("history_dir") else None
                            if history_dir is None or not history_dir.exists():
                                history_dir = target_dir / "_history" / f"{dt_str}_{run_hash}"
                                history_dir.mkdir(parents=True, exist_ok=True)
                                state["history_dir"] = str(history_dir)
                            # CORE-004: route duplicate finalization repair through
                            # the same transactional commit gate as normal delivery.
                            # Snapshot affected artifacts/index, require the live
                            # campaign-index write to succeed before persisting
                            # ready/completion, and on failure restore the exact
                            # prior artifact/index bytes and return retriable 503.
                            snap_targets = [target_dir / "campaign.json"]
                            if prof.profile_id == "quick3":
                                snap_targets.append(target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md")
                            else:
                                snap_targets.extend([
                                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md",
                                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md",
                                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json",
                                ])
                            snapshots, snap_err = capture_file_snapshots(snap_targets)
                            if snap_err:
                                self.send_json(503, {
                                    "ok": False,
                                    "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True}
                                })
                                return
                            try:
                                synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                                _write_final_artifacts(prof, synth_result, target_dir, history_dir,
                                                       dt_str, state, resolved_name)
                                save_live_campaign_index(
                                    campaign_root=target_dir, profile=prof, run_id=run_id,
                                    project_name=resolved_name,
                                    parsed_waves={
                                        wid: {
                                            "wave_id": wid,
                                            "status": "COMPLETE",
                                            "tickets": int(w_info.get("meta", {}).get("tickets", 0)),
                                            "file": Path(w_info["latest_path"]) if w_info.get("latest_path") else None,
                                            "sha256": w_info.get("sha256", ""),
                                            "completed_at": w_info.get("completed_at", dt_str),
                                        }
                                        for wid, w_info in state.get("waves", {}).items()
                                    },
                                    completed_waves=[w.id for w in prof.waves],
                                    active_wave_id=None,
                                    status=STATUS_CAMPAIGN_COMPLETE,
                                    final_handoff_path=_get_final_handoff_path(prof, state),
                                )
                            except Exception as exc:
                                restore_file_snapshots(snapshots)
                                self.send_json(503, {
                                    "ok": False,
                                    "error": {"code": "campaign_index_failed", "message": str(exc), "retriable": True}
                                })
                                return
                            # Persist ready/completion only after the index commit
                            # succeeded; a failure above already rolled back.
                            try:
                                save_run_state(run_id, state)
                            except RunStatePersistenceError as exc:
                                restore_file_snapshots(snapshots)
                                self.send_json(503, {
                                    "ok": False,
                                    "error": {"code": "campaign_index_failed", "message": str(exc), "retriable": True}
                                })
                                return
                            is_ready = True
                            from audapack.bridge.state import increment_audit_generation
                            increment_audit_generation(resolved_name, wave_def.id, project_id=proj.id if proj else None)
                        except Exception as exc:
                            self.send_json(503, {
                                "ok": False,
                                "error": {"code": "finalization_failed", "message": str(exc), "retriable": True}
                            })
                            return
                    self.send_json(200, {
                        "ok": True,
                        "duplicate": True,
                        "run_id": run_id,
                        "profile_id": prof.profile_id,
                        "project": project,
                        "wave": wave_def.id,
                        "wave_index": wave_def.ordinal,
                        "wave_count": prof.wave_count,
                        "completed_waves": completed_count,
                        "total_waves": prof.wave_count,
                        "campaign_ready": is_ready,
                        "all3_ready": is_ready if prof.profile_id == "quick3" else state.get("all3_complete", False),
                        "files": files_written,
                    })
                    return
                else:
                    self.send_json(409, {
                        "ok": False,
                        "error": {
                            "code": "receipt_conflict",
                            "message": "Receipt already used with different content",
                            "retriable": False,
                        }
                    })
                    return

            # A completed wave is immutable within its run. Replacement would
            # require descendant invalidation/replay; reject it instead.
            if wave_state.get("complete"):
                self.send_json(409, {
                    "ok": False,
                    "error": {
                        "code": "completed_wave_immutable",
                        "message": f"Wave '{wave_def.id}' is already complete in run {run_id}; start a fresh run for replacement",
                        "retriable": False,
                    }
                })
                return

            # Order / dependency validation
            existing_waves = state.get("waves", {})
            for dep_id in wave_def.depends_on:
                if dep_id not in existing_waves or not existing_waves[dep_id].get("complete"):
                    self.send_json(400, {
                        "ok": False,
                        "error": {
                            "code": "out_of_order_wave",
                            "message": f"Wave '{wave_def.id}' depends on wave '{dep_id}' which has not been completed yet in run {run_id}",
                            "retriable": False,
                        }
                    })
                    return

            # Predecessor hash validation if provided
            if predecessor_sha and wave_def.ordinal > 1:
                prev_wave = prof.get_wave_by_ordinal(wave_def.ordinal - 1)
                if prev_wave and prev_wave.id in existing_waves:
                    prev_recorded_sha = existing_waves[prev_wave.id].get("sha256", "")
                    if prev_recorded_sha and predecessor_sha.lower() != prev_recorded_sha.lower():
                        self.send_json(409, {
                            "ok": False,
                            "error": {
                                "code": "predecessor_mismatch",
                                "message": f"Declared predecessor hash '{predecessor_sha[:12]}' does not match recorded hash of previous wave '{prev_recorded_sha[:12]}'",
                                "retriable": False,
                            }
                        })
                        return

            # Resolve project audit directory through canonical registry
            try:
                target_dir, resolved_name, proj, was_created = resolve_project_audit_dir(
                    live_cfg, project, project_id, base_dir=self.get_custom_base_dir()
                )
            except InvalidProjectPathError as exc:
                self.send_json(400, {
                    "ok": False,
                    "error": {"code": "invalid_project_path", "message": str(exc), "retriable": False}
                })
                return
            target_dir.mkdir(parents=True, exist_ok=True)

            w_no = wave_def.number
            w_slug = wave_def.slug

            latest_filename = f"{resolved_name}__{w_no}_{w_slug}.md"
            latest_path = target_dir / latest_filename
            run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
            dt_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

            if state.get("history_dir") and Path(state["history_dir"]).exists():
                history_dir = Path(state["history_dir"])
            else:
                history_dir = target_dir / "_history" / f"{dt_str}_{run_hash}"
                history_dir.mkdir(parents=True, exist_ok=True)
                state["history_dir"] = str(history_dir)

            history_filename = f"{resolved_name}__{w_no}_{w_slug}__{dt_str}.md"
            history_path = history_dir / history_filename

            # CORE-003: snapshot all canonical artifact paths before any write
            # so ANY failure below can restore the exact previous state.
            snapshot_paths = [latest_path, history_path, target_dir / "campaign.json"]
            if prof.profile_id == "quick3":
                snapshot_paths.extend([
                    target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md",
                    history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md",
                ])
            else:
                snapshot_paths.extend([
                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md",
                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md",
                    target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json",
                    history_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL__{dt_str}.md",
                    history_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL__{dt_str}.md",
                    history_dir / "manifest.json",
                ])
            snapshots, snap_err = capture_file_snapshots(snapshot_paths)
            if snap_err:
                self.send_json(500, {
                    "ok": False,
                    "error": {"code": "atomic_write_failed", "message": snap_err, "retriable": True}
                })
                return

            # Write history first, then canonical latest (CORE-003).
            try:
                atomic_write(history_path, content)
                atomic_write(latest_path, content)
            except Exception as exc:
                restore_file_snapshots(snapshots)
                self.send_json(500, {
                    "ok": False,
                    "error": {"code": "atomic_write_failed", "message": str(exc), "retriable": True}
                })
                return

            if "waves" not in state:
                state["waves"] = {}
            state["waves"][wave_def.id] = {
                "complete": True,
                "ordinal": wave_def.ordinal,
                "sha256": content_hash,
                "receipt": receipt,
                "completed_at": dt_str,
                "latest_path": str(latest_path),
                "history_path": str(history_path),
                "meta": wave_meta,
            }

            all_waves = state["waves"]
            required_waves = [w for w in prof.waves if w.required]
            campaign_ready = all(w.id in all_waves and all_waves[w.id].get("complete") for w in required_waves)

            final_handoff_path: Optional[Path] = None
            canonical_campaign_path: Optional[Path] = None
            finalization_ok = False

            if campaign_ready:
                parsed_dict = {
                    w.id: all_waves[w.id].get("meta", {}) for w in prof.waves if w.id in all_waves
                }
                try:
                    synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)
                    _write_final_artifacts(prof, synth_result, target_dir, history_dir,
                                           dt_str, state, resolved_name)
                    final_handoff_path = _get_final_handoff_path(prof, state)
                    canonical_campaign_path = _get_canonical_path(prof, state)
                    finalization_ok = True
                except Exception as exc:
                    restore_file_snapshots(snapshots)
                    self.send_json(503, {
                        "ok": False,
                        "error": {"code": "finalization_failed", "message": str(exc), "retriable": True}
                    })
                    return

            # Live campaign index — only writes COMPLETE when finalization
            # succeeded (CORE-002). On failure roll back and return retriable.
            completed_waves_list = [w.id for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")]
            next_w = prof.get_next_wave(wave_def.id)
            active_wid = None if campaign_ready else (next_w.id if next_w else None)
            c_status = STATUS_CAMPAIGN_COMPLETE if (campaign_ready and finalization_ok) else STATUS_CAMPAIGN_READY_FOR_WAVE

            parsed_waves_dict = {
                wid: {
                    "wave_id": wid,
                    "status": "COMPLETE" if w_info.get("complete") else "IDLE",
                    "tickets": int(w_info.get("meta", {}).get("tickets", 0)),
                    "file": Path(w_info.get("latest_path", "")) if w_info.get("latest_path") else None,
                    "sha256": w_info.get("sha256", ""),
                    "completed_at": w_info.get("completed_at", dt_str),
                }
                for wid, w_info in all_waves.items()
            }
            try:
                save_live_campaign_index(
                    campaign_root=target_dir,
                    profile=prof,
                    run_id=run_id,
                    project_name=resolved_name,
                    parsed_waves=parsed_waves_dict,
                    completed_waves=completed_waves_list,
                    active_wave_id=active_wid,
                    status=c_status,
                    final_handoff_path=final_handoff_path,
                )
            except Exception as ex:
                if finalization_ok:
                    restore_file_snapshots(snapshots)
                self.send_json(503, {
                    "ok": False,
                    "error": {"code": "campaign_index_failed", "message": str(ex), "retriable": True}
                })
                return

            # W2-002: persist the pending-publication marker as part of the primary
            # durable state commit BEFORE publishing generation, so recovery intent
            # survives a crash and duplicate retries can repair a missed publication.
            state["generation_pending"] = True
            try:
                save_run_state(run_id, state)
            except RunStatePersistenceError as exc:
                restore_file_snapshots(snapshots)
                self.send_json(500, {
                    "ok": False,
                    "error": {"code": "run_state_persistence_failed", "message": str(exc), "retriable": True}
                })
                return

            from audapack.bridge.state import increment_audit_generation
            generation_pending = True
            try:
                # W2-003: pass the resolved canonical project id so consumers can
                # refresh the exact project instead of falling back to name lookup.
                increment_audit_generation(resolved_name, wave_def.id, project_id=proj.id if proj else None)
                generation_pending = False
                state["generation_pending"] = False
                try:
                    save_run_state(run_id, state)
                except RunStatePersistenceError:
                    # Publication succeeded but clearing the marker failed: keep
                    # the marker so a duplicate retry repairs the clear (W2-002).
                    state["generation_pending"] = True
                    generation_pending = True
            except GenerationPersistenceError:
                # Marker already durable; duplicate retries repair publication.
                generation_pending = True

            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(resolved_name, wave_def.id)
                except Exception:
                    pass

            files_written = [str(latest_path), str(history_path)]
            if campaign_ready and finalization_ok:
                if final_handoff_path:
                    files_written.append(str(final_handoff_path))
                if canonical_campaign_path and str(canonical_campaign_path) != str(final_handoff_path):
                    files_written.append(str(canonical_campaign_path))

            completed_count = len([w for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")])

            self.send_json(200, {
                "ok": True,
                "duplicate": False,
                "api_version": client_api,
                "run_id": run_id,
                "profile_id": prof.profile_id,
                "profile_version": prof.profile_version,
                "project_id": proj.id,
                "project": resolved_name,
                "group": proj.priority_group,
                "slot": proj.slot,
                "wave": wave_def.id,
                "wave_index": wave_def.ordinal,
                "wave_count": prof.wave_count,
                "completed_waves": completed_count,
                "total_waves": prof.wave_count,
                "campaign_ready": campaign_ready and finalization_ok,
                "all3_ready": campaign_ready and finalization_ok if prof.profile_id == "quick3" else state.get("all3_complete", False),
                "files": files_written,
                "history_dir": str(history_dir),
                "final_handoff_path": str(final_handoff_path) if final_handoff_path else "",
                "canonical_campaign_path": str(canonical_campaign_path) if canonical_campaign_path else "",
                "generation_pending": generation_pending,
            })


def run_bridge_server(config: AppConfig) -> int:
    """Runs the AUDAPACK Bridge HTTP daemon on configured loopback host/port."""
    host = normalize_bridge_host(config.bridge.host)
    port = config.bridge.port

    class HandlerWithConfig(AudapackBridgeHandler):
        pass

    HandlerWithConfig.config = config

    try:
        server = ThreadingHTTPServer((host, port), HandlerWithConfig)
    except OSError as exc:
        print(f"Error starting AUDAPACK Bridge: Port {port} on {host} already in use or unavailable: {exc}", file=sys.stderr)
        return 1

    write_pid()
    print(f"AUDAPACK Bridge listening on http://{host}:{port}")
    # W2-011: prune expired history on startup (best-effort, non-blocking).
    try:
        from audapack.bridge.storage import prune_audit_history
        removed = prune_audit_history(config)
        if removed:
            logger.info(f"Pruned {removed} expired history run(s) from audit root")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        remove_pid(expected_pid=os.getpid(), expected_nonce=INSTANCE_NONCE)
    return 0
