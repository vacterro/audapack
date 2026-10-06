"""REAL BRIDGE + REAL CHROMIUM + SYNTHETIC CHATGPT DOM: widget integration.

WHAT THIS PROVES
Executes headless Chromium via Playwright against a real in-process
ThreadingHTTPServer serving `AudapackBridgeHandler`, and verifies the widget's
CURRENT manual-ZIP transaction (Widget 0.0.61) end to end:

- A normal manual ZIP is an ensure -> attach -> automatic Send -> positively
  verified Send transaction. The synthetic ChatGPT fixture models the
  observable accepted-submission transition the widget's verification relies
  on: the submitted composer payload (authored text plus attachment tiles)
  leaves the composer and exactly one user turn carrying that payload appears.
- CHAT A first ZIP: Auto Audit stays disabled, the project is selected, the
  canonical archive is ensured/attached, exactly one Send occurs and is
  positively verified, result SENT, exactly one user turn, no audit
  campaign/A3/A10/START side effects.
- Identical repeat after the verified Send: ALREADY_SENT with zero archive GET,
  zero File construction, zero injection, zero Send and no duplicate turn.
- ALREADY_ATTACHED transport state with a new outgoing payload: zero GET,
  zero injection, one Send, verified SENT.
- Send unavailable: bounded SEND_TIMEOUT with the ready archive and payload
  preserved; after Send is restored the retry reuses the same ready archive
  (zero GET, zero injection) and reaches a verified SENT.
- Rapid clicks: one ensure chain, at most one GET, one injection, one accepted
  Send; the coalesced callers receive the same transaction result semantics.
- CHAT B isolation, the navigation race and multi-tab storage isolation.

WHAT THIS DOES NOT PROVE (T-196/T-198)
The ChatGPT page here is the `CHATGPT_HTML` fixture below, and the widget
script is injected straight from `resources/AUDAPACK_WIDGET.user.js`. That
bypasses, by construction:

  * the real ChatGPT DOM;
  * Tampermonkey installation;
  * Tampermonkey's own @version comparison;
  * @updateURL / @downloadURL;
  * installed-script replacement.

So this is BRIDGE+WIDGET INTEGRATION coverage against a SYNTHETIC CHATGPT DOM,
never production acceptance for the real ChatGPT page. Real production
acceptance -- a real installed userscript updating through the Bridge and
running on the real ChatGPT page -- is recorded in
`docs/AUDAPACK_WIDGET_ACCEPTANCE.md`, and the machine-verifiable half of it is
`tests/test_bridge_widget_delivery.py` (the served endpoint's bytes) plus
`tests/test_widget_release_identity.py` (the version/SHA release invariant).
Do not describe this file as proving the real ChatGPT production DOM.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from audapack.bridge.server import AudapackBridgeHandler
from audapack.config import AppConfig, save_config
from audapack.models import Project


def sync_py_http(opts):
    method = opts.get("method", "GET")
    url = opts.get("url")
    headers = opts.get("headers", {})
    data = opts.get("data")
    body = data.encode("utf-8") if isinstance(data, str) else data

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            content = resp.read()
            return {
                "status": status,
                "responseText": content.decode("utf-8", errors="replace"),
                "responseBase64": base64.b64encode(content).decode("ascii"),
            }
    except urllib.error.HTTPError as err:
        content = err.read()
        return {
            "status": err.code,
            "responseText": content.decode("utf-8", errors="replace"),
            "responseBase64": base64.b64encode(content).decode("ascii"),
        }
    except Exception as err:
        return {"status": 0, "responseText": str(err), "responseBase64": ""}


class BridgeFixture:
    def __init__(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.audit_root = self.temp_dir / "AUDITING"
        self.audit_root.mkdir(parents=True)
        self.archive_dir = self.temp_dir / "archives"
        self.archive_dir.mkdir(parents=True)

        self.source_audapack = self.temp_dir / "AUDAPACK"
        self.source_audapack.mkdir(parents=True)
        (self.source_audapack / "app.py").write_text("print('hello audapack')\n", encoding="utf-8")
        (self.source_audapack / "README.md").write_text("# AUDAPACK\n", encoding="utf-8")

        self.source_fastprompter = self.temp_dir / "FastPrompter"
        self.source_fastprompter.mkdir(parents=True)
        (self.source_fastprompter / "main.py").write_text("print('fast prompter')\n", encoding="utf-8")

        self.port = 18955
        self.token = "live_acceptance_token_987654321"

        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.config.bridge.host = "127.0.0.1"
        self.config.bridge.port = self.port
        self.config.bridge.token = self.token
        self.config.packing.output_dir = str(self.archive_dir)
        self.config.packing.include_timestamp = True
        self.config.projects = [
            Project(
                id="audapack",
                display_name="_AUDAPACK",
                source_path=str(self.source_audapack),
                priority_group="MAIN0",
                slot=1,
            ),
            Project(
                id="fastprompter",
                display_name="_FASTPROMPTER",
                source_path=str(self.source_fastprompter),
                priority_group="MAIN0",
                slot=2,
            ),
        ]

        class LiveTestHandler(AudapackBridgeHandler):
            pass

        LiveTestHandler.config = self.config
        LiveTestHandler.test_base_dir = str(self.temp_dir)
        save_config(self.config, base_dir=str(self.temp_dir))

        self.server = ThreadingHTTPServer((self.config.bridge.host, self.port), LiveTestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def teardown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)


CHATGPT_HTML = """<!DOCTYPE html>
<!-- SYNTHETIC ChatGPT DOM FIXTURE -- not the real page. See the module
     docstring: this is deliberate integration coverage, never presented as
     production acceptance for the real ChatGPT DOM (T-196/T-198).

     The fixture models the observable outcome of an ACCEPTED ChatGPT
     submission: the authored text and the attachment tiles leave the composer
     and one user turn carrying that payload appears in the thread. A click
     alone is never acceptance -- the widget's own positive verification must
     observe this transition. -->
