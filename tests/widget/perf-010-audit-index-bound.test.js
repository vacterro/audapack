'use strict';

// PERF-001 (audit/10.md): the audit-result metadata index must stay hard
// bounded on EVERY update, not only after complete runs. Abandoned/failed
// conversations used to keep every summary entry forever, so each write
// parsed and re-serialized an ever-growing monolithic JSON document.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

const INDEX_KEY = 'ai_chatbuttons_audit_result_index_v1';
const MAX_CONVERSATIONS = 50;

function record(i) {
  return {
    version: 1,
    conversationKey: `c:perf010-${i}`,
    runId: `run-perf010-${i}`,
    kind: 'core',
    projectName: 'AUDAPACK',
    profileId: 'quick3',
    profileVersion: '1.0.0',
    waveIndex: 1,
    waveCount: 3,
    completedAt: Date.now(),
    text: [
      'PROJECT_NAME: AUDAPACK',
      'CAMPAIGN_PROFILE: quick3',
      `CAMPAIGN_RUN_ID: run-perf010-${i}`,
      'WAVE_ID: core',
      'STATUS: AUDIT_CORE: COMPLETE',
      'TICKETS: 0',
      'HANDOFF: IMPLEMENTATION_AGENT',
      'x'.repeat(1500)
    ].join('\n')
  };
}

function indexEntries(h) {
  return JSON.parse(h.gmStore.has(INDEX_KEY) ? h.gmStore.get(INDEX_KEY) : '{}');
}

function waveBlobKey(conversationKey) {
  return `ai_chatbuttons_audit_result_v1::${conversationKey}::core`;
}

test('PERF-001 (audit/10.md): abandoned conversations keep the metadata index hard-bounded', () => {
  const { h, api } = setup();

  // 2,100 distinct UNFINISHED conversations, exactly the audit's measured
  // reproduction shape: one Core result each, never a complete run.
  for (let i = 0; i < 2100; i += 1) {
    assert.strictEqual(api.writeAuditResult(record(i)), true, `write ${i} failed`);
  }

  const entries = indexEntries(h);
  const keys = Object.keys(entries);
  assert.ok(
    keys.length <= MAX_CONVERSATIONS,
    `index must stay bounded at ${MAX_CONVERSATIONS}, got ${keys.length}`
  );

  // Every evicted conversation's sole surviving wave blob must still be there.
  const evicted = record(0).conversationKey;
  assert.strictEqual(api.readAuditResultFresh('core', evicted).runId, 'run-perf010-0',
    'an evicted conversation keeps its stored wave');

  // Revisit the evicted conversation: its metadata must be reconstructed.
  assert.strictEqual(api.writeAuditResult({ ...record(0), text: record(0).text + '\nnew wave data' }), true);
  const revisited = indexEntries(h)[evicted];
  assert.ok(revisited, 'revisiting an evicted conversation rehydrates its metadata entry');
  assert.strictEqual(revisited.complete, false);
  assert.ok(revisited.pending, 'an in-progress conversation reads as pending');
});

test('PERF-001 (audit/10.md): write cost stays flat as history grows', () => {
  const { h, api } = setup();

  const batch = start => {
    h.counters.gmGet = 0;
    h.counters.gmSet = 0;
    for (let i = start; i < start + 100; i += 1) {
      assert.strictEqual(api.writeAuditResult(record(i)), true, `write ${i} failed`);
    }
    return { gets: h.counters.gmGet, sets: h.counters.gmSet };
  };

  batch(0);
  const early = batch(100);

  // Grow history far past the bound, then measure again.
  for (let i = 200; i < 2100; i += 1) api.writeAuditResult(record(i));
  const late = batch(2100);

  // O(1)-per-write index work: the same constant per-write budget, not one
  // that grows with the 2,000 abandoned conversations now in storage.
  const perWriteEarly = early.gets / 100;
  const perWriteLate = late.gets / 100;
  assert.ok(
    perWriteLate <= perWriteEarly + 8,
    `late per-write reads (${perWriteLate}) must stay near early (${perWriteEarly}), not scale with history`
  );
});
