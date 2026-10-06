"""AUDAPACK Browser Widget manager and installation helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Optional

from audapack.config import app_dir, get_user_runtime_dir
from audapack.procutil import popen_hidden, run_hidden

WIDGET_FILE_NAME = "AUDAPACK_WIDGET.user.js"

#: The canonical release ledger: the version + digest of the bytes this
#: repository currently ships. See `widget_release_errors`.
WIDGET_RELEASE_FILE_NAME = "AUDAPACK_WIDGET.release.json"
WIDGET_RELEASE_SCHEMA = 1

# Windows browser detection candidates: (display name, candidate paths).
# Detected from well-known install locations and portable drives.
# NOTE: Brave is deliberately not a launch candidate. The AUDAPACK worker is
# the dedicated isolated Chromium profile only; a running Brave tab is still
# detected and honestly reported to the Bridge (browser_name/is_brave), but it
# is never selected to host the worker.
BROWSER_CANDIDATES: list[tuple[str, list[str]]] = [
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
            # P0-1: the GUI and the Bridge daemon run without a console. A
            # bare powershell spawn from them allocates a NEW console -- a
            # black window that flashes and steals focus on every worker
            # launch. run_hidden applies CREATE_NO_WINDOW + SW_HIDE.
            res = run_hidden(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=3,
            )
            out = res.stdout or ""

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


#: Parsed userscript headers, keyed on (path, mtime_ns, size) -- the same key
#: the Bridge's own bundle cache uses. Bounded in practice: one entry per file
#: revision seen in a process's lifetime.
_WIDGET_METADATA_CACHE: dict[tuple[str, int, int], dict[str, str]] = {}


def read_bundled_widget_metadata() -> dict[str, str]:
    """The bundled userscript's @name and @version, read once per revision.

    The file is ~841 KB and this re-read and re-scanned all of it on every
    call, with no cache. It sits under _get_required_widget_build(), so ONE
    dispatcher.status() with six live workers did seven full reads, a
    /v1/browser/status response thirteen, and a single /v1/browser/poll nine --
    on a four-second poll, for a release marker that changes when the operator
    upgrades the widget and at no other time. Measured before: 3.76 ms and
    802 KB per call; 1.02 s and 401 MiB per 500.

    Keyed on (path, mtime_ns, size) exactly like the Bridge's own
    _get_widget_bundle_info, so a bundle replaced under a live Bridge is picked
    up on the next call instead of needing a restart.
    """
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
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _WIDGET_METADATA_CACHE.get(key)
        if cached is not None:
            # A copy: callers have always been handed a dict they may mutate.
            return dict(cached)

    try:
        content = path.read_text(encoding="utf-8")
        m_ver = re.search(r"//\s*@version\s+([^\r\n]+)", content)
        if m_ver:
            meta["version"] = m_ver.group(1).strip()
        m_name = re.search(r"//\s*@name\s+([^\r\n]+)", content)
        if m_name:
            meta["name"] = m_name.group(1).strip()
    except Exception:
        # An unreadable bundle is NOT cached: the next call has to try again
        # rather than pin the placeholder version for the life of the process.
        return meta

    if key is not None:
        _WIDGET_METADATA_CACHE[key] = dict(meta)
    return meta


def get_widget_release_path() -> Path:
    """The canonical release ledger beside the bundled userscript."""
    return app_dir() / "resources" / WIDGET_RELEASE_FILE_NAME


def read_widget_release() -> dict | None:
    """The persisted release record, or None when it is absent/unreadable."""
    path = get_widget_release_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: One numeric version segment. Deliberately ASCII-only: a Unicode digit that
#: `str.isdigit()` accepts is not a version segment any userscript manager
#: compares, so collapsing it to an integer here would invent an ordering.
_VERSION_SEGMENT = re.compile(r"\A[0-9]+\Z")


def widget_version_key(value: str) -> tuple[int, ...]:
    """THE canonical numeric userscript version comparator.

    One owner for "which version is newer" (TARGET D): the recorder, the update
    probe and any future release check all order versions through this, so a
    release can never be strictly newer to one consumer and equal to another.

    Returns a tuple of non-negative ints; compares lexicographically
    (`widget_version_key("0.0.59") < widget_version_key("0.0.60")`).

    Raises ``ValueError`` for anything outside the numeric contract. Release
    identity is a strictly-increasing numeric sequence; a prerelease suffix
    such as ``0.0.60-beta`` has no defined order in this project's contract, so
    it is REJECTED explicitly instead of being silently compared as equal to
    ``0.0.60`` (which would launder exactly the drift T-196 filed).
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("version is empty")
    if "-" in text or "+" in text:
        raise ValueError(f"prerelease/build suffix is unsupported: {value!r}")
    key: list[int] = []
    for segment in text.split("."):
        if not _VERSION_SEGMENT.match(segment):
            raise ValueError(f"non-numeric version segment {segment!r} in {value!r}")
        key.append(int(segment))
    return tuple(key)