<html>
<head><title>ChatGPT</title></head>
<body style="margin:0; padding:0; width:100vw; height:100vh; background:#212121; color:#ececec; font-family:sans-serif;">
  <div id="__next" style="display:flex; height:100vh; width:100vw;">
    <nav id="sidebar" style="width:260px; height:100%; background:#171717; flex-shrink:0;">
      <div style="padding:16px;">Sidebar Content</div>
    </nav>
    <main style="flex:1; display:flex; flex-direction:column; height:100%; position:relative;">
      <div id="chat-thread" style="flex:1; overflow-y:auto; padding:24px;">
        <div>Chat History</div>
      </div>
      <div id="composer-shell" style="padding:16px 24px 24px 24px; position:relative;">
        <form data-type="unified-composer" style="background:#2f2f2f; border-radius:16px; padding:12px; display:flex; flex-direction:column;">
          <div id="attachment-tiles-container" style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px;"></div>
          <div style="display:flex; align-items:flex-end;">
            <div id="prompt-textarea" contenteditable="true" role="textbox" aria-label="Chat with ChatGPT" style="flex:1; background:transparent; color:#ececec; outline:none; min-height:44px; font-size:16px; padding:8px;">Initial user prompt for Chat A</div>
            <input type="file" id="upload-files" multiple style="display:none;" />
            <button type="button" id="composer-submit-button" data-testid="send-button" style="background:#fff; color:#000; border:none; border-radius:50%; width:36px; height:36px; cursor:pointer; font-weight:bold;">&uarr;</button>
          </div>
        </form>
      </div>
    </main>
  </div>
  <script>
    window.__uploadInput = document.querySelector('#upload-files');
    window.__attachmentContainer = document.querySelector('#attachment-tiles-container');
    window.__sendButton = document.querySelector('#composer-submit-button');
    window.__promptSurface = document.querySelector('#prompt-textarea');
    window.__chatThread = document.querySelector('#chat-thread');
    window.__sendClickCount = 0;
    window.__acceptedSendCount = 0;
    window.__attachmentInjections = 0;
    window.__submittedPayloads = [];
    window.__sendOutcome = 'accept';

    window.__collectComposerTiles = () => Array.from(window.__attachmentContainer.children)
      .filter(element => element.getAttribute && element.getAttribute('role') === 'group')
      .map(element => element.getAttribute('aria-label'));

    window.__appendUserTurn = payload => {
      const turn = document.createElement('article');
      turn.setAttribute('data-testid', `conversation-turn-${window.__submittedPayloads.length}`);
      const message = document.createElement('div');
      message.setAttribute('data-message-author-role', 'user');
      message.textContent = payload.text || payload.tiles.join(', ');
      turn.appendChild(message);
      window.__chatThread.appendChild(turn);
    };

    window.__sendButton.addEventListener('click', () => {
      window.__sendClickCount += 1;
      if (window.__sendOutcome !== 'accept') return;
      const text = String(window.__promptSurface.textContent || '');
      const tiles = window.__collectComposerTiles();
      if (!text.trim() && !tiles.length) return;
      // A real ACCEPTED ChatGPT submission consumes the composer: the authored
      // text and the attachment tiles leave it, and one user turn carrying the
      // submitted payload appears. This is the evidence the widget verifies.
      window.__submittedPayloads.push({ text, tiles });
      window.__acceptedSendCount += 1;
      window.__promptSurface.textContent = '';
      for (const tile of Array.from(window.__attachmentContainer.children)) tile.remove();
      window.__appendUserTurn({ text, tiles });
    });

    window.__uploadInput.addEventListener('change', () => {
      const files = window.__uploadInput.files;
      if (!files) return;
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        window.__attachmentInjections += 1;
        const tile = document.createElement('div');
        tile.setAttribute('role', 'group');
        tile.setAttribute('aria-label', file.name);
        tile.setAttribute('data-testid', 'attachment-tile');
        const label = document.createElement('span');
        label.textContent = file.name;
        tile.appendChild(label);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.setAttribute('aria-label', 'Remove file');
        removeBtn.textContent = 'x';
        removeBtn.style.cssText = 'background:none; border:none; color:#aaa; cursor:pointer; margin-left:4px;';
        removeBtn.addEventListener('click', () => {
          tile.remove();
        });
        tile.appendChild(removeBtn);

        window.__attachmentContainer.appendChild(tile);
      }
    });
  </script>
