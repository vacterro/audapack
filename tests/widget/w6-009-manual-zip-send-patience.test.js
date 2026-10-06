'use strict';

// The operator's ZIP waits out its Send budget while ChatGPT uploads.
// ChatGPT shows no spinner while it uploads a large archive, so the tile
// looks ready and Send stays disabled for several seconds. The shared wait
// gave up after 1.4 s of that, and 2 of 3 real 40 MB ZIPs in the diagnostics
// log ended as SEND_TIMEOUT with send_ready_ms around 1.5 s.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture, addComposerAttachmentTile } = require('./helpers');

// Advance FAKE time by a fixed amount. The old loop counted real wall-clock
// milliseconds, so the fake clock it advanced depended on machine load: a busy
// box reached the 5 s idle give-up inside a "2 s" drive and failed a test whose
// whole point is that the wait survives a silent upload.
async function drive(h, fakeMs, step = 30) {
  for (let advanced = 0; advanced < fakeMs; advanced += step) {
    h.advance(step);
    await new Promise(resolve => setImmediate(resolve));
  }
}

test('W6-009: the manual ZIP patience outlasts a silent upload', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  addComposerAttachmentTile(h, '_ZAICODE.zip');
  send.setAttribute('aria-disabled', 'true');

  let settled = false;
  const pending = api.waitForChatGPTSendReady(5000, 300000, { idleGiveUpMs: 5000 }).then(value => {
    settled = true;
    return value;
  });

  await drive(h, 2000);
  assert.strictEqual(settled, false, 'the wait must not give up at 1.4 s while the budget remains');

  send.setAttribute('aria-disabled', 'false');
  await drive(h, 300);
  assert.strictEqual(await pending, send);
});

test('W6-009: an automatic send waits out a silent upload instead of demanding the operator', async () => {
  // The START dispatch attaches the Core as a prompt file, and ChatGPT can
  // keep Send disabled for seconds with NO busy tile while it registers that
  // attachment server-side (observed live 2026-10-04: button=found
  // disabled=true, ready ~4s later). The 1.4 s idle exit misread that as a
  // dead composer and the run was told "Press Send manually" -- on a START
  // that is supposed to need nobody. Every automatic send must wait out its
  // own bounded budget (12 s / 30 s with attachment) before it may fall back.
  const { h, api } = setup();
  const { input, send } = composerFixture(h);
  addComposerAttachmentTile(h, '_ZAICODE.zip');
  input.textContent = 'START payload';
  send.setAttribute('aria-disabled', 'true');

  const promise = api.triggerSend(api.detectSite(), input, {});
  await drive(h, 3000);
  assert.strictEqual(send._clickCount, undefined,
    'nothing may click a Send the platform still reports as not ready');

  send.setAttribute('aria-disabled', 'false');
  // Drain the whole fake-timer chain: the click plus up to three acceptance
  // windows (2.5s each) inside clickChatGPTSendVerified.
  await h.settle();
  const result = await promise;
  assert.notStrictEqual(result.mode, 'manual-only',
    'a silent attachment upload must never become manual-send-required');
  assert.strictEqual(send._clickCount, 1,
    'the wait must click Send itself once the platform enables it');
});

test('W6-009: automated callers keep the short early exit', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  addComposerAttachmentTile(h, '_ZAICODE.zip');
  send.setAttribute('aria-disabled', 'true');

  let settled = false;
  const pending = api.waitForChatGPTSendReady(5000, 300000).then(value => {
    settled = true;
    return value;
  });
  await drive(h, 2000);
  assert.strictEqual(settled, true, 'a dead composer must not hold a lane');
  assert.strictEqual(await pending, null);
});
