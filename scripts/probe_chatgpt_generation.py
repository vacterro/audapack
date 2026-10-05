"""T-257 live ChatGPT generation-truth probe.

Launches the dedicated AUDAPACK Chromium profile against the REAL current
chatgpt.com and dumps the STRUCTURAL EVIDENCE the widget's own
``chatGPTGenerationSnapshot()`` returns, so an operator can see exactly which
DOM element is (or is not) claiming generation at a given moment.

The production symptom this exists for: an enabled A3 runtime sat at BUSY with a
visibly FINISHED response (final response actions mounted, no real generation
Stop on screen), because an over-broad stop-shaped control near the composer was
read as generation and short-circuited the audit evaluator before the completed
response could be committed.

It logs no conversation text, no prompts, no account data, no tokens. Every field
is shape, identity or structural relation only.

Usage:
    python scripts/probe_chatgpt_generation.py --once
    python scripts/probe_chatgpt_generation.py --watch 60 3
    python scripts/probe_chatgpt_generation.py --once --keep-open

--watch N M samples every M seconds for N seconds, so one run can capture BOTH
the actively-thinking phase and the finished phase of the same FastPrompter Core.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from audapack.components.widget import (
    dedicated_chromium_command,
    get_dedicated_chromium_profile_dir,
    select_dedicated_chromium,
)

# T-259: 9224 is a commonly used CDP port and is ALREADY TAKEN on this machine by
# an unrelated application whose own UI carries a "Stop the running turn" button.
# A probe that connects there reads a foreign page and reports its DOM as
# ChatGPT, which is how a capture that proves nothing gets mistaken for evidence.
# Use a port nothing else owns, and verify the origin before trusting a sample.
DEBUG_PORT = 9231

# Structure-only DOM probe. Mirrors resources/AUDAPACK_WIDGET.user.js exactly:
# exact generation identity AND proven composer ownership, plus the terminal
# evidence from the latest assistant's final response actions. If the two
# disagree, the record names the element so nobody has to guess.
PROBE_JS = r"""
() => {
  const STOP_CANONICAL = [
    '[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]'
  ].join(', ');
  const STOP_LIKE = [
    STOP_CANONICAL,
    'button[data-testid*="stop" i]',
    'button[aria-label*="stop" i]'
  ].join(', ');
  const NON_GENERATION = [
    '[data-testid*="voice" i]',
    '[data-testid*="audio" i]',
    '[data-testid*="dictation" i]',
    '[aria-label*="voice" i]',
    '[aria-label*="microphone" i]',
    '[aria-label*="audio" i]',
    '[aria-label*="dictation" i]'
  ].join(', ');
  const RESPONSE_ACTIONS = '[aria-label="Response actions"], [data-testid="response-actions"]';
  const TURN = '[data-testid^="conversation-turn-"], article[data-testid], article';

  function visible(el) {
    if (!el || !el.isConnected || el.hidden) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.display !== 'none' && s.visibility !== 'hidden';
  }

  function composerInput() {
    for (const c of document.querySelectorAll(
      '#prompt-textarea[contenteditable="true"][role="textbox"], ' +
      '#prompt-textarea.ProseMirror[contenteditable="true"], ' +
      '[contenteditable="true"][role="textbox"].ProseMirror, ' +
      '[contenteditable="true"][role="textbox"][aria-label="Chat with ChatGPT"], ' +
      '[contenteditable="true"][role="textbox"][aria-label^="Ask ChatGPT" i]'
    )) {
      if (visible(c) && !c.closest(TURN)) return c;
    }
    return null;
  }

  function composerRoot() {
    const canonical = document.querySelector('form[data-type="unified-composer"]');
    if (canonical && visible(canonical)) return canonical;
    const input = composerInput();
    if (!input) return null;
    const form = input.closest('form');
    if (form && visible(form)) return form;
    const shell = input.closest('[data-type="unified-composer"], [data-testid*="composer" i], [class*="Composer" i]');
    return shell && visible(shell) ? shell : null;
  }

  function latestAssistant() {
    let found = null;
    for (const turn of document.querySelectorAll('[data-message-author-role="assistant"]')) {
      if (!turn.closest(TURN) && turn.tagName.toLowerCase() !== 'article') continue;
      if (!found || (turn.compareDocumentPosition(found) & Node.DOCUMENT_POSITION_PRECEDING)) found = turn;
    }
    return found;
  }

  function hasFinalActions(turn) {
    if (!turn) return false;
    return !!turn.querySelector(
      'button[data-testid="copy-turn-action-button"][aria-label*="Copy response" i]'
    ) || !!turn.querySelector(RESPONSE_ACTIONS + ' button[data-testid="copy-turn-action-button"]');
  }

  function excluded(el) {
    return !!el.closest(TURN) || !!el.closest(RESPONSE_ACTIONS) || !!el.closest(NON_GENERATION);
  }

  function distanceTo(input, el) {
    if (!input || !el) return -1;
    if (input === el || input.contains(el)) return 0;
    let d = 0;
    for (let n = input.parentElement; n; n = n.parentElement) {
      d += 1;
      if (n === el || n.contains(el)) return d;
      if (n === document.body || n === document.documentElement) break;
    }
    return -1;
  }

  function owned(el, root) {
    if (!el || !visible(el) || excluded(el) || !root) return false;
    if (root.contains(el)) return true;
    const input = composerInput();
    const form = input ? input.closest('form') : (root.tagName === 'FORM' ? root : null);
    const shell = form ? form.parentElement : null;
    if (!shell || !shell.contains(el)) return false;
    const send = shell.querySelector(
      '#composer-submit-button, [data-testid="send-button"], [data-testid="composer-submit-button"]'
    );
    return !!(send && visible(send));
  }

  function describe(el) {
    if (el.matches('[data-testid="stop-button"]')) return 'canonical-stop-button';
    if (el.matches('button[aria-label="Stop generating"]')) return 'canonical-stop-generating';
    if (el.matches('button[aria-label="Stop streaming"]')) return 'canonical-stop-streaming';
    return 'stop-like';
  }

  const root = composerRoot();
  const input = composerInput();
  const form = input ? input.closest('form') : (root && root.tagName === 'FORM' ? root : null);
  const send = document.querySelector(
    '#composer-submit-button, [data-testid="send-button"], [data-testid="composer-submit-button"]'
  );
  const assistant = latestAssistant();
  const finalActions = hasFinalActions(assistant);

  const all = [];
  for (const el of document.querySelectorAll(STOP_LIKE)) {
    if (!visible(el)) continue;
    all.push({
      selector_class: describe(el),
      tag: el.tagName.toLowerCase(),
      data_testid: el.getAttribute('data-testid') || '',
      aria_label: (el.getAttribute('aria-label') || '').slice(0, 60),
      inside_composer_root: !!(root && root.contains(el)),
      same_form: !!(form && form.contains(el)),
      composer_distance: distanceTo(input, el),
      inside_conversation_turn: !!el.closest(TURN),
      inside_response_actions: !!el.closest(RESPONSE_ACTIONS),
      non_generation_excluded: excluded(el),
      owned: owned(el, root),
      // The widget's own verdict, recomputed from these two facts.
      reads_as_generation: !!(el.matches(STOP_CANONICAL) && owned(el, root))
    });
    if (all.length >= 24) break;
  }

  const canonical = all.find(c => c.reads_as_generation) || null;
  const stale = all.find(c => !c.reads_as_generation) || null;

  return {
    url_path: location.pathname,
    composer_found: !!root,
    send_control_present: !!(send && visible(send)),
    stop_candidates_visible: all.length,
    canonical_stop: canonical,
    rejected_stop_candidate: stale,
    latest_assistant_has_final_actions: finalActions,
    // The widget's live answer for this instant.
    generating: !!canonical,
    state: canonical ? (finalActions ? 'stabilizing_or_conflict' : 'generating') : (finalActions ? 'terminal' : 'idle'),
    diagnostic: (!canonical && stale && finalActions) ? 'stale_stop_candidate_ignored' : '',
    all_stop_candidates: all
  };
}
"""


def _print(label: str, record: dict) -> None:
    print(f"=== {label} ===")
    print(json.dumps(record, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sample (default)")
    ap.add_argument("--watch", nargs=2, type=int, metavar=("SECONDS", "INTERVAL"),
                    help="sample for SECONDS every INTERVAL seconds")
    ap.add_argument("--keep-open", action="store_true", help="leave the window open after probing")
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
            # A capture from a page that is not ChatGPT is not evidence about
            # ChatGPT. Refuse it rather than printing a confident wrong answer.
            if "chatgpt.com" not in page.url:
                print(f"REFUSED: connected page is {page.url!r}, not chatgpt.com", file=sys.stderr)
                return 3
            page.wait_for_load_state("domcontentloaded")
            time.sleep(6)

            if args.watch:
                total, interval = args.watch
                deadline = time.time() + max(0, total)
                index = 0
                while time.time() < deadline:
                    index += 1
                    _print(f"SAMPLE {index} @ {time.strftime('%H:%M:%S')}", page.evaluate(PROBE_JS))
                    time.sleep(max(1, interval))
            else:
                _print("SAMPLE 1", page.evaluate(PROBE_JS))

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