</body>
</html>
"""


async def setup_widget_page(page, bridge: BridgeFixture, initial_path: str = "/c/chat-a-1234"):
    await page.expose_function("__pw_http_request", lambda opts: sync_py_http(opts))

    await page.route("https://chatgpt.com/**", lambda route: route.fulfill(
        status=200,
        content_type="text/html",
        body=CHATGPT_HTML
    ))

    await page.goto(f"https://chatgpt.com{initial_path}")

    with open("resources/AUDAPACK_WIDGET.user.js", "r", encoding="utf-8") as f:
        script_text = f.read()

    # Expose bridge port and token to JS
    await page.evaluate(f"""() => {{
        window.__ACB_ENABLE_TEST_HOOK__ = true;
        window.__TEST_BRIDGE_PORT__ = {bridge.port};
        window.__TEST_BRIDGE_TOKEN__ = '{bridge.token}';
        window.__HTTP_LOG__ = [];

        const store = window.__SHARED_STORE__ || {{}};
        store['ai_chatbuttons_bridge_token_v1'] = window.__TEST_BRIDGE_TOKEN__;
        window.__SHARED_STORE__ = store;

        window.GM_setValue = (k, v) => {{ store[k] = v; }};
        window.GM_getValue = (k, defVal) => {{ return (k in store) ? store[k] : defVal; }};
        window.GM_deleteValue = (k) => {{ delete store[k]; }};
        window.GM_listValues = () => Object.keys(store);
        window.GM_addStyle = (css) => {{
            const s = document.createElement('style');
            s.textContent = css;
            document.head.appendChild(s);
        }};
        window.GM_addValueChangeListener = () => {{}};

        // Real GM_xmlhttpRequest backed by backend Python HTTP proxy (Tampermonkey background extension model)
        window.GM_xmlhttpRequest = (opts) => {{
            const method = opts.method || 'GET';
            const url = opts.url;
            window.__HTTP_LOG__.push({{ method, url, time: Date.now() }});
            window.__pw_http_request({{
                method: method,
                url: url,
                headers: opts.headers || {{}},
                data: opts.data,
                responseType: opts.responseType
            }}).then(res => {{
                let response = res.responseText;
                if (opts.responseType === 'arraybuffer') {{
                    const bin = atob(res.responseBase64 || '');
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    response = bytes;
                }}
                if (opts.onload) {{
                    opts.onload({{
                        status: res.status,
                        responseText: res.responseText,
                        response: response
                    }});
                }}
            }}).catch(err => {{
                if (opts.onerror) opts.onerror(err);
            }});
        }};
    }}""")

    # Run widget script
    await page.evaluate(script_text)

    # Point widget to live bridge port
    await page.evaluate(f"""() => {{
        const api = window.__ACB_TEST__;
        api.state.bridgeUrl = 'http://127.0.0.1:{bridge.port}';
        api.bindAutoRuntimeToCurrentConversation();
    }}""")


async def manual_zip(page, **options):
    """Run one normal ZIP action and wait for its transaction result."""
    return await page.evaluate(
        "(options) => window.__ACB_TEST__.manualArchiveZipAction(options)",
        options,
    )


async def composer_state(page):
    """The synthetic DOM's observable state plus the Bridge request counters."""
    return await page.evaluate("""() => ({
        text: String(window.__promptSurface.textContent || ''),
        tiles: window.__collectComposerTiles(),
        sendClicks: window.__sendClickCount,
        acceptedSends: window.__acceptedSendCount,
        injections: window.__attachmentInjections,
        userTurns: window.__submittedPayloads.map(entry => ({ text: entry.text, tiles: entry.tiles.slice() })),
        ensurePosts: window.__HTTP_LOG__.filter(r => r.method === 'POST' && r.url.endsWith('/archive/ensure')).length,
        archiveGets: window.__HTTP_LOG__.filter(r => r.method === 'GET' && r.url.endsWith('/archive')).length
    })""")