#: Ledger read states. ABSENT and MALFORMED are different mechanical facts:
#: a first record is a bootstrap, a corrupt ledger is a fail-closed refusal.
RELEASE_LEDGER_ABSENT = "ABSENT"
RELEASE_LEDGER_OK = "OK"
RELEASE_LEDGER_MALFORMED = "MALFORMED"


def load_widget_release_state() -> tuple[str, dict | None]:
    """The persisted ledger and HOW it read: (state, record)."""
    path = get_widget_release_path()
    if not path.exists():
        return RELEASE_LEDGER_ABSENT, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RELEASE_LEDGER_MALFORMED, None
    if not isinstance(data, dict):
        return RELEASE_LEDGER_MALFORMED, None
    return RELEASE_LEDGER_OK, data


def write_widget_release(record: dict, path: Path | None = None) -> None:
    """Atomically replace the release ledger with ``record``.

    Written to a sibling temp file, fsynced, then ``os.replace``d, so a crash
    or a failed write can never truncate or destroy the PREVIOUS valid ledger
    (TARGET B). The parent directory is never left holding the temp file.
    """
    target = path or get_widget_release_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".widget-release-", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def widget_release_transition(
    current: dict, previous: dict | None
) -> tuple[bool, str, str]:
    """Is recording ``current`` over ``previous`` a legal release transition?

    THE monotonic release-identity rule (TARGET A). ``@version`` is the only
    thing an installed userscript manager compares, so two different builds
    shipped under one version string are one build as far as every browser is
    concerned. This answers, mechanically, whether the ledger may move:

      * no previous ledger            -> allowed (bootstrap);
      * identical bytes               -> allowed (idempotent no-op);
      * different bytes               -> allowed ONLY when the current
                                         ``@version`` is STRICTLY GREATER than
                                         the recorded one;
      * different bytes, same/lower   -> REFUSED, ledger untouched.

    Returns ``(allowed, code, detail)``; on refusal the code names the exact
    reason and ``detail`` is the human sentence.

    Both versions are parsed by the ONE canonical comparator. An unsupported
    (prerelease/non-numeric) version on either side is a refusal, never a
    silent comparison: releasing inside an ordering the project never defined
    is how the T-196 defect would come back.
    """
    if previous is None:
        return True, "BOOTSTRAP", "no previous recorded release"
    previous_sha = str(previous.get("sha256") or "").lower()
    previous_version = str(previous.get("version") or "")
    current_sha = str(current.get("sha256") or "").lower()
    current_version = str(current.get("version") or "")

    if current_sha and current_sha == previous_sha:
        # Identical bytes: nothing to release. The version cannot differ for
        # the same bytes (the version lives IN the bytes), so a mismatch means
        # the ledger was already inconsistent -- fail closed rather than bless.
        if current_version != previous_version:
            return (
                False,
                "LEDGER_VERSION_MISMATCH",
                f"recorded bytes hash to {previous_sha[:16]}... but the ledger "
                f"names version {previous_version!r} while the script declares "
                f"{current_version!r}; the ledger is inconsistent, refusing to "
                "rewrite it",
            )
        return True, "NO_OP", "bytes and version are unchanged -- nothing to record"

    try:
        current_key = widget_version_key(current_version)
    except ValueError as exc:
        return False, "UNSUPPORTED_VERSION", f"current @version {current_version!r}: {exc}"
    try:
        previous_key = widget_version_key(previous_version)
    except ValueError as exc:
        return (
            False,
            "MALFORMED_LEDGER",
            f"recorded release version {previous_version!r} is not a supported "
            f"numeric version: {exc}",
        )

    if current_key <= previous_key:
        return (
            False,
            "VERSION_NOT_INCREASED",
            f"shipped bytes changed but // @version is {current_version!r}, "
            f"which is not strictly greater than the recorded {previous_version!r}; "
            "an installed userscript manager would never install these bytes. "
            "Bump // @version and record again",
        )
    return True, "RECORDED", f"{previous_version} -> {current_version}"


