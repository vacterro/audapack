"""Producer transport into a project's bound SAIPEN installation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from audapack.procutil import run_hidden


class SaipenTransportError(OSError):
    """Enqueue failed; the caller must retain its capture for an exact retry."""


def is_managed(root: Path | str) -> bool:
    return (Path(root) / ".saipen").is_dir()


def _entrypoint(root: Path) -> Path:
    try:
        state = (root / ".saipen" / "STATE.md").read_text(encoding="utf-8-sig")
        front = state.split("---", 2)
        if len(front) != 3 or front[0].strip():
            raise ValueError("STATE has no YAML front matter")
        values = re.findall(r"^saipen_home:\s*(.+)$", front[1], re.M)
        if len(values) != 1:
            raise ValueError("STATE needs one saipen_home binding")
        value = values[0].strip()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        home = Path(value)
        if not home.is_absolute():
            raise ValueError("saipen_home must be absolute")
        entry = home / "tools" / "saipen.py"
        if not entry.is_file():
            raise ValueError(f"missing bound CLI: {entry}")
        return entry
    except (OSError, ValueError, TypeError) as exc:
        raise SaipenTransportError(f"Cannot enqueue audit: {exc}. Repair the project's SAIPEN binding and retry.") from exc


def enqueue_file(root: Path | str, body: Path, operation_id: str, *, item_id: str = "") -> dict:
    """Retry the same producer operation, including after its layer was consumed."""
    root = Path(root).resolve()
    entry = _entrypoint(root)
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    args = [str(executable), str(entry), "audit", "enqueue", "--producer", "audapack",
            "--operation-id", operation_id, "--file", str(body.resolve()),
            "--project-root", str(root), "--json"]
    if item_id:
        args.extend(["--item-id", item_id])
    try:
        expected = hashlib.sha256(body.read_bytes()).hexdigest()
        result = run_hidden(args, capture_output=True, text=True, encoding="utf-8", timeout=30)
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("CLI returned no result object")
        if result.returncode or data.get("ok") is not True:
            raise ValueError(f"{data.get('code', 'ENQUEUE_FAILED')}: {data.get('detail', '')}")
        if (data.get("code") != "AUDIT_ENQUEUED"
                or data.get("sha256") != expected
                or data.get("producer_operation_id") != operation_id
                or data.get("producer") != "audapack"
                or not re.fullmatch(r"audit/[1-9][0-9]*\.md", str(data.get("rel", "")))):
            raise ValueError("CLI returned mismatched enqueue evidence")
        return data
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SaipenTransportError(f"SAIPEN enqueue failed: {exc}. Capture retained; retry the same capture.") from exc


def enqueue_bytes(root: Path | str, body: bytes, operation_id: str, *, item_id: str = "") -> dict:
    with tempfile.TemporaryDirectory(prefix="audapack-enqueue-") as temp:
        path = Path(temp) / "capture.md"
        path.write_bytes(body)
        return enqueue_file(root, path, operation_id, item_id=item_id)


def continuation_command(action: str) -> str:
    """Legacy GG controls also continue through the canonical Audit Inbox."""
    return "saipen cc" if action else ""
