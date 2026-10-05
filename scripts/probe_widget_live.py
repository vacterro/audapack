"""LIVE widget verification -- no fixtures, no guessing.

Verifies the widget against the REAL system in one of two ways:

  * page mode -- when the dedicated worker profile is free, the probe launches
    it over CDP at the real worker URL (``?audapack_worker=1``) and inspects the
    live page: widget UI, state label, the widget's own Bridge diagnostics, and
    its self-diagnostic round-trip;
  * registry mode -- when the managed fleet already holds the profile (the
    normal production state), the probe does NOT disturb it. It verifies the
    live worker registry instead: registration, build, worker class taxonomy,
    page eligibility, composer topology -- and reads the classifier's own live
    verdicts (``lineage=...``) out of the mirrored diagnostics log.

Both modes also settle the version question -- but never from a literal baked
into this probe. The expected build is read from canonical runtime truth (the
Bridge's own ``dispatch.required_widget_build``), so the probe cannot go stale
on the next widget release. Read-only towards the ChatGPT account. Run it
directly:

    python scripts/probe_widget_live.py
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEBUG_PORT = 9222
BRIDGE = "http://127.0.0.1:17843"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def bridge_get(path: str):
    from audapack.bridge.server import _live_bridge_token

    req = urllib.request.Request(
        f"{BRIDGE}{path}",
        headers={"Authorization": f"Bearer {_live_bridge_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as exc:  # the probe reports; it never hides
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def registry_workers() -> list[dict]:
    # NB: workers live under "dispatch" -- reading them at the top level is how
    # an earlier revision of this probe reported a healthy fleet as empty.
    return bridge_get("/v1/browser/status").get("dispatch", {}).get("workers", [])


def classify_worker_build(required: str, worker: str) -> str:
    """``current`` / ``stale`` / ``unknown``, derived -- no literal version.

    Equal builds are current; any other worker build is stale; a missing side
    is unknown rather than a false PASS.
    """
    required = str(required or "")
    worker = str(worker or "")
    if not required or not worker:
        return "unknown"
    return "current" if worker == required else "stale"


def required_widget_build() -> str:
    """Canonical expected build: what the running Bridge itself requires.

    Read from ``GET /v1/browser/status`` -> ``dispatch.required_widget_build``
    so the probe judges the live fleet against the value the dispatcher will
    actually enforce. The same helper the Bridge uses is the fallback for when
    the endpoint is unreachable.
    """
    reported = bridge_get("/v1/browser/status").get("dispatch", {}).get("required_widget_build")
    if reported:
        return str(reported)
    from audapack.bridge.browser_dispatch import _get_required_widget_build

    return str(_get_required_widget_build() or "")


def profile_in_use() -> bool:
    """Is the dedicated worker profile already held by a running Chromium?

    Checked BEFORE any launch: Chrome is a per-profile singleton, so a launch
    against a busy profile does not open a browser at all -- it forwards its
    URL into the live fleet window (an unwanted navigation in a window that may
    be mid-run). The probe must never do that.
    """
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\").CommandLine"],
        capture_output=True, text=True, timeout=30,
    )
    return "browser_worker" in (out.stdout or "")


def mirror_log_path() -> Path:
    from audapack.config import get_user_runtime_dir

    return Path(get_user_runtime_dir()) / "logs" / "widget_diagnostics.log"


def live_lineage_verdicts(limit: int = 5) -> list[str]:
    path = mirror_log_path()
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[::-1]:
        if "lineage=" in line:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            ts = datetime.datetime.fromtimestamp(entry.get("at", 0) / 1000).strftime("%H:%M:%S")
            msg = str(entry.get("message") or "")
            verdict = msg[msg.index("lineage="):][:180]
            out.append(f"{ts} {entry.get('event')}: {verdict}")
            if len(out) >= limit:
                break
    return out


def main() -> int:
    # The live labels carry UI glyphs (U+25BE and friends); the Windows console
    # defaults to cp1251 and must not crash the probe over one character.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    findings: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        findings.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # ---- shared: the live registry and the mirrored classifier verdicts ----
    def check_registry() -> None:
        workers = registry_workers()
        required = required_widget_build()
        print(f"      required_build={required!r} (source: canonical Bridge runtime truth)")
        if workers:
            for w in workers:
                build = w.get("widget_build_version")
                verdict = classify_worker_build(required, build)
                record(
                    "worker registered",
                    True,
                    f"id={str(w.get('worker_id'))[:8]} state={w.get('state')!r} "
                    f"build={build!r} class={w.get('worker_class')!r} "
                    f"eligible={w.get('page_eligible')} turns={w.get('has_conversation_turns')} "
                    f"clean={w.get('clean_for_audit')} slot={w.get('managed_slot')}",
                )
                record("widget build current",
                       verdict == "current" and w.get("worker_class") != "STALE_WIDGET",
                       f"required_build={required!r} worker_build={build!r} "
                       f"verdict={verdict} class={w.get('worker_class')!r}")
                topo = str(w.get("upload_topology") or "")
                record("live composer topology reported", "composer=" in topo, topo[:160])
        else:
            record("worker registered", False, "no worker registered with the Bridge")

        verdicts = live_lineage_verdicts()
        record("live classifier verdicts in mirror log", bool(verdicts),
               " || ".join(verdicts) if verdicts else "no lineage= entries mirrored yet")

    from audapack.components.widget import (
        AUDAPACK_WORKER_URL,
        dedicated_chromium_command,
        get_dedicated_chromium_profile_dir,
        select_dedicated_chromium,
    )

    if profile_in_use():
        print("      profile is held by the live managed fleet -- registry mode (fleet undisturbed)")
        check_registry()
        proc = None
        page_mode = False
    else:
        cmd = dedicated_chromium_command(
            select_dedicated_chromium(), get_dedicated_chromium_profile_dir(), AUDAPACK_WORKER_URL
        )
        cmd.insert(1, f"--remote-debugging-port={DEBUG_PORT}")
        proc = subprocess.Popen(cmd)
        time.sleep(6)
        page_mode = proc.poll() is None
        if not page_mode:
            # Singleton forward happened despite the pre-check (race). Never
            # pretend to attach; report from the registry instead.
            print("      profile became busy during launch -- registry mode (fleet undisturbed)")
            check_registry()

    try:
        if page_mode:
            from playwright.sync_api import sync_playwright

            time.sleep(4)
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
                context = b.contexts[0]
                page = next(
                    (pg for pg in context.pages if "chatgpt.com" in pg.url or "chat.openai.com" in pg.url),
                    context.pages[0],
                )
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                time.sleep(5)

                record("page", "chatgpt.com" in page.url, page.url[:70])

                popup = page.query_selector("#acb-popup")
                record("widget UI mounted", popup is not None,
                       "#acb-popup " + ("found" if popup else "MISSING (widget not installed or did not run)"))

                session = page.evaluate(
                    """() => {
                        const composer = document.querySelector('#prompt-textarea, form [contenteditable], form textarea');
                        const login = document.querySelector('a[href*=login], button[class*=login]');
                        return { composer: !!composer, login: !!login };
                    }"""
                )
                record("chatgpt session state", True,
                       "composer present (logged in)" if session["composer"]
                       else ("login control present (logged out)" if session["login"]
                             else "undetermined (no composer, no login control)"))

                label = page.evaluate(
                    """() => {
                        // SRC-115: this probe asked for '#acb-compact-cell', an id
                        // the userscript no longer emits, and then fell back to
                        // '[id^=acb-]', which lands on the #acb-popup container
                        // whose text is empty while collapsed. It reported the
                        // compact state as '' on a perfectly healthy widget.
                        // Read the elements that actually carry the text, and
                        // name which one answered so a future rename is visible.
                        const el = document.querySelector('#acb-super-state')
                                || document.querySelector('#acb-super-brand');
                        return el
                            ? ((el.id || '?') + '=' + (el.textContent || '').trim().slice(0, 110))
                            : '';
                    }"""
                )
                record("state label rendered", bool(label.split("=", 1)[-1].strip()), repr(label[:120]))

                log_before = page.evaluate(
                    """() => {
                        const el = document.querySelector('#acb-bridge-log');
                        return el ? (el.textContent || '').trim() : null;
                    }"""
                )
                record("live diagnostics log present", log_before is not None, (log_before or "MISSING")[:250])

                probe = page.evaluate(
                    """() => {
                        const b = document.querySelector('#acb-bridge-probe-get');
                        if (b) { b.click(); return true; }
                        return false;
                    }"""
                )
                if probe:
                    log_after = ""
                    for _ in range(20):
                        time.sleep(1.5)
                        log_after = page.evaluate(
                            """() => {
                                const el = document.querySelector('#acb-bridge-log');
                                return el ? (el.textContent || '').trim() : '';
                            }"""
                        )
                        if log_after and log_after != (log_before or ""):
                            break
                    record("self-diagnostic (Probe GET) recorded",
                           bool(log_after) and log_after != (log_before or ""), log_after[-350:])
                else:
                    record("self-diagnostic (Probe GET) recorded", False, "#acb-bridge-probe-get not in the live DOM")

                check_registry()
                b.close()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = [f for f in findings if not f[1]]
    print(f"\nLIVE WIDGET PROBE: {len(findings) - len(failed)}/{len(findings)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