def record_widget_release(
    *, bootstrap: bool = False, ledger_path: Path | None = None
) -> dict:
    """Validate and atomically record the current bytes' release identity.

    The mechanical half of T-196's fix (TARGETS A/B/D). Order is deliberate:

        read previous ledger -> read current bytes -> parse version ->
        compute SHA -> validate transition -> atomic write -> post-write verify

    Validation happens BEFORE any write, so the evidence needed to detect
    same-version drift is never destroyed by a failed or illegal record. On
    refusal the previous valid ledger survives byte-for-byte.

    ``bootstrap`` is the ONE explicit recovery path: it permits the FIRST
    record over a missing OR malformed ledger. It changes nothing about the
    monotonic rule for an existing well-formed ledger.

    Returns a structured result (never raises for a protocol refusal):
    ``{"ok", "code", "detail", "record", "previous", "wrote"}``.
    """
    target = ledger_path or get_widget_release_path()
    current = widget_release_record()
    if not current.get("version"):
        return {
            "ok": False,
            "code": "NO_VERSION",
            "detail": f"{get_bundled_widget_path()} declares no // @version",
            "record": current,
            "previous": None,
            "wrote": False,
        }

    state, previous = load_widget_release_state() if ledger_path is None else _load_ledger_at(target)
    if state != RELEASE_LEDGER_OK and not bootstrap:
        if state == RELEASE_LEDGER_MALFORMED:
            return {
                "ok": False,
                "code": "MALFORMED_LEDGER",
                "detail": f"release ledger {target} is unreadable or not a JSON object; "
                "refusing to overwrite it (use --bootstrap only to re-establish a "
                "known-good ledger)",
                "record": current,
                "previous": None,
                "wrote": False,
            }
        return {
            "ok": False,
            "code": "MISSING_LEDGER",
            "detail": f"no release ledger at {target}; the FIRST record is an explicit "
            "bootstrap (run with --bootstrap)",
            "record": current,
            "previous": None,
            "wrote": False,
        }
    if state != RELEASE_LEDGER_OK:
        previous = None
    allowed, code, detail = widget_release_transition(current, previous)
    if not allowed:
        return {
            "ok": False,
            "code": code,
            "detail": detail,
            "record": current,
            "previous": previous,
            "wrote": False,
        }
    if code == "NO_OP":
        return {
            "ok": True,
            "code": code,
            "detail": detail,
            "record": current,
            "previous": previous,
            "wrote": False,
        }

    write_widget_release(current, path=target)
    verify_state, written = _load_ledger_at(target)
    if verify_state != RELEASE_LEDGER_OK or not _records_agree(current, written):
        return {
            "ok": False,
            "code": "WRITE_VERIFY_FAILED",
            "detail": f"the release ledger at {target} did not read back as the "
            "recorded release; the previous ledger was replaced and must be "
            "re-recorded",
            "record": current,
            "previous": previous,
            "wrote": True,
        }
    return {
        "ok": True,
        "code": code,
        "detail": detail,
        "record": current,
        "previous": previous,
        "wrote": True,
    }


def _load_ledger_at(path: Path) -> tuple[str, dict | None]:
    """Ledger state at an explicit path (the recorder's testable seam)."""
    if not path.exists():
        return RELEASE_LEDGER_ABSENT, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RELEASE_LEDGER_MALFORMED, None
    if not isinstance(data, dict):
        return RELEASE_LEDGER_MALFORMED, None
    return RELEASE_LEDGER_OK, data


def _records_agree(current: dict, written: dict | None) -> bool:
    if not isinstance(written, dict):
        return False
    for field in ("version", "sha256", "script", "schema"):
        if current.get(field) != written.get(field):
            return False
    return True


def widget_release_record(version: str | None = None, sha256: str | None = None) -> dict:
    """The release record for the CURRENT bytes (or the ones supplied)."""
    if version is None or sha256 is None:
        path = get_bundled_widget_path()
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        match = re.search(rb"//\s*@version\s+([^\r\n]+)", data)
        version = match.group(1).strip().decode("ascii", "ignore") if match else ""
    return {
        "schema": WIDGET_RELEASE_SCHEMA,
        "script": WIDGET_FILE_NAME,
        "version": version,
        "sha256": sha256,
    }


