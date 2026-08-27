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
from audapack.bridge.lifecycle import remove_pid, write_pid
from audapack.bridge.state import get_run_lock, get_run_state, save_run_state
from audapack.bridge.storage import (
    InvalidProjectPathError,
    atomic_write,
    generate_canonical_campaign,
    parse_wave,
    resolve_project_audit_dir,
)
from audapack.campaign import (
    get_canonical_manifest_hash,
    get_profile,
    load_profiles,
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
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            self.send_json(415, {"ok": False, "error": {"code": "unsupported_media_type", "retriable": False}})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_request", "retriable": False}})
            return

        max_bytes = self.config.bridge.max_request_bytes
        if length > max_bytes:
            self.send_json(413, {"ok": False, "error": {"code": "payload_too_large", "retriable": False}})
            return

        try:
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except Exception:
            self.send_json(400, {"ok": False, "error": {"code": "invalid_json", "retriable": False}})
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
            if target_proj is None and name_proj is None:
                try:
                    name_proj, _created = live_registry.resolve_or_register_project(project)
                except RegistrySaveError as exc:
                    self.send_json(503, {
                        "ok": False,
                        "error": {"code": "configuration_error", "message": str(exc), "retriable": True}
                    })
                    return
            if target_proj is None and name_proj is not None:
                target_proj = name_proj
            if target_proj is not None:
                project = target_proj.audit_project_name or target_proj.display_name

        run_lock = get_run_lock(run_id)
        with run_lock:
            state = get_run_state(run_id)
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
                    completed_count = len([w for w in prof.waves if state.get("waves", {}).get(w.id, {}).get("complete")])
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

            # Check if all required waves are complete
            all_waves = state["waves"]
            required_waves = [w for w in prof.waves if w.required]
            campaign_ready = all(w.id in all_waves and all_waves[w.id].get("complete") for w in required_waves)

            final_handoff_path: Optional[Path] = None
            canonical_campaign_path: Optional[Path] = None

            if campaign_ready:
                parsed_dict = {
                    w.id: all_waves[w.id].get("meta", {}) for w in prof.waves if w.id in all_waves
                }
                synth_result = generate_canonical_campaign(prof, run_id, parsed_dict, resolved_name)

                if prof.profile_id == "quick3":
                    all3_content = synth_result.get("all3", "")
                    all3_latest = target_dir / f"{resolved_name}__00_AUDIT_ALL_3.md"
                    all3_hist = history_dir / f"{resolved_name}__00_AUDIT_ALL_3__{dt_str}.md"
                    try:
                        atomic_write(all3_latest, all3_content)
                        atomic_write(all3_hist, all3_content)
                        state["all3_complete"] = True
                        state["all3_path"] = str(all3_latest)
                        canonical_campaign_path = all3_latest
                        final_handoff_path = all3_latest
                    except Exception as exc:
                        logger.error(f"Failed to write ALL_3: {exc}")
                else:
                    # SUPER10 / N-wave outputs
                    super_all = synth_result.get("super_all", "")
                    super_final = synth_result.get("super_final", "")
                    super_index = synth_result.get("super_index", "")

                    all_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL.md"
                    final_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL.md"
                    index_latest = target_dir / f"{resolved_name}__00_SUPER_AUDIT_INDEX.json"

                    all_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_ALL__{dt_str}.md"
                    final_hist = history_dir / f"{resolved_name}__00_SUPER_AUDIT_FINAL__{dt_str}.md"
                    index_hist = history_dir / "manifest.json"

                    try:
                        atomic_write(all_latest, super_all)
                        atomic_write(all_hist, super_all)
                        atomic_write(final_latest, super_final)
                        atomic_write(final_hist, super_final)
                        atomic_write(index_latest, super_index)
                        atomic_write(index_hist, super_index)

                        state["campaign_complete"] = True
                        state["final_handoff_path"] = str(final_latest)
                        state["canonical_campaign_path"] = str(all_latest)
                        final_handoff_path = final_latest
                        canonical_campaign_path = all_latest
                    except Exception as exc:
                        logger.error(f"Failed to write campaign synthesis files: {exc}")

            save_run_state(run_id, state)

            from audapack.bridge.state import increment_audit_generation
            increment_audit_generation(resolved_name, wave_def.id)

            if _ON_AUDIT_WRITTEN:
                try:
                    _ON_AUDIT_WRITTEN(resolved_name, wave_def.id)
                except Exception:
                    pass

            files_written = [str(latest_path), str(history_path)]
            if campaign_ready:
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
                "campaign_ready": campaign_ready,
                "all3_ready": campaign_ready if prof.profile_id == "quick3" else state.get("all3_complete", False),
                "files": files_written,
                "history_dir": str(history_dir),
                "final_handoff_path": str(final_handoff_path) if final_handoff_path else "",
                "canonical_campaign_path": str(canonical_campaign_path) if canonical_campaign_path else "",
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
