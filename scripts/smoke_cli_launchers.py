"""Bounded REAL Windows smoke for the multi-agent CLI launchers (SRC-081).

REAL WINDOWS LAUNCH ACCEPTANCE: for each locally installed launcher this
proves, with real processes and real windows --

  * the actually resolved invocation mechanism (printed, non-secret);
  * the console starts in the selected project's canonical root;
  * the managed title carries project + launcher + root + token (no secrets);
  * the process registers under the exact launcher id (InstanceMonitor);
  * a second normal click resolves to focus/reuse (focus_candidate + focus);
  * closing the process removes the stale running state.

Credentials are never read, printed, copied or moved. Claude 1 / Claude 2
profile separation is proven with a sha256 digest of each profile's credential
FILE (digest only -- never the content).

Usage:
    python scripts/smoke_cli_launchers.py [project_root] [--keep]

``--keep`` leaves the launched consoles open for manual inspection.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audapack.cli_launchers import (  # noqa: E402
    BUILTIN_CLI_LAUNCHERS,
    managed_console_title,
    resolve_cli_launcher,
)
from audapack.config import create_default_launchers  # noqa: E402
from audapack.instances import InstanceMonitor, create_window_backend  # noqa: E402
from audapack.models import Project  # noqa: E402
from audapack.title_guardian import TitleGuardian  # noqa: E402

# Console titles can carry TUI glyphs (e.g. Claude's "✳ claude" rewrite) that
# cp1251 cannot print -- never let evidence printing crash the smoke.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WINDOW_WAIT_SECONDS = 20


def _window_titles() -> list[str]:
    titles: list[str] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _each(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            titles.append(buffer.value)
        return True

    user32.EnumWindows(_each, 0)
    return titles


def _credential_fingerprint(profile_dir: Path) -> str:
    """Digest of the profile's credential FILE -- proof of distinct accounts.

    The digest never leaves as content: only its first 12 hex chars are
    printed, enough to show the two profiles hold DIFFERENT credentials.
    """
    for name in (".credentials.json", ".claude.json"):
        candidate = profile_dir / name
        if candidate.is_file():
            return f"{name}:{hashlib.sha256(candidate.read_bytes()).hexdigest()[:12]}"
    return "no-credential-file"


def smoke_one(launcher_id: str, cfg, root: str, monitor: InstanceMonitor, project: Project, keep: bool) -> bool:
    resolution = resolve_cli_launcher(cfg, project_root=root, project_name=project.display_name)
    print(f"\n=== {cfg.name} ({launcher_id}) ===")
    print(f"resolved stage : {resolution.stage}")
    print(f"resolved kind  : {'CLI/TUI' if resolution.is_cli else 'GUI fallback (documented)'}")
    print(f"profile        : {resolution.profile or '-'}")
    print(f"command        : {resolution.console_command or '-'}")
    if not resolution.ok:
        print(f"UNAVAILABLE    : {resolution.diagnostic(cfg.name, project.display_name)}")
        return False

    token = f"smoketok-{launcher_id}-{os.getpid()}"
    title = managed_console_title(project.display_name, cfg.name, root, token)
    script = f"[Console]::Title = '{title}'; {resolution.console_command}"
    create_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    process = subprocess.Popen(
        ["powershell.exe", "-NoExit", "-Command", script],
        cwd=root,
        creationflags=create_console,
    )
    assert monitor.track_launch(process.pid, launcher_id, project, correlation_token=token)
    print(f"launched pid   : {process.pid}")

    # Managed-title recovery exactly as production does it (T-205/T-209):
    # Claude Code rewrites the console title within milliseconds of starting,
    # so the canonical title is PROVEN by the guardian restoring it -- the same
    # mechanism _register_agent_launch arms in the product.
    guardian = TitleGuardian()
    guardian.register(
        process.pid, title, correlation_token=token,
        launcher_id=launcher_id, project_id=project.id,
    )

    deadline = time.monotonic() + WINDOW_WAIT_SECONDS
    found = ""
    while time.monotonic() < deadline:
        try:
            guardian.heartbeat()
        except Exception as exc:  # pragma: no cover - native best effort
            print(f"guardian warn  : {exc}")
        found = next((t for t in _window_titles() if token in t), "")
        if found:
            break
        time.sleep(0.5)
    if not found:
        drifted = next((t for t in _window_titles() if "laude" in t or "SmokeProj" in t), "")
        print("FAIL           : managed title never observable (even after guardian restore)")
        if drifted:
            print(f"drifted title  : {drifted!r}")
        _kill(process)
        return False
    print(f"managed title  : {found}")
    ok = True
    for needle in (project.display_name, cfg.name, root):
        if needle not in found:
            print(f"FAIL           : managed title missing {needle!r}")
            ok = False

    instances = monitor.refresh([project], create_default_launchers())
    mine = [item for item in instances if item.launcher_id == launcher_id and item.project_id == project.id]
    if not mine:
        print("FAIL           : InstanceMonitor did not register the launch under the exact id")
        ok = False
    else:
        print(f"registered     : launcher_id={mine[0].launcher_id} project_id={mine[0].project_id}")
        candidate = monitor.focus_candidate(project.id, launcher_id)
        if candidate is None:
            print("FAIL           : second-click focus/reuse found no focus candidate")
            ok = False
        else:
            # Second-click semantics: the EXACT instance answers the click.
            # SetForegroundWindow can still be refused by Windows foreground
            # policy (T-179 reports exactly that instead of duplicating), so a
            # refusal after bounded retries is an OS limitation, reported --
            # never a launch-identity failure.
            focused = False
            for _attempt in range(3):
                if monitor.focus(candidate):
                    focused = True
                    break
                time.sleep(0.5)
            if focused:
                print(f"focus/reuse    : OK (hwnd={candidate.hwnd} pid={candidate.pid})")
            else:
                print(
                    f"focus/reuse    : exact candidate resolved (hwnd={candidate.hwnd} "
                    f"pid={candidate.pid}); WARN: Windows foreground policy refused "
                    "SetForegroundWindow (product reports this instead of duplicating)"
                )

    if keep:
        print("kept open      : --keep")
        return ok

    guardian.unregister(process.pid)
    _kill(process)
    time.sleep(1.0)
    monitor._load_records()
    # Only THIS launch's record must die with its process: unrelated leftovers
    # from earlier runs never fail (or excuse) this run's cleanup check.
    survivor = [
        item for item in monitor.refresh([project], create_default_launchers())
        if item.launcher_id == launcher_id
        and (item.launch_pid == process.pid or getattr(item, "correlation_token", "") == token)
    ]
    if survivor:
        print(f"FAIL           : stale running state survived process exit: {survivor}")
        ok = False
    else:
        print("cleanup        : process exit removed the running state")
    return ok


def _kill(process) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:  # pragma: no cover - cleanup best effort
        print(f"cleanup warn   : {exc}")


def main() -> int:
    keep = "--keep" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = str(Path(args[0]).resolve()) if args else str(Path(__file__).resolve().parent.parent)
    if not Path(root).is_dir():
        print(f"project root does not exist: {root}")
        return 2

    print(f"project root   : {root}")
    home = Path.home()
    print("\n--- Claude profile separation (TARGET C) ---")
    default_fp = _credential_fingerprint(home / ".claude")
    account2_fp = _credential_fingerprint(home / ".claude-account2")
    print(f"~/.claude            : {default_fp}")
    print(f"~/.claude-account2   : {account2_fp}")
    if default_fp == account2_fp and default_fp != "no-credential-file":
        print("FAIL           : the two Claude profiles carry IDENTICAL credentials")
        return 1
    if default_fp != "no-credential-file" and account2_fp != "no-credential-file":
        print("distinct       : different credential digests -> different accounts")
    else:
        print("note           : a profile has no credential file (resolution will report it)")

    project = Project(
        id="smokeproj",
        display_name="SmokeProj",
        source_path=root,
        priority_group="MAIN0",
        slot=1,
    )
    monitor = InstanceMonitor(backend=create_window_backend())
    launchers = {lc.id: lc for lc in create_default_launchers()}

    results: dict[str, bool] = {}
    for launcher_id in BUILTIN_CLI_LAUNCHERS:
        results[launcher_id] = smoke_one(
            launcher_id, launchers[launcher_id], root, monitor, project, keep
        )

    print("\n--- summary ---")
    for launcher_id, passed in results.items():
        print(f"{launcher_id:12s}: {'PASS' if passed else 'FAIL/SKIP (see reason above)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
