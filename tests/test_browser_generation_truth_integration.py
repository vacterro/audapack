"""REAL CHROMIUM + REAL WIDGET BYTES: T-249 live-generation state truth.

WHAT THIS PROVES
T-249's contract is a claim about what the compact widget label SAYS while
ChatGPT is visibly streaming, and about what survives a reload in the middle
of that stream. Both are browser-runtime facts -- MutationObservers, the
composer root search, route hydration and a real page reload -- so synthetic
DOM objects in a Node vm cannot settle them.

This runs headless Chromium (Playwright) against a synthetic ChatGPT page and
the REAL `resources/AUDAPACK_WIDGET.user.js` bytes, and proves:

- an enabled runtime that is stored `idle` on a conversation which is
  visibly generating never renders READY -- it renders the live wave
  (CORE / W2 / PERF / W<n>) for a recognizable audit lineage, and BUSY for
  ordinary non-audit generation;
- the same page, reloaded in the middle of that stream, reconstructs the
  runtime on its own -- no manual Resume, no new turn node, and no second
  Send.

WHAT THIS DOES NOT PROVE
The ChatGPT page is the synthetic fixture below, not the real one, and the
script is injected directly rather than installed by Tampermonkey. So this is
the machine-verifiable half of the acceptance, not production acceptance for
the real ChatGPT DOM. `docs/AUDAPACK_WIDGET_ACCEPTANCE.md` keeps the
human-operated half. Do not describe this file as replacing it.

The Node-level unit contract for the same rules lives in
tests/widget/w7-003-live-generation-state.test.js and
tests/widget/w7-004-hydration-miss-recovery.test.js.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WIDGET_PATH = REPO_ROOT / "resources" / "AUDAPACK_WIDGET.user.js"

CORE_TURN = (
    "AUDIT CORE — wave 1/3 of Quick 3 Waves.\n"
    "CAMPAIGN_RUN_ID: run-live\n"
    "ACB_CHAIN_RECEIPT: startcore-live-1"
)
SECOND_TURN = (
    "AUDIT SECOND WAVE\nACB_CHAIN_RECEIPT: startcore-live-2"
)

CHATGPT_HTML = """<!DOCTYPE html>
<!-- SYNTHETIC ChatGPT DOM FIXTURE -- see the module docstring. Not the real
     page; it exists so the widget's real browser runtime (observers, composer
     root search, hydration, reload) can be exercised without a ChatGPT
     account. -->
<html>
<head><title>ChatGPT</title></head>
<body style="margin:0;background:#212121;color:#ececec;font-family:sans-serif;">
  <div id="__next">
    <main id="main">
      <div id="chat-thread"></div>
      <div id="composer-shell">
        <form data-type="unified-composer">
          <div id="prompt-textarea" contenteditable="true" role="textbox"
               aria-label="Chat with ChatGPT"></div>
          <input type="file" id="upload-files" multiple style="display:none;" />
          <button type="button" data-testid="send-button">Send</button>
        </form>
      </div>
    </main>
  </div>
  <script>
    window.__sendClickCount = 0;
    window.__HTTP_LOG__ = [];
    document.addEventListener('click', function (ev) {
      const b = ev.target && ev.target.closest && ev.target.closest('[data-testid="send-button"]');
      if (b) window.__sendClickCount++;
    }, true);
  </script>
