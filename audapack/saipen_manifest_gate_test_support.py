"""Test-support stub for the SAIPEN audit-manifest authority gate.

T-850/SRC-007 introduced a pre-pack gate that enrolls a SAIPEN project
through the REAL protocol CLI (``saipen audit manifest --write``) whenever
``.saipen/MANIFEST.json`` is missing or stale, and fails the pack closed
when no CLI can be reached. Tests that exercise unrelated machinery
(fidelity profiles, Git inventory) but whose fixtures carry a `.saipen/`
tree need the gate to succeed WITHOUT depending on a protocol install --
and, importantly, without weakening the gate for production packs.

This module is the same test double the manifest-gate regression suite
uses, packaged once: a stub protocol CLI reached through the fixture
project's own ``saipen_home`` STATE key (the gate resolves STATE before
PATH), which writes a minimal ``saipen-audit-manifest/1`` contract naming
the four mandatory documents. It lives under ``audapack/`` (importable from
every test module) but is NEVER imported by production code paths: the gate
imports only ``saipen_evidence`` and ``procutil``.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import sys
from pathlib import Path

_CONTRACT = {
    "schema_version": 1,
    "kind": "saipen_audit_manifest",
    "contract_version": 1,
    "protocol_version": "8.0.1",
    "generator": "saipen-audit-manifest/1",
    "memory_root": ".saipen",
    "required": ["STATE.md", "BOARD.md", "LOG.md", "IDENTITY.md"],
    "evidence": {
        "mandatory": [
            {"path": "STATE.md", "kind": "file"},
            {"path": "BOARD.md", "kind": "file"},
            {"path": "LOG.md", "kind": "file"},
            {"path": "IDENTITY.md", "kind": "file"},
        ],
        "conditional": [],
        "optional": [],
        "non_exportable": ["locks/", "recovery/", "LOCAL_STATE.json"],
    },
}

_WRITER_SOURCE = '''"""Stub SAIPEN protocol CLI writer (test double)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    sp = Path.cwd() / ".saipen"
    sp.mkdir(parents=True, exist_ok=True)
    contract_path = Path(__file__).resolve().parent / "saipen_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    (sp / "MANIFEST.json").write_text(
        json.dumps(contract, indent=1), encoding="utf-8"
    )
    print("code: AUDIT_MANIFEST_WRITTEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_POSIX_SHIM = '#!/bin/sh\nexec "{python}" "{writer}" "$@"\n'
_WIN_SHIM = '@echo off\r\n"{python}" "{writer}" %*\r\n'


class StubSaipenCli:
    """A stub protocol CLI home wired into one project's STATE.md."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.home = self.project_root / ".stub-saipen-home"
        self._state_path = self.project_root / ".saipen" / "STATE.md"
        self._original_state: bytes | None = None
        self._created_required: list[Path] = []

    def install(self) -> None:
        (self.home / "bin").mkdir(parents=True, exist_ok=True)
        writer = self.home / "bin" / "saipen_writer.py"
        writer.write_text(_WRITER_SOURCE, encoding="utf-8")
        (self.home / "bin" / "saipen_contract.json").write_text(
            json.dumps(_CONTRACT, indent=1), encoding="utf-8"
        )
        python = sys.executable.replace("\\", "/")
        shim_args = {"python": python, "writer": str(writer).replace("\\", "/")}
        (self.home / "bin" / "saipen").write_text(
            _POSIX_SHIM.format(**shim_args), encoding="utf-8"
        )
        (self.home / "bin" / "saipen.cmd").write_text(
            _WIN_SHIM.format(**shim_args), encoding="utf-8"
        )
        if os.name != "nt":
            shim = self.home / "bin" / "saipen"
            shim.chmod(shim.stat().st_mode | stat_module.S_IEXEC)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        # The canonical contract names the four core files. Small unrelated
        # fidelity fixtures often provide only their own lowercase control
        # files; create the missing declared files so the stub's generated
        # contract is authoritative for the fixture it is helping.
        for name in ("BOARD.md", "LOG.md", "IDENTITY.md"):
            required = self.project_root / ".saipen" / name
            if not required.exists():
                required.write_text(f"test fixture {name}\n", encoding="utf-8")
                self._created_required.append(required)
        # Declare the stub home in the project's STATE.md so the gate's
        # STATE-key discovery resolves it before PATH. Preserve the original
        # file when present so uninstall restores the fixture verbatim.
        if self._state_path.is_file():
            self._original_state = self._state_path.read_bytes()
            current = self._state_path.read_text(encoding="utf-8", errors="replace")
        else:
            current = ""
        if "saipen_home:" not in current:
            # The declaration must sit on its own line: the gate's parser
            # reads only lines that START with the key, and a fixture file
            # may not end with a newline (or with any line structure).
            line = f"\nsaipen_home: {self.home.as_posix()}\n"
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(current + line, encoding="utf-8")

    def uninstall(self) -> None:
        if self._original_state is not None:
            self._state_path.write_bytes(self._original_state)
            self._original_state = None
        elif self._state_path.exists():
            # We created STATE.md; the contract it now names is gone with the
            # stub home, so remove the file and any enrollment the CLI wrote.
            self._state_path.unlink(missing_ok=True)
        for required in reversed(self._created_required):
            required.unlink(missing_ok=True)
        self._created_required.clear()
        manifest = self.project_root / ".saipen" / "MANIFEST.json"
        if manifest.exists():
            manifest.unlink()
        import shutil

        shutil.rmtree(self.home, ignore_errors=True)