async def audit_side_effects(page):
    return await page.evaluate("""() => ({
        autoEnabled: window.__ACB_TEST__.autoRuntime?.enabled === true,
        startHandoff: window.__ACB_TEST__.readStartAuditHandoff(),
        a3Intent: window.__ACB_TEST__.readA3Intent(),
        workerLease: window.__ACB_TEST__.browserWorkerLease
    })""")


@pytest.mark.asyncio
async def test_bridge_widget_integration_across_manual_zip_scenarios():
    bridge = BridgeFixture()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})

            await setup_widget_page(page, bridge, initial_path="/c/chat-a-1234")
            await page.wait_for_selector("#acb-manual-zip-btn", timeout=10000)

            # ================================================================
            # TARGET B: CHAT A first normal manual ZIP -> attach, auto-Send,
            # positively verified SENT, one user turn, no audit side effects
            # ================================================================
            auto_enabled = await page.evaluate("() => window.__ACB_TEST__.autoRuntime?.enabled === true")
            assert not auto_enabled, "Auto Audit must start disabled"

            # Button is in unbound state: "ZIP \u25be"
            btn_text = await page.evaluate("() => document.querySelector('#acb-manual-zip-btn').textContent.trim()")
            assert "ZIP" in btn_text, f"Expected ZIP in button text, got {btn_text}"

            # Open picker by clicking ZIP button
            await page.click("#acb-manual-zip-btn")
            await page.wait_for_timeout(500)

            menu_visible = await page.evaluate("() => !document.querySelector('#acb-manual-zip-menu').hidden")
            assert menu_visible, "Picker menu must be open"

            items = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('#acb-manual-zip-menu-list button[data-project-id]'))
                    .map(b => ({ id: b.dataset.projectId, title: b.getAttribute('title') }));
            }""")
            item_ids = [it["id"] for it in items]
            assert "audapack" in item_ids, f"audapack missing in picker items: {item_ids}"
            assert "fastprompter" in item_ids, f"fastprompter missing in picker items: {item_ids}"

            # Selecting the project runs the whole transaction, including Send.
            await page.click('#acb-manual-zip-menu-list button[data-project-id="audapack"]')
            await page.wait_for_function(
                """() => window.__ACB_TEST__.manualArchiveLogSnapshot()
                    .some(record => record.resultCode === 'SENT')""",
                timeout=20000,
            )

            first = await composer_state(page)
            assert first["acceptedSends"] == 1, f"Expected exactly one accepted Send, got {first['acceptedSends']}"
            assert first["sendClicks"] == 1, f"Expected exactly one Send click, got {first['sendClicks']}"
            assert first["injections"] == 1, f"Expected exactly one injection, got {first['injections']}"
            assert first["ensurePosts"] == 1, f"Expected exactly one ensure, got {first['ensurePosts']}"
            assert first["archiveGets"] == 1, f"Expected exactly one archive GET, got {first['archiveGets']}"
            assert first["text"] == "", "An accepted Send consumes the composer text"
            assert first["tiles"] == [], "An accepted Send detaches the submitted tiles"
            assert len(first["userTurns"]) == 1, f"Expected exactly one user turn, got {first['userTurns']}"
            first_tile_name = first["userTurns"][0]["tiles"][0]
            assert first_tile_name.lower().startswith("_audapack_") and first_tile_name.lower().endswith(".zip"), (
                f"Expected the canonical _AUDAPACK_*.zip in the submitted payload, got {first_tile_name}"
            )
            # The exact authored text is the outgoing payload; it was submitted,
            # never rewritten.
            assert first["userTurns"][0]["text"] == "Initial user prompt for Chat A", (
                f"Outgoing payload text corrupted: {first['userTurns'][0]['text']}"
            )

            side_effects = await audit_side_effects(page)
            assert side_effects == {"autoEnabled": False, "startHandoff": None, "a3Intent": None, "workerLease": None}, (
                f"manual ZIP must never start an audit campaign: {side_effects}"
            )

            # Verify button label updated with project identity
            btn_text_after = await page.evaluate("() => document.querySelector('#acb-manual-zip-btn').textContent.trim()")
            assert "AUDAPACK" in btn_text_after, f"Button label must include project name: {btn_text_after}"

            # ================================================================
            # TARGET C: identical repeat -> ALREADY_SENT, zero transport work,
            # zero second Send, no duplicate user turn
            # ================================================================
            before_dup = await composer_state(page)
            duplicate = await manual_zip(page)
            assert duplicate.get("ok") is True, f"Identical repeat failed: {duplicate}"
            assert duplicate.get("code") == "ALREADY_SENT", f"Expected ALREADY_SENT, got {duplicate}"
            assert duplicate.get("ensureCode") == "REUSED_EXISTING", (
                f"Expected a metadata-only ensure, got {duplicate.get('ensureCode')}"
            )
            assert duplicate.get("getCount") == 0, f"ALREADY_SENT performed a GET: {duplicate.get('getCount')}"
            assert duplicate.get("injectionCount") == 0, (
                f"ALREADY_SENT constructed/injected a File: {duplicate.get('injectionCount')}"
            )
            assert duplicate.get("sendAttempts") == 0, f"ALREADY_SENT performed a Send: {duplicate.get('sendAttempts')}"

            after_dup = await composer_state(page)
            assert after_dup["ensurePosts"] == before_dup["ensurePosts"] + 1, (
                "the duplicate must prove archive freshness with one metadata ensure"
            )
            assert after_dup["archiveGets"] == before_dup["archiveGets"], "ALREADY_SENT performed a redundant GET"
            assert after_dup["injections"] == before_dup["injections"], "ALREADY_SENT injected an attachment"
            assert after_dup["sendClicks"] == before_dup["sendClicks"], "ALREADY_SENT clicked Send"
            assert after_dup["acceptedSends"] == before_dup["acceptedSends"], "ALREADY_SENT created a duplicate turn"
            assert after_dup["userTurns"] == before_dup["userTurns"], "ALREADY_SENT added a duplicate user turn"

            # ================================================================
            # TARGET D: ALREADY_ATTACHED transport state + NEW outgoing payload
            # -> zero GET, zero injection, Send exactly once, verified SENT
            # ================================================================
            # The attach-only escape hatch leaves the canonical archive in the
            # composer without sending: that is the transport state ALREADY_ATTACHED
            # must recognize.
            attached_only = await manual_zip(page, attachOnly=True)
            assert attached_only.get("ok") is True, f"attach-only failed: {attached_only}"
            assert attached_only.get("code") == "ATTACHED_NEW", f"Expected ATTACHED_NEW, got {attached_only}"
            attached_state = await composer_state(page)
            assert attached_state["sendClicks"] == before_dup["sendClicks"], "attach-only must never Send"
            assert len(attached_state["tiles"]) == 1, f"attach-only must leave one tile: {attached_state['tiles']}"
            attached_tile = attached_state["tiles"][0]

            # Same canonical archive, genuinely different outgoing text.
            await page.evaluate("() => { window.__promptSurface.textContent = 'Second outgoing payload'; }")
            sent_from_attached = await manual_zip(page)
            assert sent_from_attached.get("ok") is True, f"ALREADY_ATTACHED -> Send failed: {sent_from_attached}"
            assert sent_from_attached.get("code") == "SENT", (
                f"ALREADY_ATTACHED must still Send, got {sent_from_attached.get('code')}"
            )
            assert sent_from_attached.get("ensureCode") == "REUSED_EXISTING", (
                f"Expected a metadata-only ensure, got {sent_from_attached.get('ensureCode')}"
            )
            assert sent_from_attached.get("getCount") == 0, (
                f"ALREADY_ATTACHED performed a GET: {sent_from_attached.get('getCount')}"
            )
            assert sent_from_attached.get("injectionCount") == 0, (
                f"ALREADY_ATTACHED injected a second File: {sent_from_attached.get('injectionCount')}"
            )
            assert sent_from_attached.get("sendAttempts") == 1, "the ALREADY_ATTACHED path must Send exactly once"

            after_attached_send = await composer_state(page)
            assert after_attached_send["sendClicks"] == attached_state["sendClicks"] + 1
            assert after_attached_send["archiveGets"] == attached_state["archiveGets"], "no GET on the re-send"
            assert after_attached_send["injections"] == attached_state["injections"], "no injection on the re-send"
            assert after_attached_send["userTurns"] == [
                {"text": "Initial user prompt for Chat A", "tiles": [first_tile_name]},
                {"text": "Second outgoing payload", "tiles": [attached_tile]},
            ], f"Unexpected outgoing turns: {after_attached_send['userTurns']}"

            # ================================================================
            # TARGET E: Send unavailable -> bounded SEND_TIMEOUT, archive and
            # payload preserved, then a Send-only retry reaches SENT
            # ================================================================
            await page.evaluate("""() => {
                window.__promptSurface.textContent = 'Payload that must survive an unavailable Send';
                window.__sendButton.disabled = true;
            }""")
            before_timeout = await composer_state(page)
            timed_out = await manual_zip(page)
            assert timed_out.get("ok") is False, f"Expected a bounded Send failure, got {timed_out}"
            assert timed_out.get("code") == "SEND_TIMEOUT", f"Expected SEND_TIMEOUT, got {timed_out}"
            assert timed_out.get("errorCode") == "send-timeout", f"Expected send-timeout, got {timed_out.get('errorCode')}"
            assert timed_out.get("sendAttempts") == 1, "the transaction really attempted the Send stage"

            timeout_state = await composer_state(page)
            assert timeout_state["sendClicks"] == before_timeout["sendClicks"], "a disabled Send must not be clicked"
            assert timeout_state["acceptedSends"] == before_timeout["acceptedSends"], "nothing was accepted"
            assert timeout_state["userTurns"] == before_timeout["userTurns"], "no turn may appear without acceptance"
            assert timeout_state["tiles"] == [attached_tile], "the ready archive stays attached"
            assert timeout_state["text"] == "Payload that must survive an unavailable Send", (
                "the unsubmitted payload survives"
            )
            assert timeout_state["injections"] == before_timeout["injections"] + 1, (
                "the failing transaction performed only its own single attach"
            )
            assert timeout_state["archiveGets"] == before_timeout["archiveGets"], (
                "the verified byte cache serves the attach without a GET"
            )
            receipts = await page.evaluate("() => window.__ACB_TEST__.manualArchiveSentStore().entries.length")
            assert receipts == 1, f"no receipt may be written before positive acceptance: {receipts}"

            # Restore Send availability and retry: Send only, same ready archive.
            await page.evaluate("() => { window.__sendButton.disabled = false; }")
            retry = await manual_zip(page)
            assert retry.get("ok") is True, f"Send-only retry failed: {retry}"
            assert retry.get("code") == "SENT", f"Expected SENT on retry, got {retry.get('code')}"
            assert retry.get("ensureCode") == "REUSED_EXISTING", "the retry is a metadata no-op, not a repack"
            assert retry.get("getCount") == 0, f"the retry performed a GET: {retry.get('getCount')}"
            assert retry.get("injectionCount") == 0, f"the retry injected: {retry.get('injectionCount')}"

            after_retry = await composer_state(page)
            assert after_retry["archiveGets"] == timeout_state["archiveGets"], "a Send-only retry downloads nothing"
            assert after_retry["injections"] == timeout_state["injections"], "a Send-only retry injects nothing"
            assert after_retry["sendClicks"] == before_timeout["sendClicks"] + 1
            assert after_retry["userTurns"][-1] == {
                "text": "Payload that must survive an unavailable Send",
                "tiles": [attached_tile],
            }, f"Unexpected retry turn: {after_retry['userTurns'][-1]}"

            # ================================================================
            # TARGET F: ten rapid clicks collapse into one transaction
            # ================================================================
            await page.evaluate("() => { window.__promptSurface.textContent = 'Rapid payload'; }")
            before_rapid = await composer_state(page)
            rapid_results = await page.evaluate("""async () => {
                const api = window.__ACB_TEST__;
                const calls = [];
                for (let i = 0; i < 10; i += 1) calls.push(api.manualArchiveZipAction());
                return await Promise.all(calls);
            }""")
            coalesced = [r for r in rapid_results if r.get("dedupe") == "DUPLICATE_COALESCED"]
            primary = [r for r in rapid_results if r.get("dedupe") != "DUPLICATE_COALESCED"]
            assert len(coalesced) == 9, f"expected 9 coalesced callers, got {len(coalesced)}: {rapid_results}"
            assert all(r.get("ok") is True for r in rapid_results), f"a rapid caller failed: {rapid_results}"
            assert len(primary) == 1 and primary[0].get("code") == "SENT", f"unexpected primary result: {primary}"

            after_rapid = await composer_state(page)
            assert after_rapid["ensurePosts"] == before_rapid["ensurePosts"] + 1, "one effective ensure chain"
            assert after_rapid["archiveGets"] == before_rapid["archiveGets"], "at most one GET (byte cache hit)"
            assert after_rapid["injections"] == before_rapid["injections"] + 1, "one injection"
            assert after_rapid["sendClicks"] == before_rapid["sendClicks"] + 1, "one Send"
            assert after_rapid["acceptedSends"] == before_rapid["acceptedSends"] + 1, "one verified outgoing message"
            assert after_rapid["userTurns"][-1] == {"text": "Rapid payload", "tiles": [attached_tile]}, (
                f"no duplicate ChatGPT turns allowed: {after_rapid['userTurns']}"
            )
            assert len(after_rapid["userTurns"]) == len(before_rapid["userTurns"]) + 1

            # ================================================================
            # NAVIGATION RACE: in-flight transaction is fenced from the
            # destination chat; origin never Sends there
            # ================================================================
            race_before = await composer_state(page)
            race_result = await page.evaluate("""() => {
                const api = window.__ACB_TEST__;
                // Start a manual ZIP in Chat A, then navigate to Chat C while
                // the ensure/download is still resolving.
                const promise = api.manualArchiveZipAction();
                history.pushState({}, '', '/c/chat-c-race');
                return promise;
            }""")
            assert race_result.get("ok") is False, f"Expected failure on race, got {race_result}"
            assert race_result.get("errorCode") == "conversation_changed", (
                f"Expected errorCode conversation_changed, got {race_result}"
            )
            race_after = await composer_state(page)
            assert race_after["sendClicks"] == race_before["sendClicks"], "the origin transaction never Sends in Chat C"
            assert race_after["userTurns"] == race_before["userTurns"], "no turn may appear in the destination chat"

            # ================================================================
            # CHAT B: different project bound, zero binding/proof leakage,
            # restores on return to Chat A
            # ================================================================
            await page.evaluate("""() => {
                history.pushState({}, '', '/c/chat-b-9999');
                window.__ACB_TEST__.bindAutoRuntimeToCurrentConversation();
            }""")

            binding_b = await page.evaluate("() => window.__ACB_TEST__.manualArchiveBindingFor()")
            assert binding_b is None, f"Chat B should not inherit Chat A binding: {binding_b}"

            btn_text_b = await page.evaluate("() => document.querySelector('#acb-manual-zip-btn').textContent.trim()")
            assert "ZIP" in btn_text_b and "AUDAPACK" not in btn_text_b, f"Chat B button must not show AUDAPACK: {btn_text_b}"

            await page.evaluate("""() => {
                const api = window.__ACB_TEST__;
                api.setManualArchiveBinding({ project_id: 'fastprompter', display_name: '_FASTPROMPTER' });
                api.renderManualArchiveControl();
            }""")
            btn_text_b2 = await page.evaluate("() => document.querySelector('#acb-manual-zip-btn').textContent.trim()")
            assert "FASTPROMPTER" in btn_text_b2, f"Chat B must show FASTPROMPTER: {btn_text_b2}"

            await page.evaluate("""() => {
                history.pushState({}, '', '/c/chat-a-1234');
                window.__ACB_TEST__.bindAutoRuntimeToCurrentConversation();
            }""")
            binding_a_restored = await page.evaluate("() => window.__ACB_TEST__.manualArchiveBindingFor()")
            assert binding_a_restored["project_id"] == "audapack", "Chat A binding lost on return"

            # ================================================================
            # MULTI-TAB: independent draft bindings co-exist in storage
            # ================================================================
            page_tab1 = page
            page_tab2 = await browser.new_page(viewport={"width": 1280, "height": 800})

            await setup_widget_page(page_tab2, bridge, initial_path="/")

            tab1_key = await page_tab1.evaluate("""() => {
                const api = window.__ACB_TEST__;
                history.pushState({}, '', '/');
                const key = 'draft:tab-1-key';
                api.setManualArchiveBinding({ project_id: 'audapack', display_name: '_AUDAPACK' }, key);
                return key;
            }""")

            tab2_key = await page_tab2.evaluate("""() => {
                const api = window.__ACB_TEST__;
                history.pushState({}, '', '/');
                const key = 'draft:tab-2-key';
                api.setManualArchiveBinding({ project_id: 'fastprompter', display_name: '_FASTPROMPTER' }, key);
                return key;
            }""")

            tab1_stored = await page_tab1.evaluate(f"() => window.__ACB_TEST__.manualArchiveBindingFor('{tab1_key}')")
            tab2_stored = await page_tab2.evaluate(f"() => window.__ACB_TEST__.manualArchiveBindingFor('{tab2_key}')")

            assert tab1_stored["project_id"] == "audapack", f"Tab 1 binding corrupted: {tab1_stored}"
            assert tab2_stored["project_id"] == "fastprompter", f"Tab 2 binding corrupted: {tab2_stored}"

            await page_tab1.evaluate(f"""() => {{
                const api = window.__ACB_TEST__;
                api.migrateManualArchiveBinding('{tab1_key}', 'c:tab-1-stable');
            }}""")

            migrated_tab1 = await page_tab1.evaluate("() => window.__ACB_TEST__.manualArchiveBindingFor('c:tab-1-stable')")
            assert migrated_tab1["project_id"] == "audapack", f"Migrated Tab 1 binding invalid: {migrated_tab1}"

            tab2_preserved = await page_tab2.evaluate(f"() => window.__ACB_TEST__.manualArchiveBindingFor('{tab2_key}')")
            assert tab2_preserved["project_id"] == "fastprompter", f"Tab 2 draft binding was clobbered: {tab2_preserved}"

            await browser.close()
    finally:
        bridge.teardown()
