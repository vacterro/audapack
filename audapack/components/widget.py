"""AUDAPACK Browser Widget manager and installation helper."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional

from audapack.config import app_dir, get_user_runtime_dir

WIDGET_FILE_NAME = "AUDAPACK_WIDGET.user.js"

# Windows browser detection candidates: (display name, candidate paths).
# Detected from well-known install locations and portable drives.
BROWSER_CANDIDATES: list[tuple[str, list[str]]] = [
    ("Brave Browser", [
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"V:\___VAC\__P\__SOFT\_BRAVE\app\brave.exe",
        r"V:\___VAC\__P\__SOFT\_BRAVE\brave-portable.exe",
        r"D:\___VAC\__P\__SOFT\_BRAVE\app\brave.exe",
        r"C:\Brave\brave.exe",
    ]),
    ("Cent Browser", [
        r"V:\___VAC\__P\_CENT\chrome.exe",
        r"%LOCALAPPDATA%\CentBrowser\Application\chrome.exe",
        r"%ProgramFiles%\CentBrowser\Application\chrome.exe",
    ]),
    ("Google Chrome", [
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    ]),
    ("Microsoft Edge", [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    ]),
    ("Mozilla Firefox", [
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
        r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe",
    ]),
    ("Opera", [
        r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
        r"%ProgramFiles%\Opera\opera.exe",
        r"V:\___VAC\__P\__SOFT\_OPERA\opera.exe",
    ]),
    ("Vivaldi", [
        r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
        r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe",
    ]),
]

KNOWN_BROWSER_NAMES = {
    "brave.exe", "brave-portable.exe", "chrome.exe", "msedge.exe",
    "opera.exe", "firefox.exe", "vivaldi.exe", "cent.exe", "arc.exe",
    "zen.exe", "librewolf.exe", "waterfox.exe", "yandex.exe"
}


def _expand_candidate(path: str) -> Path:
    return Path(os.path.expandvars(path))


def _clean_browser_name(raw_name: str, exe_path: str) -> str:
    lower_path = exe_path.lower()
    lower_name = raw_name.lower()
    if "brave" in lower_path or "brave" in lower_name:
        return "Brave Browser"
    if "cent" in lower_path or "cent" in lower_name:
        return "Cent Browser"
    if "opera" in lower_path or "opera" in lower_name:
        return "Opera"
    if "edge" in lower_path or "edge" in lower_name or "msedge" in lower_path:
        return "Microsoft Edge"
    if "firefox" in lower_path or "firefox" in lower_name:
        return "Mozilla Firefox"
    if "vivaldi" in lower_path or "vivaldi" in lower_name:
        return "Vivaldi"
    if "chrome" in lower_path or "chrome" in lower_name:
        return "Google Chrome"
    stem = Path(exe_path).stem.replace("-portable", "").replace("_", " ")
    return stem.title() or "Browser"


def detect_installed_browsers() -> list[dict[str, any]]:
    """Return installed browsers as [{name, exe, running}], in priority order.

    Combines running browser processes, Windows Registry registrations,
    and filesystem candidate locations (including portable paths).
    """
    found: dict[str, dict[str, any]] = {}

    # 1. Running browser processes (highest relevance to active user)
    if sys.platform == "win32":
        try:
            ps_cmd = (
                '$names = @("brave","brave-portable","chrome","msedge","opera","firefox","vivaldi","cent","arc","zen"); '
                'Get-Process -ErrorAction SilentlyContinue | '
                'Where-Object { $names -contains $_.ProcessName } | '
                'Select-Object -ExpandProperty Path -Unique'
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                text=True,
                errors="ignore",
                timeout=3,
            )
            for line in out.strip().splitlines():
                line = line.strip().strip('"')
                if line and os.path.exists(line) and line.lower().endswith(".exe"):
                    base_name = Path(line).name.lower()
                    if not any(skip in base_name for skip in ["crashreporter", "installer", "update", "notification"]):
                        norm = str(Path(line).resolve()).lower()
                        name = _clean_browser_name("", line)
                        found[norm] = {"name": name, "exe": str(Path(line).resolve()), "running": True}
        except Exception:
            pass

    # 2. Windows Registry
    if sys.platform == "win32":
        try:
            import winreg

            # 2a. StartMenuInternet
            roots = [
                (winreg.HKEY_CURRENT_USER, r"Software\Clients\StartMenuInternet"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Clients\StartMenuInternet"),
            ]
            for root, key_path in roots:
                try:
                    with winreg.OpenKey(root, key_path) as k:
                        num = winreg.QueryInfoKey(k)[0]
                        for i in range(num):
                            sub = winreg.EnumKey(k, i)
                            try:
                                with winreg.OpenKey(root, rf"{key_path}\{sub}\shell\open\command") as ck:
                                    cmd, _ = winreg.QueryValueEx(ck, "")
                                    m = re.match(r'^"([^"]+)"', cmd.strip())
                                    path_str = m.group(1) if m else cmd.strip().split()[0]
                                    if path_str and os.path.exists(path_str):
                                        norm = str(Path(path_str).resolve()).lower()
                                        if norm not in found:
                                            found[norm] = {
                                                "name": _clean_browser_name(sub, path_str),
                                                "exe": str(Path(path_str).resolve()),
                                                "running": False,
                                            }
                            except Exception:
                                pass
                except Exception:
                    pass

            # 2b. App Paths
            app_roots = [
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            ]
            for root, key_path in app_roots:
                try:
                    with winreg.OpenKey(root, key_path) as k:
                        num = winreg.QueryInfoKey(k)[0]
                        for i in range(num):
                            sub = winreg.EnumKey(k, i)
                            if sub.lower() in KNOWN_BROWSER_NAMES:
                                try:
                                    with winreg.OpenKey(root, rf"{key_path}\{sub}") as ck:
                                        cmd, _ = winreg.QueryValueEx(ck, "")
                                        m = re.match(r'^"([^"]+)"', cmd.strip())
                                        path_str = m.group(1) if m else (cmd.strip().split()[0] if cmd.strip() else "")
                                        if path_str and os.path.exists(path_str):
                                            norm = str(Path(path_str).resolve()).lower()
                                            if norm not in found:
                                                found[norm] = {
                                                    "name": _clean_browser_name(sub, path_str),
                                                    "exe": str(Path(path_str).resolve()),
                                                    "running": False,
                                                }
                                except Exception:
                                    pass
                except Exception:
                    pass
        except Exception:
            pass

    # 3. Known candidate filesystem paths
    for name, candidates in BROWSER_CANDIDATES:
        for cand in candidates:
            exe = _expand_candidate(cand)
            if exe.exists():
                norm = str(exe.resolve()).lower()
                if norm not in found:
                    found[norm] = {"name": name, "exe": str(exe.resolve()), "running": False}

    # Sort: running browsers first, then alphabetical by name
    result = list(found.values())
    result.sort(key=lambda b: (not b.get("running", False), b["name"].lower()))
    return result


def get_bundled_widget_path() -> Path:
    return app_dir() / "resources" / WIDGET_FILE_NAME


def read_bundled_widget_metadata() -> dict[str, str]:
    path = get_bundled_widget_path()
    meta = {
        "name": "AUDAPACK Widget",
        "version": "0.0.01",
        "exists": False,
        "path": str(path),
    }
    if not path.exists():
        return meta

    meta["exists"] = True
    try:
        content = path.read_text(encoding="utf-8")
        m_ver = re.search(r"//\s*@version\s+([^\r\n]+)", content)
        if m_ver:
            meta["version"] = m_ver.group(1).strip()
        m_name = re.search(r"//\s*@name\s+([^\r\n]+)", content)
        if m_name:
            meta["name"] = m_name.group(1).strip()
    except Exception:
        pass

    return meta


CHROMIUM_KEEPALIVE_FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,TabDiscarding,MemorySaverMode",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
]

# Kept for callers that imported the old name. The flags apply to every
# Chromium worker now, not only Brave.
BRAVE_KEEPALIVE_FLAGS = CHROMIUM_KEEPALIVE_FLAGS

AUDAPACK_WORKER_URL = "https://chatgpt.com/?audapack_worker=1"


def _is_brave_exe(exe_path: str) -> bool:
    """Returns True if the executable path points to a Brave-based browser."""
    lower = Path(exe_path).stem.lower()
    return "brave" in lower


def _is_chromium_exe(exe_path: str) -> bool:
    """Return whether *exe_path* is a supported Chromium-family browser."""
    lower_path = str(exe_path).lower()
    name = Path(exe_path).name.lower()
    if any(token in lower_path for token in ("firefox", "librewolf", "waterfox", "zen")):
        return False
    return name in {
        "brave.exe", "brave-portable.exe", "chrome.exe", "msedge.exe",
        "opera.exe", "vivaldi.exe", "cent.exe", "arc.exe", "yandex.exe",
    }


def get_dedicated_chromium_profile_dir() -> Path:
    """Canonical browser profile used only by the AUDAPACK worker."""
    return get_user_runtime_dir() / "browser_worker" / "chromium_profile"


def select_dedicated_chromium(browser_exe: Optional[str] = None) -> Optional[str]:
    """Choose a stable installed Chromium, preferring non-Brave browsers."""
    if browser_exe:
        candidate = Path(browser_exe)
        return str(candidate.resolve()) if candidate.is_file() and _is_chromium_exe(str(candidate)) else None

    cfg = None
    try:
        from audapack.config import load_config
        cfg = load_config()
        preferred = str(getattr(cfg.ui, "preferred_browser", "") or "")
        if preferred:
            candidate = Path(preferred)
            if candidate.is_file() and _is_chromium_exe(str(candidate)):
                return str(candidate.resolve())
    except Exception:
        pass

    priority = {
        "Google Chrome": 0,
        "Cent Browser": 1,
        "Microsoft Edge": 2,
        "Vivaldi": 3,
        "Opera": 4,
        "Brave Browser": 5,
    }
    candidates = [
        item for item in detect_installed_browsers()
        if _is_chromium_exe(str(item.get("exe") or ""))
        and "ms-playwright" not in str(item.get("exe") or "").lower()
    ]
    candidates.sort(key=lambda item: (
        priority.get(str(item.get("name") or ""), 50),
        not bool(item.get("running", False)),
        str(item.get("exe") or "").lower(),
    ))
    return str(candidates[0]["exe"]) if candidates else None


def dedicated_chromium_command(
    browser_exe: str,
    profile_dir: Path,
    target: str = AUDAPACK_WORKER_URL,
) -> list[str]:
    """Build the isolated worker launch command without starting a process."""
    if not _is_chromium_exe(browser_exe):
        raise ValueError("AUDAPACK worker requires a Chromium-family browser")
    return [
        browser_exe,
        *CHROMIUM_KEEPALIVE_FLAGS,
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--new-window",
        target,
    ]


def _launch_dedicated_chromium(
    target: str,
    browser_exe: Optional[str] = None,
) -> tuple[bool, str, Optional[str], Path]:
    selected = select_dedicated_chromium(browser_exe)
    profile = get_dedicated_chromium_profile_dir()
    if not selected:
        return False, "No supported Chromium browser was found.", None, profile
    profile.mkdir(parents=True, exist_ok=True)
    try:
        command = dedicated_chromium_command(selected, profile, target)
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception as exc:
        return False, f"Failed to launch dedicated Chromium: {exc}", selected, profile
    return True, "", selected, profile


def launch_dedicated_chromium_worker(browser_exe: Optional[str] = None) -> tuple[bool, str]:
    """Launch an isolated Chromium profile configured for background work.

    Chromium flags prevent timer/renderer throttling for minimized or occluded
    windows. They cannot run JavaScript while Windows itself is asleep or
    hibernating.
    """
    ok, error, selected, profile = _launch_dedicated_chromium(AUDAPACK_WORKER_URL, browser_exe)
    if not ok or not selected:
        return False, error
    return True, f"AUDAPACK Chromium started ({_clean_browser_name('', selected)}; profile: {profile})."


def open_widget_in_dedicated_chromium(
    browser_exe: Optional[str] = None,
    use_bridge: bool = False,
    bridge_url: Optional[str] = None,
) -> tuple[bool, str]:
    """Open the widget installer inside the same isolated worker profile."""
    widget = get_bundled_widget_path()
    if not widget.exists():
        return False, "Bundled AUDAPACK Widget was not found."
    if use_bridge and not bridge_url:
        from audapack.config import load_config
        cfg = load_config()
        bridge_url = f"http://{cfg.bridge.host}:{cfg.bridge.port}/widget.user.js"
    target = str(bridge_url) if use_bridge else widget.as_uri()
    ok, error, selected, profile = _launch_dedicated_chromium(target, browser_exe)
    if not ok or not selected:
        return False, error
    return True, f"Widget installer opened in AUDAPACK Chromium ({_clean_browser_name('', selected)}; profile: {profile})."


def open_widget_in_browser(browser_exe: Optional[str] = None, use_bridge: bool = False) -> bool:
    """Open the widget for Tampermonkey installation in a chosen browser.

    - ``browser_exe``: explicit browser executable path. When None, checks preferred_browser in config
      before falling back to the system default browser.
    - ``use_bridge``: prefer the Bridge-served URL (http://127.0.0.1:17843/widget.user.js)
      over the local file:// URI. The Bridge must be running.
    Returns True when the launch was attempted.
    """
    widget = get_bundled_widget_path()
    if not widget.exists():
        return False

    from audapack.config import load_config
    cfg = load_config()

    if use_bridge:
        target = f"http://{cfg.bridge.host}:{cfg.bridge.port}/widget.user.js"
    else:
        target = widget.as_uri()

    if not browser_exe and getattr(cfg.ui, "preferred_browser", None):
        cand = Path(cfg.ui.preferred_browser)
        if cand.exists():
            browser_exe = str(cand)

    if browser_exe:
        try:
            args = [browser_exe]
            if _is_chromium_exe(browser_exe):
                args.extend(CHROMIUM_KEEPALIVE_FLAGS)
            args.append(target)
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    try:
        opened = webbrowser.open(target)
        return bool(opened)
    except Exception:
        return False


def open_widget_installation(browser_exe: Optional[str] = None) -> bool:
    """Helper: open the bundled widget in the chosen, preferred, or default browser."""
    return open_widget_in_browser(browser_exe=browser_exe, use_bridge=False)