</body>
</html>
"""

# GM storage survives a reload inside one tab via sessionStorage, which is also
# how the widget keeps a managed worker's id across a reload (T-64).
BOOTSTRAP_JS = """
() => {
  window.__ACB_ENABLE_TEST_HOOK__ = true;
  const KEY = '__acb_test_gm__';
  const store = JSON.parse(sessionStorage.getItem(KEY) || '{}');
  const persist = () => sessionStorage.setItem(KEY, JSON.stringify(store));
  window.GM_setValue = (k, v) => { store[k] = v; persist(); };
  window.GM_getValue = (k, d) => (k in store ? store[k] : d);
  window.GM_deleteValue = (k) => { delete store[k]; persist(); };
  window.GM_listValues = () => Object.keys(store);
  window.GM_addStyle = (css) => {
    const s = document.createElement('style');
    s.textContent = css;
    document.head.appendChild(s);
  };
  window.GM_addValueChangeListener = () => {};
  // The Bridge is not under test here; record every call and answer benignly so
  // a duplicate Send is visible as a duplicate request rather than a hang.
  window.GM_xmlhttpRequest = (opts) => {
    window.__HTTP_LOG__.push({ method: opts.method || 'GET', url: opts.url, data: opts.data || null });
    if (opts.onload) {
      opts.onload({ status: 200, responseText: JSON.stringify({ ok: true }), response: '' });
    }
  };
  window.GM_xmlhttpRequest.RESPONSE_TYPE_ARRAYBUFFER = 'arraybuffer';
}
"""


def _turn_html(turn_id: str, role: str, text: str) -> str:
    return (
        f'<article data-message-author-role="{role}" '
        f'data-testid="conversation-turn-{turn_id}" data-message-id="{turn_id}">'
        f'<div class="markdown">{text}</div></article>'
    )


def _page_html(turns: list[tuple[str, str, str]], generating: bool) -> str:
    body = "".join(_turn_html(tid, role, text) for tid, role, text in turns)
    stop = (
        '<button data-testid="stop-button" aria-label="Stop generating">Stop</button>'
        if generating
        else '<button data-testid="stop-button" aria-label="Stop generating" hidden>Stop</button>'
    )
    return CHATGPT_HTML.replace(
        '<div id="chat-thread"></div>',
        f'<div id="chat-thread">{body}</div>',
    ).replace(
        '<button type="button" data-testid="send-button">Send</button>',
        '<button type="button" data-testid="send-button">Send</button>' + stop,
    )


@pytest.fixture(scope="module")
def chromium():
    if shutil.which("") is None and not Path.home().joinpath("AppData/Local/ms-playwright").exists():
        pytest.skip("playwright chromium is not installed on this host")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield browser
            browser.close()
    except Exception as exc:  # pragma: no cover - host without the browser build
        pytest.skip(f"chromium unavailable: {exc}")


def _open(context, path: str, turns, generating: bool):
    page = context.new_page()
    page.route("https://chatgpt.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=_page_html(turns, generating),
    ))
    page.goto(f"https://chatgpt.com{path}")
    return page


def _inject_widget(page):
    page.evaluate(BOOTSTRAP_JS)
    page.evaluate(WIDGET_PATH.read_text(encoding="utf-8"))


def _arm_enabled_idle(page, conversation_key: str = "c:live1234"):
    """An enabled runtime parked on `idle` -- the state T-249 was found in."""
    page.evaluate(
        """(key) => {
            const api = window.__ACB_TEST__;
            api.state.auditProfile = 'quick3';
            api.autoRuntime = {
              version: 4, enabled: true, stage: 'idle',
              conversationKey: key, anchorUserId: '', seenUserId: '',
              coreUserId: '', secondUserId: '', performanceUserId: '',
              expectedKind: '', pendingSendReceipt: '', pendingSendKind: '',
              pendingSendPreviousUserId: '', pendingSendStartedAt: 0,
              lastActivityAt: Date.now(), a3OperatorExplicitOff: false
            };
        }""",
        conversation_key,
    )


def test_generating_audit_conversation_never_reads_ready(chromium):
    """The defect: an enabled idle runtime rendered READY while ChatGPT streamed."""
    context = chromium.new_context()
    try:
        page = _open(context, "/c/live1234", [("t1", "user", CORE_TURN)], generating=True)
        _inject_widget(page)
        _arm_enabled_idle(page)

        assert page.evaluate("() => window.__ACB_TEST__.chatGPTIsGenerating()") is True, (
            "the visible Stop control must read as live generation"
        )

        label = page.evaluate("() => window.__ACB_TEST__.superCompactAutoLabel()")
        assert label != "READY", f"a streaming audit must never render READY, got {label!r}"
        assert label == "CORE", f"the live wave must be shown, got {label!r}"
    finally:
        context.close()


def test_ordinary_generation_reads_busy_not_ready(chromium):
    """Same gate, non-audit generation: neutral activity, never a wave, never READY."""
    context = chromium.new_context()
    try:
        turns = [("t1", "user", "just a normal question about python")]
        page = _open(context, "/c/live1234", turns, generating=True)
        _inject_widget(page)
        _arm_enabled_idle(page)

        assert page.evaluate("() => window.__ACB_TEST__.chatGPTIsGenerating()") is True
        label = page.evaluate("() => window.__ACB_TEST__.superCompactAutoLabel()")
        assert label not in ("READY", "CORE", "W2", "PERF"), (
            f"ordinary generation must not borrow an audit wave token, got {label!r}"
        )
    finally:
        context.close()


def test_reload_while_streaming_never_shows_ready_and_sends_nothing(chromium):
    """A reload mid-stream must not drop to READY, and must not re-Send.

    The stream is still running after the reload, so the label must still be
    the live wave; and a reconstruction is a read, never a fresh irreversible
    send, so the send counter and the HTTP log must both stay empty.
    """
    context = chromium.new_context()
    try:
        page = _open(context, "/c/live1234", [("t1", "user", CORE_TURN)], generating=True)
        _inject_widget(page)
        _arm_enabled_idle(page)
        before = page.evaluate("() => window.__ACB_TEST__.superCompactAutoLabel()")
        assert before == "CORE"

        # Same tab, same conversation, stream still running: this is the reload
        # the operator does when a long audit looks stalled.
        page.reload()
        _inject_widget(page)
        _arm_enabled_idle(page)
        page.wait_for_timeout(400)

        assert page.evaluate("() => window.__ACB_TEST__.chatGPTIsGenerating()") is True, (
            "the stream is still live after the reload"
        )
        label = page.evaluate("() => window.__ACB_TEST__.superCompactAutoLabel()")
        assert label != "READY", f"post-reload label must not read READY, got {label!r}"
        assert label == "CORE", f"post-reload must show the live wave, got {label!r}"

        sends = page.evaluate("() => window.__sendClickCount")
        assert sends == 0, f"a reload must never re-send, saw {sends} send click(s)"

        posts = page.evaluate(
            "() => window.__HTTP_LOG__.filter(e => (e.method || 'GET').toUpperCase() === 'POST')"
        )
        assert posts == [], f"a reload must not issue bridge writes, saw {json.dumps(posts)}"
    finally:
        context.close()
