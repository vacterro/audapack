"""AUDAPACK Loopback HTTP Bridge server implementation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from audapack import __app_name__, __version__
from audapack.bridge.lifecycle import remove_pid, write_pid
from audapack.bridge.state import get_run_lock, get_run_state, save_run_state
from audapack.bridge.storage import (
    WAVES_CONFIG,
    InvalidProjectPathError,
    atomic_write,
    generate_canonical_all3,
    parse_wave,
    resolve_project_audit_dir,
)
from audapack.components.widget import get_bundled_widget_path
from audapack.config import AppConfig, app_dir, legacy_token_acceptance_revoked, load_config, normalize_bridge_host
from audapack.projects import ProjectRegistry, RegistrySaveError

logger = logging.getLogger("audapack.bridge")

# Canonical API contract version. /health advertises it; every audit payload
# must declare the same value or receive a permanent unsupported_api_version.
BRIDGE_API_VERSION = 2

# Global callback for notifying UI of new audits or auto-registered projects
_ON_AUDIT_WRITTEN: Optional[Callable[[str, str], None]] = None


def set_audit_written_callback(cb: Optional[Callable[[str, str], None]]):
    global _ON_AUDIT_WRITTEN
    _ON_AUDIT_WRITTEN = cb


class AudapackBridgeHandler(BaseHTTPRequestHandler):
    config: AppConfig

    def log_message(self, format: str, *args):
        # Override to prevent default console spam; use logger
        pass

    def _cors_origin(self) -> str:
        # Reflect the caller's Origin instead of wildcarding credential-bearing
        # API access (CORE-001). The Bridge authenticates via the X-ACB-Token
        # header (not cookies), so reflecting Origin keeps cross-origin widget
        # access working without publishing a permissive "*" surface.
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

    def get_custom_base_dir(self) -> Optional[Path]:
        if self.config and self.config.bridge.port != 17843:
            try:
                return Path(self.config.audits.root).parent
            except Exception:
                pass
        return None

    def get_live_config(self) -> AppConfig:
        try:
            if self.config and (self.config.bridge.port != 17843 or not Path(self.config.audits.root).exists()):
                return self.config
            cfg = load_config()
            if cfg and cfg.audits and cfg.audits.root:
                return cfg
        except Exception:
            pass
        return self.config

    def _legacy_token_candidates(self) -> list[Path]:
        """Migration-scoped legacy token files under %LOCALAPPDATA% (never hard-coded users).

        Acceptance stops permanently once revoke_legacy_token_acceptance() has run
        after a verified takeover/rotation; the canonical production token is read
        through config helpers only.
        """
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

        # Migration-scoped legacy backup tokens (bounded by the revocation marker)
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
        # Source-address proof that the request actually originated locally.
        # Host-header validation alone is a DNS-rebinding defense and must never
        # be treated as source authentication (CORE-002).
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
        if parsed.path == "/health":
            live_cfg = self.get_live_config()
            self.send_json(200, {
                "ok": True,
                "service": "AUDAPACK Bridge",
                "version": __version__,
                "api_version": BRIDGE_API_VERSION,
                "instance_id": f"audapack_{os.getpid()}",
                "registry_revision": len(live_cfg.projects),
            })
        elif parsed.path == "/widget.user.js":
            w_path = get_bundled_widget_path()
            if w_path.exists():
                # SECURITY (CORE-001): never substitute the production token into
                # the served widget. The bundled file uses an empty placeholder and
                # token provisioning is user-mediated (Tampermonkey storage), so we
                # ship the JavaScript exactly as bundled, token-free.
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

    def get_custom_base_dir(self) -> Optional[Path]:
        if self.config and self.config.bridge.port != 17843:
            try:
                return Path(self.config.audits.root).parent
            except Exception:
                pass
        return None

    def handle_project_resolve(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_json", "retriable": False}})
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
            # Persistence failed: never report a registration the next request
            # cannot observe. Retriable by the client.
            self.send_json(503, {"ok": False, "error": {"code": "configuration_error", "message": str(exc), "retriable": True}})
            return

        if was_created:
            from audapack.bridge.state import increment_audit_generation
            increment_audit_generation(proj.display_name, "registered")
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
        # Content-Type check
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.send_json(415, {"ok": False, "error": {"code": "unsupported_media_type", "retriable": False}})
            return

        # Content-Length check
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "retriable": False}})
            return

        max_bytes = self.config.bridge.max_request_bytes
        if length > max_bytes:
            self.send_json(413, {"ok": False, "error": {"code": "payload_too_large", "retriable": False}})
            return

        # Read body
        try:
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except Exception:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_json", "retriable": False}})
            return

        run_id = str(data.get("run_id", "")).strip()
        project = str(data.get("project") or data.get("project_name") or "").strip()
        project_id = str(data.get("project_id", "")).strip() or None
        wave = str(data.get("wave", "")).strip().lower()
        status = str(data.get("status", "complete")).strip().lower()
        receipt = str(data.get("receipt", "")).strip()
        content = str(data.get("content", ""))

        if not all([run_id, (project or project_id), wave, receipt, content]):
            self.send_json(400, {"ok": False, "error": {"code": "missing_fields", "message": "run_id, project/project_id, wave, receipt, and content are required", "retriable": False}})
            return

        if wave not in WAVES_CONFIG:
            self.send_json(400, {"ok": False, "error": {"code": "unsupported_wave", "message": f"Wave must be one of {list(WAVES_CONFIG.keys())}", "retriable": False}})
            return

        # API version contract: never ignored, never guessed.
        try:
            client_api = int(data.get("api_version", 0))
        except Exception:
            client_api = 0
        if client_api != BRIDGE_API_VERSION:
            self.send_json(400, {"ok": False, "error": {"code": "unsupported_api_version", "message": f"Bridge speaks API v{BRIDGE_API_VERSION}; payload declared v{client_api}", "retriable": False}})
            return

        if status != "complete":
            self.send_json(400, {"ok": False, "error": {"code": "invalid_status", "message": "Only complete waves can be delivered", "retriable": False}})
            return

        # Reload live config to re-resolve current placement
        live_cfg = self.get_live_config()

        # Validate audit root existence (fail closed)
        out_root = Path(live_cfg.audits.root).resolve()
        if not out_root.exists():
            self.send_json(503, {"ok": False, "error": {"code": "output_unavailable", "message": f"Audit root unavailable: {out_root}", "retriable": True}})
            return

        # Validate wave content structure
        valid, wave_meta, parse_err = parse_wave(content, wave)
        if not valid:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_wave_structure", "message": parse_err, "retriable": False}})
            return

        # Reconcile project name from wave metadata if present
        if wave_meta and wave_meta.get("project_name"):
            project = wave_meta["project_name"]

        # ---- Canonical identity resolution BEFORE any state write ----
        # The explicit payload project_id (if given) and the effective name
        # (handoff PROJECT_NAME, else request name) must resolve to the SAME
        # canonical registry identity; otherwise 409 with no physical write.
        live_registry = ProjectRegistry(live_cfg, base_dir=self.get_custom_base_dir(), transactional=True)
        target_proj = None
        if project_id:
            target_proj = live_registry.get_project_by_id(project_id)
            if target_proj is None:
                self.send_json(400, {"ok": False, "error": {"code": "invalid_project_id", "message": f"Unknown project_id: {project_id}", "retriable": False}})
                return
        if project:
            # CORE-012: read-only identity lookup first. Never auto-register an
            # unknown handoff name before the conflict check; registration may
            # occur only when there is no authoritative project_id/binding and the
            # resolved identities do not conflict. This keeps a 409 from mutating
            # persistent registry state.
            name_proj = live_registry.get_project_by_name(project)
            if target_proj and name_proj and name_proj.id != target_proj.id:
                self.send_json(409, {"ok": False, "error": {"code": "project_identity_conflict", "message": f"Payload project_id '{project_id}' resolves to '{target_proj.id}' but handoff name '{project}' resolves to '{name_proj.id}'", "retriable": False}})
                return
            if target_proj is None and name_proj is None:
                try:
                    name_proj, _created = live_registry.resolve_or_register_project(project)
                except RegistrySaveError as exc:
                    self.send_json(503, {"ok": False, "error": {"code": "configuration_error", "message": str(exc), "retriable": True}})
                    return
            if target_proj is None and name_proj is not None:
                target_proj = name_proj
            # Normalize the effective name to the canonical identity so later
            # directory resolution never triggers a second, conflicting register.
            if target_proj is not None:
                project = target_proj.audit_project_name or target_proj.display_name

        run_lock = get_run_lock(run_id)
        with run_lock:
            state = get_run_state(run_id)
            existing_project = state.get("project")

            # Immutable run -> project_id binding, resolved before comparing.
            bound_pid = state.get("project_id") or ""
            if not bound_pid and existing_project:
                legacy_bound = live_registry.get_project_by_name(existing_project)
                bound_pid = legacy_bound.id if legacy_bound else ""
                if not legacy_bound:
                    # Unresolvable legacy binding: strict textual fallback.
                    if project and existing_project and existing_project.lower() != project.lower():
                        self.send_json(409, {"ok": False, "error": {"code": "project_identity_conflict", "message": f"Run {run_id} belongs to project '{existing_project}', cannot accept '{project}'", "retriable": False}})
                        return

            if target_proj is not None and bound_pid and target_proj.id != bound_pid:
                self.send_json(409, {"ok": False, "error": {"code": "project_identity_conflict", "message": f"Run {run_id} is bound to project_id '{bound_pid}', cannot accept '{target_proj.id}'", "retriable": False}})
                return

            if target_proj is not None:
                state["project_id"] = target_proj.id
                state["project_display_name"] = target_proj.display_name  # diagnostic only
            elif not bound_pid:
                # No canonical resolution yet; keep textual for later resolution.
                state["project"] = project or existing_project or ""
            if project or existing_project:
                state["project"] = project or existing_project
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            wave_state = state.get("waves", {}).get(wave, {})

            # Receipt idempotency check
            if wave_state.get("receipt") == receipt:
                if wave_state.get("sha256") == content_hash:
                    files_written = []
                    if wave_state.get("latest_path"):
                        files_written.append(str(wave_state["latest_path"]))
                    if wave_state.get("history_path"):
                        files_written.append(str(wave_state["history_path"]))
                    self.send_json(200, {
                        "ok": True,
                        "duplicate": True,
                        "run_id": run_id,
                        "project": project,
                        "wave": wave,
                        "all3_ready": state.get("all3_complete", False),
                        "files": files_written,
                    })
                    return
                else:
                    # Same receipt, different content: conflict!
                    self.send_json(409, {"ok": False, "error": {"code": "receipt_conflict", "message": "Receipt already used with different content", "retriable": False}})
                    return

            # Resolve project audit directory through canonical registry
            try:
                target_dir, resolved_name, proj, was_created = resolve_project_audit_dir(live_cfg, project, project_id, base_dir=self.get_custom_base_dir())
            except InvalidProjectPathError as exc:
                self.send_json(400, {"ok": False, "error": {"code": "invalid_project_path", "message": str(exc), "retriable": False}})
                return
            target_dir.mkdir(parents=True, exist_ok=True)

            w_info = WAVES_CONFIG[wave]
            w_no = w_info["number"]
            w_slug = w_info["slug"]

            # 1. Latest canonical wave file
            latest_filename = f"{resolved_name}__{w_no}_{w_slug}.md"
            latest_path = target_dir / latest_filename

            # 2. History wave file in unified single-run history folder
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

            try:
                atomic_write(latest_path, content)
                atomic_write(history_path, content)
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": {"code": "atomic_write_failed", "message": str(exc), "retriable": True}})
                return

            # Update run state
            if "waves" not in state:
                state["waves"] = {}
            state["waves"][wave] = {
                "complete": True,
                "sha256": content_hash,
                "receipt": receipt,
                "completed_at": dt_str,
                "latest_path": str(latest_path),
                "history_path": str(history_path),
                "meta": wave_meta,
            }

            # Check if all 3 waves are complete
            all3_ready = False
            all_waves = state["waves"]
            if all(k in all_waves for k in ["core", "second", "performance"]):
                # Build canonical ALL_3
                parsed_dict = {
                    k: all_waves[k].get("meta", {}) for k in ["core", "second", "performance"]
                }
                all3_content = generate_canonical_all3(resolved_name, run_id, parsed_dict)

                all3_latest_path = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
                all3_history_path = history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md"
                try:
                    atomic_write(all3_latest_path, all3_content)
                    atomic_write(all3_history_path, all3_content)
                    all3_ready = True
                    state["all3_complete"] = True
                except Exception as exc:
                    logger.error(f"Failed to write ALL_3: {exc}")

            save_run_state(run_id, state)

            # Signal cross-process generation update
            from audapack.bridge.state import increment_audit_generation
            increment_audit_generation(resolved_name, wave)

            # Invalidate/notify UI callback
            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(resolved_name, wave)
                except Exception:
                    pass

            files_written = [str(latest_path), str(history_path)]
            if all3_ready and state.get("all3_path"):
                files_written.append(str(state["all3_path"]))

            self.send_json(200, {
                "ok": True,
                "duplicate": False,
                "run_id": run_id,
                "project_id": proj.id,
                "project": resolved_name,
                "group": proj.priority_group,
                "slot": proj.slot,
                "wave": wave,
                "all3_ready": all3_ready,
                "files": files_written,
                "history_dir": str(history_dir),
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        remove_pid()
    return 0