def widget_release_errors() -> list[str]:
    """Does the shipped userscript's BYTES identity match its VERSION?

    T-196. `@version` is the only thing Tampermonkey compares when it decides
    whether an installed script is current, so two materially different builds
    shipped under one version string are one build as far as every installed
    browser is concerned: the repository tests exercise the new bytes while the
    operator's browser keeps executing the old ones.

    The invariant is therefore: DIFFERENT SHIPPED BYTES REQUIRE A DIFFERENT,
    STRICTLY INCREASING VERSION. It is checked against the release ledger
    (`AUDAPACK_WIDGET.release.json`), which is committed beside the bundle and
    records the version and SHA-256 that were shipped together. Timestamps are
    deliberately NOT release identity -- a checkout rewrites them.

    Returns a list of human-readable violations; empty means consistent.
    """
    errors: list[str] = []
    path = get_bundled_widget_path()
    if not path.is_file():
        return [f"bundled userscript is missing: {path}"]

    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    match = re.search(rb"//\s*@version\s+([^\r\n]+)", data)
    if not match:
        return [f"{WIDGET_FILE_NAME} declares no // @version"]
    version = match.group(1).strip().decode("ascii", "ignore")

    ledger = read_widget_release()
    if ledger is None:
        return [
            f"release ledger missing or unreadable: {get_widget_release_path()} -- "
            f"record the shipped version {version!r} and its SHA-256 "
            "(use scripts/update_widget_release.py)"
        ]

    recorded_version = str(ledger.get("version") or "")
    recorded_sha = str(ledger.get("sha256") or "").lower()
    if recorded_sha != sha256:
        errors.append(
            f"shipped bytes changed without a release: {WIDGET_FILE_NAME} is "
            f"{sha256[:16]}... but the release ledger records "
            f"{recorded_sha[:16]}... as version {recorded_version!r}; bump "
            "// @version and re-record the release"
        )
        if recorded_version and version == recorded_version:
            errors.append(
                f"different bytes still declare the same // @version "
                f"{version!r} -- an installed userscript manager sees them as "
                "one build, so the new bytes never reach the browser"
            )
    elif recorded_version != version:
        errors.append(
            f"release ledger records version {recorded_version!r} for these "
            f"bytes but the script declares {version!r}; the two must agree"
        )
    return errors


CHROMIUM_KEEPALIVE_FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,TabDiscarding,MemorySaverMode,ChromeWhatsNewUI",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    # A worker window must land on ChatGPT and nothing else. Chrome's own
    # interstitials -- the post-update "What's New" tab, the search engine
    # choice screen, the profile picker, the first-run flow -- open INSTEAD of
    # the requested URL, which is how a START AUDIT press produced a window
    # that flashed up and never became a worker.
    "--disable-search-engine-choice-screen",
    "--no-service-autorun",
    "--disable-fre",
    "--suppress-message-center-popups",
]

# Kept out: the old BRAVE_KEEPALIVE_FLAGS alias and the Brave launch paths
# were retired -- the flags apply to every dedicated Chromium worker.

AUDAPACK_WORKER_URL = "https://chatgpt.com/?audapack_worker=1"


