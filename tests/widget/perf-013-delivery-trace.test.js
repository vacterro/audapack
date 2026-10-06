'use strict';

// APP-PERF-001: real delivery latency is now TRACED and the avoidable internal
// gaps are gone. Each test pins one structural guarantee:
//
//   TARGET P  one bounded per-delivery trace (manual + worker), durations only
//   TARGET Q  compact phase line naming the dominant phase, no DevTools needed
//   TARGET R/T  attachment readiness releases the MOMENT the condition is true
//   TARGET U  a remounted Send node never reopens a second full timeout
//   TARGET V  a healthy empty poll re-enters promptly (no failure backoff)
//   TARGET W  the trace is bounded and carries no text/token material
//
// perf-011 already pins the manual fast paths (zero registry GET, hot proof,
// SHA receipts, byte cache, ALREADY_ATTACHED / ALREADY_SENT zero-transport).

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const {
  setup,
  composerFixture,
  addComposerAttachmentTile
} = require('./helpers');

const WIDGET_PATH = path.join(__dirname, '..', '..', 'resources', 'AUDAPACK_WIDGET.user.js');

test('PERF-013: delivery trace is bounded and formats a compact phase line', () => {
  const { api } = setup();
  const t = api.manualArchiveTimingBegin();

  // TARGET P/W: a fixed, bounded key set -- a trace that can grow without
  // bound is a leak wearing a stopwatch.
  // SRC-083 raised the ceiling once, deliberately, for the 12-field GET split.
  assert.ok(Object.keys(t).length <= 48, 'trace keys stay bounded');
  assert.strictEqual(typeof t.trace_id, 'string');
  assert.ok(t.trace_id.length > 0 && t.trace_id.length <= 32, 'trace id stays short');
  assert.strictEqual(t.path, 'manual');

  api.manualArchiveTimingSet(t, 'ensure_total_ms', 30);
  api.manualArchiveTimingSet(t, 'download_ms', 140);
  api.manualArchiveTimingSet(t, 'attachment_ready_ms', 1920);
  api.manualArchiveTimingSet(t, 'send_ready_ms', 420);
  api.manualArchiveTimingSet(t, 'send_verify_ms', 180);
  api.manualArchiveTimingMark(t, 'total_ms', t.startedAt);

  // TARGET Q: the compact diagnostic the operator reads off the status line.
  const line = api.manualArchiveTimingLine(t);
  assert.match(
    line,
    /^ZIP \d+\.\d{2}s \| ensure \d+\.\d{2}s \| GET \d+\.\d{2}s \| attach \d+\.\d{2}s \| send-ready \d+\.\d{2}s \| verify \d+\.\d{2}s$/
  );
  assert.ok(line.includes('0.03s') && line.includes('1.92s'), 'real durations survive formatting');

  // TARGET Q: the next slow delivery can name its dominant phase.
  assert.strictEqual(api.manualArchiveTimingDominant(t), 'attachment_ready_ms');

  // TARGET W (regression matrix 20): no composer text, tokens or credential
  // material can ride a trace. Only numbers, booleans and short ids.
  const json = JSON.stringify(t);
  for (const banned of ['token', 'password', 'secret', 'bearer', 'credential', 'composer_text']) {
    assert.ok(!json.toLowerCase().includes(banned), `trace must not carry ${banned}`);
  }
  for (const value of Object.values(t)) {
    if (typeof value === 'string') {
      assert.ok(value.length <= 64, `string trace field too large: ${value.slice(0, 16)}...`);
    }
  }
});

test('PERF-013: the worker trace shares the same timing system and names its phases', () => {
  const { api } = setup();
  const t = api.manualArchiveTimingBegin();
  t.path = 'worker';
  api.manualArchiveTimingSet(t, 'poll_wait_ms', 300);
  api.manualArchiveTimingSet(t, 'artifact_fetch_ms', 500);
  api.manualArchiveTimingSet(t, 'attachment_ready_ms', 1100);
  api.manualArchiveTimingSet(t, 'send_accept_ms', 500);
  api.manualArchiveTimingSet(t, 'transition_ack_ms', 200);

  const line = api.manualArchiveTimingLine(t);
  assert.match(line, /^Worker \d+\.\d{2}s \| poll /);
  assert.strictEqual(api.manualArchiveTimingDominant(t), 'attachment_ready_ms');
});

