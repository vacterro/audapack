import subprocess
import time

from playwright.sync_api import sync_playwright

from audapack.components.widget import (
    dedicated_chromium_command,
    get_dedicated_chromium_profile_dir,
    select_dedicated_chromium,
)

cmd = dedicated_chromium_command(select_dedicated_chromium(), get_dedicated_chromium_profile_dir(), 'https://chatgpt.com/')
cmd.insert(1, '--remote-debugging-port=9222')
proc = subprocess.Popen(cmd)
try:
    time.sleep(6)
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = b.contexts[0]
        page = context.pages[0]
        page.wait_for_load_state('domcontentloaded')
        time.sleep(4)

        popup = page.query_selector('#acb-popup')
        zip_btn = page.query_selector('#acb-manual-zip-btn')
        print(f"popup: {popup is not None}, zip_btn: {zip_btn is not None}")

        # Check if Tampermonkey extension is loaded
        # Check all extension targets in CDP
        targets = b.contexts[0].service_workers if hasattr(b.contexts[0], 'service_workers') else []
        print(f"Service workers: {len(targets)}")
        for sw in targets:
            print("  SW URL:", sw.url)

        b.close()
finally:
    proc.terminate()
