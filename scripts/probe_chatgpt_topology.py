"""T-248 live ChatGPT composer topology probe.

Launches the dedicated AUDAPACK Chromium profile against the REAL current
chatgpt.com, opens the composer's own attachment surface exactly as the widget
would, and dumps STRUCTURE ONLY: how many file inputs exist, their ids/testids,
whether the composer owns them, and what the widget's own discovery verdict is.

It logs no filenames, prompt text, conversation text, account data or tokens.

Usage:
    python scripts/probe_chatgpt_topology.py
    python scripts/probe_chatgpt_topology.py --keep-open   # leave the window up
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time

from playwright.sync_api import sync_playwright

from audapack.components.widget import (
    dedicated_chromium_command,
    get_dedicated_chromium_profile_dir,
    select_dedicated_chromium,
)

DEBUG_PORT = 9223

# Structure-only DOM probe. Mirrors the widget's own discovery contract so the
# verdict here matches what chatGPTUploadSurface() decides in production.
PROBE_JS = r"""
() => {
  const composerMarker = '[data-type="unified-composer"], [data-testid*="composer" i], [id*="composer" i]';
  const attachSel = [
    '[data-testid*="attach" i]',
    'button[aria-label*="attach" i]',
    'button[aria-label*="add file" i]',
    'button[aria-label*="upload" i]',
    'button[title*="attach" i]'
  ].join(', ');

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }

  function composerRoot() {
    const canonical = document.querySelector('form[data-type="unified-composer"]');
    if (canonical && visible(canonical)) return canonical;
    const input = document.querySelector('#prompt-textarea[contenteditable="true"], [contenteditable="true"][role="textbox"][aria-label="Chat with ChatGPT"]');
    if (!input) return null;
    const form = input.closest('form');
    if (form && visible(form)) return form;
    return input.closest(composerMarker);
  }

  const root = composerRoot();
  const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
  const attachControls = root ? Array.from(root.querySelectorAll(attachSel)) : [];

  const describe = (input) => {
    let ownerTag = '';
    for (let n = input.parentElement, d = 0; n && d < 8; d++, n = n.parentElement) {
      if (n.matches && n.matches(composerMarker)) { ownerTag = n.tagName.toLowerCase() + (n.getAttribute('data-type') ? '[' + n.getAttribute('data-type') + ']' : ''); break; }
    }
    // Bounded declared-ownership check (aria-controls / aria-owns / popovertarget).
    const declared = new Set();
    for (const c of attachControls) {
      for (const a of ['aria-controls', 'aria-owns', 'popovertarget']) {
        const v = (c.getAttribute(a) || '').trim();
        if (v) declared.add(v);
      }
    }
    let declaredOwned = false;
    for (let n = input; n && n.getAttribute; n = n.parentElement) {
      const id = (n.getAttribute('id') || '').trim();
      if (id && declared.has(id)) { declaredOwned = true; break; }
    }
    return {
      id: input.id || '',
      testid: input.getAttribute('data-testid') || '',
      name: input.getAttribute('name') || '',
      accept: (input.getAttribute('accept') || '').slice(0, 60),
      multiple: input.hasAttribute('multiple'),
      disabled: !!input.disabled,
      inComposerRoot: !!(root && root.contains(input)),
      declaredOwned,
      ownerTag,
      classHint: (input.className || '').slice(0, 40)
    };
  };

  return {
    url_path: location.pathname,
    composer_found: !!root,
    composer_root_tag: root ? (root.tagName.toLowerCase() + (root.getAttribute('data-type') ? '[' + root.getAttribute('data-type') + ']' : '')) : '',
    attach_controls: attachControls.map(c => ({
      tag: c.tagName.toLowerCase(),
      testid: c.getAttribute('data-testid') || '',
      aria: (c.getAttribute('aria-label') || '').slice(0, 40),
      controls: c.getAttribute('aria-controls') || c.getAttribute('aria-owns') || c.getAttribute('popovertarget') || ''
    })),
    file_inputs_total: inputs.length,
    file_inputs: inputs.slice(0, 16).map(describe)
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-open", action="store_true", help="leave the window open after probing")
    ap.add_argument("--open-attach", action="store_true", help="click the composer attach control and re-probe")
    args = ap.parse_args()

    cmd = dedicated_chromium_command(
        select_dedicated_chromium(), get_dedicated_chromium_profile_dir(), "https://chatgpt.com/"
    )
    cmd.insert(1, f"--remote-debugging-port={DEBUG_PORT}")
    proc = subprocess.Popen(cmd)
    try:
        time.sleep(7)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = next((pg for pg in context.pages if "chatgpt.com" in pg.url), context.pages[0])
            page.wait_for_load_state("domcontentloaded")
            # Give the SPA composer time to hydrate.
            time.sleep(6)

            before = page.evaluate(PROBE_JS)
            print("=== BEFORE opening attach control ===")
            print(json.dumps(before, indent=2, ensure_ascii=False))

            if args.open_attach and before.get("attach_controls"):
                try:
                    # Click the first non-send attach control, then re-probe.
                    page.evaluate(
                        """() => {
                          const sel = '[data-testid*="attach" i], button[aria-label*="attach" i], button[aria-label*="add file" i], button[aria-label*="upload" i], button[title*="attach" i]';
                          const root = document.querySelector('form[data-type="unified-composer"]') || document;
                          for (const c of root.querySelectorAll(sel)) {
                            const s = ((c.getAttribute('aria-label')||'') + ' ' + (c.getAttribute('data-testid')||'')).toLowerCase();
                            if (/(send|submit|stop|voice|dictat|search)/.test(s)) continue;
                            c.click();
                            return;
                          }
                        }"""
                    )
                    time.sleep(2.5)
                    after = page.evaluate(PROBE_JS)
                    print("=== AFTER opening attach control ===")
                    print(json.dumps(after, indent=2, ensure_ascii=False))
                except Exception as exc:  # noqa: BLE001
                    print(f"attach-open probe failed: {exc}")

            widget_marker = page.evaluate(
                "() => ({ popup: !!document.querySelector('#acb-popup'), version: (document.querySelector('#acb-popup')?.getAttribute('data-acb-version')) || '' })"
            )
            print("=== widget marker ===")
            print(json.dumps(widget_marker, indent=2, ensure_ascii=False))

            if not args.keep_open:
                browser.close()
        return 0
    finally:
        if not args.keep_open:
            proc.terminate()
        else:
            print(f"(window left open; debug port {DEBUG_PORT})")


if __name__ == "__main__":
    raise SystemExit(main())