test('PERF-013: one diagnostics record per trace, published exactly once', () => {
  const { h, api } = setup();
  const t = api.manualArchiveTimingBegin();
  api.manualArchiveTimingSet(t, 'download_ms', 140);

  const line = api.manualArchivePublishTiming(t);
  // A second finish of the SAME timings object (transaction outcome plus the
  // click-level action) must not publish a duplicate record.
  api.manualArchivePublishTiming(t);

  assert.match(line, /^ZIP /);
  assert.strictEqual(t.published, true);

  const stores = [h.gmStore, h.sessionStore, h.localStore]
    .map(store => Array.from(store.values()).map(value => JSON.stringify(value)).join('\n'))
    .join('\n');
  assert.strictEqual(
    (stores.match(/delivery_timing/g) || []).length,
    1,
    'exactly one delivery_timing diagnostics record per trace'
  );
});

test('PERF-013: Send readiness releases immediately when the button is already usable', async () => {
  const { h, api } = setup();
  const fixture = composerFixture(h);

  const startedAt = h.timers.now;
  const send = await api.waitForChatGPTSendReady(40000);

  assert.ok(send, 'usable Send is returned');
  assert.strictEqual(h.timers.now - startedAt, 0, 'zero wait when the condition is already true (TARGET R/U)');
  assert.strictEqual(send, fixture.send, 'the live composer button is returned');
});

test('PERF-013: a remounted Send node never reopens a second full timeout (TARGET U)', () => {
  const src = fs.readFileSync(WIDGET_PATH, 'utf8');
  assert.ok(
    !src.includes('if (!sendUsable(send)) send = await waitForChatGPTSendReady(MANUAL_ARCHIVE_SEND_READY_TIMEOUT_MS);'),
    'the double full-budget Send wait must stay gone (it doubled the documented deadline)'
  );
  assert.ok(
    src.includes('const remainingMs = Math.max(0, sendBudgetMs - (manualArchiveNow() - readyStarted));'),
    'Send reacquisition must stay inside the original budget\'s remaining time'
  );
});

test('PERF-013: worker attachment wait releases by condition, not by a 200ms poll', async () => {
  const { h, api } = setup();
  composerFixture(h);

  const startedAt = h.timers.now;
  const pending = api.waitForExactProjectAttachment({ filename: 'ART.zip', expectedSize: 0, timeoutMs: 40000 });
  addComposerAttachmentTile(h, 'ART.zip');

  // PERF-003 (audit/12.md): the release must come from the MutationObserver on
  // the composer subtree, with no timer advanced at all. The interval beside it
  // is only the coarse re-anchor fallback, and is asserted separately.
  let result = null;
  pending.then(value => { result = value; });
  h.flushObservers();
  for (let i = 0; i < 5 && !result; i += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }

  assert.ok(result, 'the observer released the wait without reaching its 40s upper bound');
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.reason, 'exact-match');
  const elapsed = h.timers.now - startedAt;
  assert.strictEqual(
    elapsed, 0,
    `released by the observable condition, not by any timer (elapsed ${elapsed}ms)`
  );
});

test('PERF-003: the composer-remount fallback is coarse, and shared by both waits', () => {
  const src = fs.readFileSync(WIDGET_PATH, 'utf8');
  const fallback = src.match(/COMPOSER_REMOUNT_FALLBACK_MS\s*=\s*(\d+)/);
  assert.ok(fallback, 'the shared fallback constant must exist');
  const ms = Number(fallback[1]);
  assert.ok(ms >= 250 && ms <= 500, `fallback is inside the 250-500ms corridor (got ${ms}ms)`);
  assert.ok(
    !/setInterval\(\s*\(\)\s*=>\s*\{\s*watch\(\);\s*check\(\);\s*\}\s*,\s*60\s*\)/.test(src),
    'the near-frame-rate 60ms composer poll must stay gone'
  );
  // Shared primitive, not a copy per wait: the observer body appears once.
  const reanchors = src.match(/observer\.observe\(live, \{/g) || [];
  assert.strictEqual(
    reanchors.length, 1,
    `both attachment waits must re-anchor through one primitive (found ${reanchors.length})`
  );
});

test('PERF-013: healthy worker polls idle in a local timer, never in a held request (TARGET V)', () => {
  // The userscript manager queues every window's requests together, so a
  // poll the Bridge holds open delays the operator's own ZIP ensure/GET. The
  // healthy gap is therefore a short local timer, and the poll asks for no
  // server-side hold at all.
  const { api } = setup();
  const healthyDelay = api.browserWorkerPollBackoff();
  assert.ok(healthyDelay >= 1000 && healthyDelay <= 3000,
    `healthy empty poll idles briefly without a tight loop (got ${healthyDelay}ms)`);

  // Failure backoff must remain a real, growing table -- no busy loop, and no
  // removal of the backoff either (regression matrix 16).
  const src = fs.readFileSync(WIDGET_PATH, 'utf8');
  assert.ok(
    src.includes('const table = [1000, 2000, 5000, 15000, 30000];'),
    'repeated poll failures must still back off through the bounded table'
  );
});
