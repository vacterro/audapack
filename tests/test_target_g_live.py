import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from audapack.components.widget import (
    dedicated_chromium_command,
    get_dedicated_chromium_profile_dir,
    select_dedicated_chromium,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

cmd = dedicated_chromium_command(
    select_dedicated_chromium(),
    get_dedicated_chromium_profile_dir(),
    "https://chatgpt.com/",
)
cmd.insert(1, "--remote-debugging-port=9222")
cmd.insert(2, "--window-size=1280,900")
proc = subprocess.Popen(cmd)
try:
    time.sleep(5)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = b.contexts[0]
        page = context.pages[0]
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        print("=== TARGET G: REAL ZIP ACCEPTANCE ===")
        # 1. First click: open menu
        page.click("#acb-manual-zip-btn")
        print("Waiting for project picker buttons to load...")
        page.wait_for_selector("#acb-manual-zip-menu-list button", timeout=15000)

        # Click on _AUDAPACK button in the picker
        btn = page.query_selector("#acb-manual-zip-menu-list button:has-text('_AUDAPACK')")
        if not btn:
            btn = page.query_selector("#acb-manual-zip-menu-list button")
        print("Clicking project button:", repr(btn.inner_text() if btn else None))
        btn.click()

        # Wait for attach to complete
        print("Waiting for attachment to attach...")
        for i in range(15):
            time.sleep(1)
            status_text = page.evaluate("""() => {
                const zip = document.querySelector('#acb-manual-zip-btn');
                const toast = document.querySelector('#acb-toast');
                return {
                    zipState: zip ? zip.dataset.state : null,
                    zipText: zip ? zip.innerText : null,
                    toastText: toast ? toast.innerText : null
                };
            }""")
            print(f"  [{i}s]:", status_text)
            state = status_text.get("zipState")
            text = status_text.get("zipText") or ""
            if state in ["ready", "bound", "attached", "error"] or "attached" in text.lower():
                break

        # Check tiles in DOM
        tiles_info = page.evaluate("""() => {
            const files = Array.from(document.querySelectorAll('*')).filter(el => {
                const t = el.innerText || '';
                return t.includes('.zip') && t.length < 80;
            }).map(el => el.innerText.trim());
            const zip = document.querySelector('#acb-manual-zip-btn');
            return {
                matchingZipText: Array.from(new Set(files)),
                zipButton: zip ? { text: zip.innerText, state: zip.dataset.state } : null
            };
        }""")
        print("Tiles / ZIP info after first attach:", tiles_info)

        # 2. Second unchanged click -> ALREADY_ATTACHED
        print("Triggering second unchanged click...")
        page.click("#acb-manual-zip-btn")
        time.sleep(5)
        second_result = page.evaluate("""() => {
            const zip = document.querySelector('#acb-manual-zip-btn');
            const toast = document.querySelector('#acb-toast');
            const status = document.querySelector('#acb-status');
            const files = Array.from(document.querySelectorAll('*')).filter(el => {
                const t = el.innerText || '';
                return t.includes('.zip') && t.length < 80;
            }).map(el => el.innerText.trim());
            return {
                zipText: zip ? zip.innerText : null,
                zipState: zip ? zip.dataset.state : null,
                toastText: toast ? toast.innerText : null,
                statusText: status ? status.innerText : null,
                tiles: Array.from(new Set(files))
            };
        }""")
        print("Second click result:", second_result)

        # 3. Rapid clicks test -> single-flight
        print("Triggering rapid clicks...")
        page.click("#acb-manual-zip-btn")
        page.click("#acb-manual-zip-btn")
        page.click("#acb-manual-zip-btn")
        time.sleep(2)
        rapid_result = page.evaluate("""() => {
            const zip = document.querySelector('#acb-manual-zip-btn');
            const toast = document.querySelector('#acb-toast');
            return {
                zipText: zip ? zip.innerText : null,
                zipState: zip ? zip.dataset.state : null,
                toastText: toast ? toast.innerText : null
            };
        }""")
        print("Rapid clicks result:", rapid_result)

        b.close()
finally:
    proc.terminate()
