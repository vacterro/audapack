"""Read-only SAIPEN protocol inspection and Git change awareness for AUDAPACK."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from audapack.models import SaipenInfo


def detect_saipen_root(project_path: str | Path) -> Optional[Path]:
    """Checks if project root contains .saipen directory."""
    if not project_path:
        return None
    try:
        p = Path(project_path).resolve()
        if not p.exists() or not p.is_dir():
            return None
        saipen_dir = p / ".saipen"
        if saipen_dir.exists() and saipen_dir.is_dir():
            return saipen_dir
    except Exception:
        pass
    return None


def read_saipen_summary(saipen_dir: Path) -> dict[str, str]:
    """Reads STATE.md and IDENTITY.md in a read-only manner."""
    info = {
        "task": "",
        "phase": "",
        "next_action": "",
        "updated": "",
        "style_contract": "",
    }
    state_file = saipen_dir / "STATE.md"
    if state_file.exists() and state_file.is_file():
        try:
            content = state_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line_s = line.strip()
                if line_s.startswith("task:"):
                    info["task"] = line_s.split("task:", 1)[1].strip()
                elif line_s.startswith("phase:"):
                    info["phase"] = line_s.split("phase:", 1)[1].strip()
                elif line_s.startswith("next_action:"):
                    info["next_action"] = line_s.split("next_action:", 1)[1].strip()
                elif line_s.startswith("last_event:"):
                    info["updated"] = line_s.split("last_event:", 1)[1].strip()
                elif line_s.startswith("style_contract:"):
                    info["style_contract"] = line_s.split("style_contract:", 1)[1].strip()
        except Exception:
            pass

    return info


def inspect_git_status(project_path: Path) -> dict[str, Any]:
    """Runs lightweight read-only git queries to check branch, head, dirty, and changed files."""
    result = {
        "git_available": False,
        "branch": "",
        "head": "",
        "dirty": False,
        "changed_files": 0,
        "untracked_files": 0,
    }
    git_dir = project_path / ".git"
    if not git_dir.exists():
        return result

    try:
        # Branch
        proc_br = subprocess.run(
            ["git", "-C", str(project_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if proc_br.returncode == 0:
            result["git_available"] = True
            result["branch"] = proc_br.stdout.strip()

        # HEAD commit
        proc_head = subprocess.run(
            ["git", "-C", str(project_path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if proc_head.returncode == 0:
            result["head"] = proc_head.stdout.strip()

        # Status
        proc_st = subprocess.run(
            ["git", "-C", str(project_path), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if proc_st.returncode == 0:
            lines = proc_st.stdout.splitlines()
            changed = 0
            untracked = 0
            for line in lines:
                if line.startswith("??"):
                    untracked += 1
                elif line.strip():
                    changed += 1
            result["dirty"] = (changed > 0 or untracked > 0)
            result["changed_files"] = changed
            result["untracked_files"] = untracked

    except Exception:
        pass

    return result


def get_saipen_info(project_path: str | Path) -> SaipenInfo:
    """Full read-only inspection of SAIPEN metadata and Git changes."""
    if not project_path:
        return SaipenInfo(detected=False)

    p = Path(project_path).resolve()
    saipen_dir = detect_saipen_root(p)
    if not saipen_dir:
        return SaipenInfo(detected=False)

    summary = read_saipen_summary(saipen_dir)
    git_info = inspect_git_status(p)

    return SaipenInfo(
        detected=True,
        root_path=saipen_dir,
        task=summary.get("task", ""),
        phase=summary.get("phase", ""),
        next_action=summary.get("next_action", ""),
        updated=summary.get("updated", ""),
        git_branch=git_info.get("branch", ""),
        git_head=git_info.get("head", ""),
        git_dirty=bool(git_info.get("dirty", False)),
        git_changed_files=int(git_info.get("changed_files", 0)),
        git_untracked_files=int(git_info.get("untracked_files", 0)),
    )


def saipen_gg_entrypoint(path_or_str: str | Path) -> dict[str, Any]:
    """
    Entrypoint resolver for 'saipen gg <path>'.
    Discovers campaign root, reconstructs campaign state, and determines active wave and prerequisite artifacts.
    """
    from audapack.campaign import resolve_audit_campaign_entrypoint
    return resolve_audit_campaign_entrypoint(path_or_str)
