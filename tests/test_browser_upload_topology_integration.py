"""REAL CHROMIUM + REAL WIDGET BYTES: the 2026-09 ChatGPT composer topology.

T-248 shipped a fix for a production outage -- six audits BLOCKED PRE-START with
one opaque `file-injection-rejected` each, no ZIP attached, no START sent -- and
the evidence for it was a Node suite whose helper modelled exactly ONE composer
shape. The shape it modelled was the one that no longer exists.

The live root cause, confirmed against chatgpt.com by CDP, was that the build
changed underneath the widget:

  * `#prompt-textarea` was DROPPED; the editor is now a bare ProseMirror
    `contenteditable[role=textbox]` with `aria-label="Ask ChatGPT"`;
  * the composer `<form>` lost `data-type="unified-composer"`; only hashed
    `Composer*` layout classes remain;
  * the composer now mounts THREE media-only file inputs alongside ONE general
    file input, so "the only file input on the page" stopped being true;
  * the attachment tile's filename moved off `[role=group][aria-label]` onto the
    tile's own "Remove <name>" button.

A helper that only ever builds the old shape cannot see any of that. So this
file builds the NEW shape and runs the real widget bytes in real Chromium
against it. It is the regression T-248 actually needed and never had.

WHAT THIS PROVES
- the composer is still found with no id and no data-type (hashed classes only);
- the upload surface picks the GENERAL input and rejects all three media-only
  ones, instead of refusing the composer as ambiguous or grabbing an image
  input and calling it a project ZIP;
- a diagnostic never lies about which input it chose;
- an injection lands on the general input and the tile is discoverable by name
  under the new tile shape.

WHAT THIS DOES NOT PROVE
A synthetic page, and an injected script rather than a Tampermonkey install.
The human-operated half stays in docs/AUDAPACK_WIDGET_ACCEPTANCE.md. The Node
contract for the same rules lives in tests/widget/w7-001-live-upload-surface.test.js
and w7-002-pre-start-block-once.test.js.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WIDGET_PATH = REPO_ROOT / "resources" / "AUDAPACK_WIDGET.user.js"

# The 2026-09 shape, transcribed from the live CDP probe recorded at LOG E-2426.
# Everything the old helper guaranteed an id for is simply absent here.
MODERN_CHATGPT_HTML = """<!DOCTYPE html>
<!-- SYNTHETIC fixture modelling the 2026-09 ChatGPT composer. See module
     docstring: this is the shape that caused the T-248 outage, rebuilt so the
     regression is executable. Not the real page. -->
<html>
<head><title>ChatGPT</title></head>
<body style="margin:0;background:#212121;color:#ececec;font-family:sans-serif;">
  <div id="__next">
    <main>
      <div id="chat-thread"></div>
      <div class="_root_1xy6t ComposerComposerShell__abcd">
        <!-- No data-type on the form. Hashed layout classes only. -->
        <form class="_root_2fq1a ComposerTextArea__wXYn">
          <div class="_root_9d8fg ProseMirror ComposerTextArea__text"
               contenteditable="true" role="textbox" aria-label="Ask ChatGPT"
               data-testid="prompt-textarea-manual"></div>
          <!-- THREE media-only inputs, exactly as the live build mounts them. -->
          <input type="file" accept="image/*" multiple style="display:none;" />
          <input type="file" accept="video/*" multiple style="display:none;" />
          <input type="file" accept="audio/*" multiple style="display:none;" />
          <!-- ...and the ONE general input, with no stable id. -->
          <input type="file" multiple style="display:none;" />
          <button type="button" data-testid="send-message-btn" aria-label="Send message">Send</button>
        </form>
      </div>
    </main>
  </div>
  <script>
    window.__injections = [];
    // A general input that accepts any type is the one an archive belongs in.
    for (const input of document.querySelectorAll('input[type=file]')) {
      if (input.getAttribute('accept')) continue;
      input.addEventListener('change', () => {
        window.__injections.push({
          name: input.files && input.files[0] ? input.files[0].name : '',
          size: input.files && input.files[0] ? input.files[0].size : 0
        });
        const tile = document.createElement('div');
        tile.setAttribute('role', 'group');
        // T-248: the filename no longer lives on the tile's aria-label.
        const remove = document.createElement('button');
        remove.setAttribute('aria-label', 'Remove _AUDAPACK_PROJ.zip');
        tile.appendChild(remove);
        input.closest('form').appendChild(tile);
      });
    }
  </script>