def _is_brave_exe(exe_path: str) -> bool:
    """True for a Brave executable. Brave is detected and REPORTED to the
    Bridge, but never selected to host the dedicated worker."""
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
    """Select the browser for the dedicated worker.

    The worker is the dedicated isolated Chromium profile only. Brave is
    never selected -- not as an explicit choice, not via preferred_browser,
    not as a detected candidate -- even though a running Brave tab stays
    visible to the Bridge through the worker protocol.
    """
    if browser_exe:
        candidate = Path(browser_exe)
        if candidate.is_file() and _is_chromium_exe(str(candidate)) and not _is_brave_exe(str(candidate)):
            return str(candidate.resolve())
        return None

    cfg = None
    try:
        from audapack.config import load_config
        cfg = load_config()
        preferred = str(getattr(cfg.ui, "preferred_browser", "") or "")
        if preferred:
            candidate = Path(preferred)
            if candidate.is_file() and _is_chromium_exe(str(candidate)) and not _is_brave_exe(str(candidate)):
                return str(candidate.resolve())
    except Exception:
        pass

    priority = {
        "Google Chrome": 0,
        "Cent Browser": 1,
        "Microsoft Edge": 2,
        "Vivaldi": 3,
        "Opera": 4,
    }
    candidates = [
        item for item in detect_installed_browsers()
        if _is_chromium_exe(str(item.get("exe") or ""))
        and not _is_brave_exe(str(item.get("exe") or ""))
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
    new_window: bool = True,
) -> list[str]:
    """Build the isolated worker launch command without starting a process.

    ``new_window`` off opens the target as a TAB in whatever window the profile
    already has. A worker lane wants its own window; the userscript installer
    does not, and forcing one on it is how pressing Install Widget started
    producing two windows -- an empty warmed one and the installer beside it.
    """
    if not _is_chromium_exe(browser_exe):
        raise ValueError("AUDAPACK worker requires a Chromium-family browser")
    return [
        browser_exe,
        *CHROMIUM_KEEPALIVE_FLAGS,
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        *(["--new-window"] if new_window else []),
        target,
    ]


def _launch_dedicated_chromium(
    target: str,
    browser_exe: Optional[str] = None,
    new_window: bool = True,
) -> tuple[bool, str, Optional[str], Path]:
    selected = select_dedicated_chromium(browser_exe)
    profile = get_dedicated_chromium_profile_dir()
    if not selected:
        return False, "No supported Chromium browser was found.", None, profile
    profile.mkdir(parents=True, exist_ok=True)
    try:
        command = dedicated_chromium_command(selected, profile, target, new_window)
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
        # popen_hidden ORs in CREATE_NO_WINDOW: a portable launcher that is
        # itself a console program must not flash a black window over the
        # worker it is opening.
        popen_hidden(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception as exc:
        return False, f"Failed to launch dedicated Chromium: {exc}", selected, profile
    return True, "", selected, profile


def launch_dedicated_chromium_worker(
    browser_exe: Optional[str] = None,
    *,
    managed_slot: Optional[int] = None,
    managed_generation: Optional[int] = None,
) -> tuple[bool, str]:
    """Launch an isolated Chromium profile configured for background work.

    Chromium flags prevent timer/renderer throttling for minimized or occluded
    windows. They cannot run JavaScript while Windows itself is asleep or
    hibernating.
    """
    target = AUDAPACK_WORKER_URL
    if managed_slot is not None and managed_generation is not None:
        slot = max(1, min(6, int(managed_slot)))
        generation = max(1, int(managed_generation))
        target = f"{AUDAPACK_WORKER_URL}&audapack_worker_slot={slot}&audapack_worker_generation={generation}"
    ok, error, selected, profile = _launch_dedicated_chromium(target, browser_exe)
    if not ok or not selected:
        return False, error
    return True, f"AUDAPACK Chromium started ({_clean_browser_name('', selected)}; profile: {profile})."


#: A plain ChatGPT window in the worker profile. No ``audapack_worker_slot``,
#: so the widget never claims it as a managed lane and the dispatcher never
#: sends work to it -- it is the operator's own window, signed in like the rest
#: of the profile, for an audit they drive by hand.
MANUAL_WINDOW_URL = "https://chatgpt.com/"


def open_manual_chromium_window(browser_exe: Optional[str] = None) -> tuple[bool, str]:
    """Open an unclaimed window in the worker profile for a hand-run audit.

    Always its own window: the point is a place to drop an archive into, and a
    tab added to a lane that is mid-audit is not that.
    """
    ok, error, selected, profile = _launch_dedicated_chromium(
        MANUAL_WINDOW_URL, browser_exe, new_window=True
    )
    if not ok or not selected:
        return False, error
    return True, (
        f"Manual audit window opened in AUDAPACK Chromium "
        f"({_clean_browser_name('', selected)}; profile: {profile}). Drop an archive into it."
    )


def open_widget_in_dedicated_chromium(
    browser_exe: Optional[str] = None,
    use_bridge: bool = False,
    bridge_url: Optional[str] = None,
    new_window: bool = True,
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
    ok, error, selected, profile = _launch_dedicated_chromium(target, browser_exe, new_window)
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
            popen_hidden(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