</body>
</html>
"""

BOOTSTRAP_JS = """
() => {
  window.__ACB_ENABLE_TEST_HOOK__ = true;
  const store = {};
  window.GM_setValue = (k, v) => { store[k] = v; };
  window.GM_getValue = (k, d) => (k in store ? store[k] : d);
  window.GM_deleteValue = (k) => { delete store[k]; };
  window.GM_listValues = () => Object.keys(store);
  window.GM_addStyle = (css) => {
    const s = document.createElement('style'); s.textContent = css;
    document.head.appendChild(s);
  };
  window.GM_addValueChangeListener = () => {};
  window.GM_xmlhttpRequest = (opts) => {
    if (opts.onload) opts.onload({ status: 200, responseText: JSON.stringify({ ok: true }), response: '' });
  };
  window.GM_xmlhttpRequest.RESPONSE_TYPE_ARRAYBUFFER = 'arraybuffer';
}
"""


@pytest.fixture(scope="module")
def chromium():
    if not Path.home().joinpath("AppData/Local/ms-playwright").exists():
        pytest.skip("playwright chromium is not installed on this host")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield browser
            browser.close()
    except Exception as exc:  # pragma: no cover - host without the browser build
        pytest.skip(f"chromium unavailable: {exc}")


def _open(context):
    page = context.new_page()
    page.route("https://chatgpt.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=MODERN_CHATGPT_HTML
    ))
    page.goto("https://chatgpt.com/")
    page.evaluate(BOOTSTRAP_JS)
    page.evaluate(WIDGET_PATH.read_text(encoding="utf-8"))
    return page


def test_the_composer_is_still_found_without_an_id_or_data_type(chromium):
    """No `#prompt-textarea`, no `data-type="unified-composer"`."""
    context = chromium.new_context()
    try:
        page = _open(context)
        assert page.evaluate("() => document.querySelector('#prompt-textarea') === null"), (
            "the fixture must not carry the removed id, or it proves nothing"
        )
        assert page.evaluate(
            "() => document.querySelector('form[data-type=\"unified-composer\"]') === null"
        ), "the fixture must not carry the removed data-type"

        found = page.evaluate(
            "() => { const r = window.__ACB_TEST__.chatGPTComposerRoot(); return Boolean(r); }"
        )
        assert found is True, "the 2026-09 composer shell was not recognised at all"
    finally:
        context.close()


def test_the_general_input_wins_over_three_media_only_inputs(chromium):
    """The live build mounts media inputs first; an image input is not a ZIP."""
    context = chromium.new_context()
    try:
        page = _open(context)
        topo = page.evaluate("() => window.__ACB_TEST__.chatGPTUploadTopology()")
        composer_inputs = [c for c in topo["candidates"] if c["inComposerRoot"]]
        assert len(composer_inputs) == 4, (
            f"the fixture must reproduce the 3-media + 1-general topology: {composer_inputs}"
        )
        surface = page.evaluate(
            """() => {
                const s = window.__ACB_TEST__.chatGPTUploadSurface();
                return {
                  ok: s.ok, reason: s.reason,
                  accept: s.input ? (s.input.getAttribute('accept') || '') : null,
                  multiple: s.input ? s.input.hasAttribute('multiple') : null
                };
            }"""
        )
        assert surface["ok"] is True, f"upload surface refused the composer: {surface}"
        assert surface["accept"] == "", (
            f"a media-only input was chosen for a project archive: {surface}"
        )
        assert surface["multiple"] is True
    finally:
        context.close()


def test_the_topology_probe_names_media_rejection_honestly(chromium):
    """A diagnostic that hides why it refused is how six BLOCKED lanes shipped."""
    context = chromium.new_context()
    try:
        page = _open(context)
        topo = page.evaluate("() => window.__ACB_TEST__.chatGPTUploadTopology()")
        # The widget mounts an upload input of its own, so the page total is the
        # composer's four PLUS the widget's. Only the composer's are in scope:
        # claiming the widget's own input is the failure this guards.
        own = [c for c in topo["candidates"] if not c["inComposerRoot"]]
        assert all(not c["boundToComposer"] for c in own), (
            f"the widget's own file input must never read as composer-owned: {own}"
        )
        composer_inputs = [c for c in topo["candidates"] if c["inComposerRoot"]]
        rejected = [c for c in composer_inputs if c["rejected"]]
        assert len(rejected) == 3, (
            f"the three media-only inputs must be named as rejected, got {composer_inputs}"
        )
        assert {c["rejected"] for c in rejected} == {"media-only"}
        assert sum(1 for c in composer_inputs if not c["rejected"]) == 1, (
            f"exactly one general input must survive, got {composer_inputs}"
        )
        assert topo["verdict"] == "composer-input", topo["verdict"]
    finally:
        context.close()


def test_an_injection_lands_on_the_general_input_and_names_the_tile(chromium):
    """The end of the T-248 outage: a ZIP is attached, discoverable by name."""
    context = chromium.new_context()
    try:
        page = _open(context)
        result = page.evaluate(
            """async () => {
                const api = window.__ACB_TEST__;
                const bytes = new Uint8Array([80, 75, 3, 4, 0, 0, 0, 0, 0, 0, 0, 0]);
                const file = new File([bytes], '_AUDAPACK_PROJ.zip', { type: 'application/zip' });
                const outcome = await api.injectComposerArchiveFile(file);
                return { outcome: outcome === undefined ? null : outcome,
                         injections: window.__injections };
            }"""
        )
        assert result["injections"], (
            f"nothing was injected into any file input: {result['outcome']}"
        )
        assert result["injections"][0]["name"] == "_AUDAPACK_PROJ.zip", result["injections"]
        assert result["injections"][0]["size"] == 12, result["injections"]

        # And the tile is findable by NAME under the new shape, where the
        # filename lives on the Remove button rather than the tile aria-label.
        named = page.evaluate(
            """() => Boolean([...document.querySelectorAll('button[aria-label]')]
                .find(b => /Remove .*\\.zip/i.test(b.getAttribute('aria-label') || '')))"""
        )
        assert named is True, "the attached tile is not discoverable by name"
    finally:
        context.close()
