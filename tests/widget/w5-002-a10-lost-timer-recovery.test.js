'use strict';

// A10 lost-timer recovery. A Super10 campaign completes Architecture, durably
// advances to sending-correctness, and then loses its ephemeral next-wave
// timer -- exactly what a reload, a route hydration or any runtime
// re-evaluation does to a setTimeout. The runtime evaluation path recognized
// only sending-second / sending-performance (and await-second-user /
// await-performance-user), so the Correctness wave was never re-armed:
// evaluation fell into the generic wait-stage tail, read the still-visible
// Architecture turn as a conflicting audit command, and stranded the campaign
// right after its first response.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture } = require('./helpers');

const RUN_ID = 'run-a10-lost-timer';
const CONVO = 'c:abc123';

function handoffFor(api, waveDef, profileId) {
  const profile = api.EMBEDDED_AUDIT_PROFILES.profiles[profileId];
  const pfx = waveDef.ticket_prefix.replace(/-$/, '');
  const termKey = waveDef.terminal_status_key || waveDef.slug;
  const fields = (waveDef.ticket_fields && waveDef.ticket_fields.length)
    ? waveDef.ticket_fields
    : ['EVIDENCE', 'DEFECT', 'REPAIR', 'VERIFY'];
  return `PROJECT_NAME: AUDAPACK
DATE_TIME: 2026-09-07T12:00:00+03:00
CAMPAIGN_PROFILE: ${profileId}
CAMPAIGN_RUN_ID: ${RUN_ID}
WAVE_ID: ${waveDef.id}
WAVE_INDEX: ${waveDef.ordinal}
WAVE_COUNT: ${profile.waves.length}
WAVE: ${waveDef.wave_header}
STATUS: ${termKey}: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [${pfx}-001] Sample defect issue title
${fields.map(field => `${field}: sample ${field.toLowerCase()}.`).join('\n')}

${waveDef.done_marker.replace(/:\s*$/, '')}: All tickets and handoffs are verified.`;
}

function campaignWindow(profileId) {
  const { h, api } = setup();
  composerFixture(h);
  api.state.auditProfile = profileId;
  // Text delivery keeps the automatic wave send observable in the harness
  // composer (attachment delivery has no upload input to bind here).
  api.state.chatgptPromptDelivery = 'text';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId });
  api.autoRuntime.conversationKey = CONVO;
  api.autoRuntime.runId = RUN_ID;
  api.autoRuntime.startedAt = Date.now();
  return { h, api };
}

function super10Window() {
  return campaignWindow('super10');
}

// Seed the first `count` waves of the profile as durably COMPLETE: visible
// command turns (unless mountTurns is false, i.e. the whole DOM is
// virtualized after a reload), one-record-per-wave cache entries for this
// exact run, and the runtime's wave anchors.
function seedWaves(h, api, waves, count, profileId, options = {}) {
  const mountTurns = options.mountTurns !== false;
  const turns = [];
  for (let i = 0; i < count; i += 1) {
    const w = waves[i];
    const handoff = handoffFor(api, w, profileId);
    if (mountTurns) {
      turns.push(userTurn(h, `u-${w.id}`, `${w.wave_header}\nACB_CHAIN_RECEIPT: startwave-${w.id}`));
      const a = assistantTurn(h, `a-${w.id}`);
      a._text = handoff;
      turns.push(a);
    }
    api.setWaveUserId(w.id, `u-${w.id}`);
    const written = api.writeAuditResult({
      version: 1,
      conversationKey: CONVO,
      runId: RUN_ID,
      kind: w.id,
      gateState: 'complete',
      text: handoff,
      completedAt: Date.now()
    });
    assert.strictEqual(written, true, `seed record for ${w.id}`);
  }
  if (mountTurns && turns.length) addTurns(h, turns);
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);
}

function composerText(h, api) {
  const input = mainEl(h).querySelector('#prompt-textarea');
  return input ? api.composerPlainText(input) : '';
}

// Advance fake timers inside a bounded budget, draining microtask chains
// between steps, so a scheduled next-wave send runs without wandering into
// unrelated multi-second watchdog loops.
async function pump(h, budgetMs) {
  let left = Number(budgetMs);
  for (let guard = 0; guard < 400 && left > 0; guard += 1) {
    await new Promise(resolve => setImmediate(resolve));
    const pending = h.timers.pending().filter(due => due <= left);
    if (!pending.length) break;
    const step = Math.max(...pending);
    h.advance(step);
    left -= step;
  }
  await new Promise(resolve => setImmediate(resolve));
}

async function evaluateAndDeliver(h, api, budgetMs = 5000) {
  const evaluating = api.evaluateAutoAudit();
  await evaluating;
  await pump(h, budgetMs);
}

function receiptCount(text) {
  return (String(text).match(/ACB_CHAIN_RECEIPT:/g) || []).length;
}

// ---------------------------------------------------------------------------
// 1. The primary lost-timer scenario
// ---------------------------------------------------------------------------

test('A10: Architecture COMPLETE -> sending-correctness re-arms after the ephemeral timer is lost', async () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  assert.strictEqual(waves[0].id, 'architecture');
  assert.strictEqual(waves[1].id, 'correctness');

  const archUser = userTurn(h, 'u-architecture', 'AUDIT ARCHITECTURE / SYSTEM INVARIANTS\nACB_CHAIN_RECEIPT: startcore-arch');
  const archAssistant = assistantTurn(h, 'a-architecture');
  archAssistant._text = handoffFor(api, waves[0], 'super10');
  addTurns(h, [archUser, archAssistant]);

  api.autoRuntime.stage = 'wait-architecture';
  api.autoRuntime.currentWaveId = 'architecture';
  api.autoRuntime.currentWaveIndex = 1;
  api.setWaveUserId('architecture', 'u-architecture');
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  const commit = api.commitTerminalWaveResult('architecture', archAssistant._text, 'complete', 'u-architecture');
  assert.strictEqual(commit.ok, true);
  assert.strictEqual(commit.nextWave, 'correctness');
  assert.strictEqual(api.autoRuntime.stage, 'sending-correctness');

  // The ephemeral scheduled timer is LOST, as after reload / route hydration.
  api.clearAutoTimers();

  // Runtime re-evaluation from durable state alone must reschedule the wave.
  await evaluateAndDeliver(h, api);

  const composer = composerText(h, api);
  assert.match(
    composer,
    /AUDIT CORRECTNESS/,
    'Correctness must be scheduled and sent again from the durable sending-correctness state'
  );
  assert.strictEqual(api.autoRuntime.stage, 'await-correctness-user');
  assert.notStrictEqual(api.autoRuntime.stage, 'idle', 'must not fall to READY');
  assert.notStrictEqual(api.autoRuntime.stage, 'paused', 'must not pause on the still-visible Architecture turn');
  assert.notStrictEqual(api.autoRuntime.stage, 'wait-architecture', 'must not restart Architecture');
  assert.strictEqual(api.getActiveProfile().profile_id, 'super10', 'must not switch profile');
  assert.strictEqual(receiptCount(composer), 1, 'exactly one wave prompt may be sent');
  assert.ok(!composer.includes('WAVE_ID: architecture'), 'the completed wave must not be resent');
});

// ---------------------------------------------------------------------------
// 2. Parameterized: every adjacent Super10 transition recovers a lost timer
// ---------------------------------------------------------------------------

const SUPER10_TRANSITIONS = [];
{
  const probe = super10Window();
  const waves = probe.api.getActiveProfile().waves;
  for (let i = 0; i + 1 < waves.length; i += 1) {
    SUPER10_TRANSITIONS.push({ from: waves[i], to: waves[i + 1] });
  }
}

for (const { from, to } of SUPER10_TRANSITIONS) {
  test(`A10 transition: ${from.id} COMPLETE -> sending-${to.id} recovers a lost timer`, async () => {
    const { h, api } = super10Window();
    const waves = api.getActiveProfile().waves;
    const fromIndex = waves.findIndex(w => w.id === from.id);

    // waves[0..fromIndex-1] are already durably complete.
    seedWaves(h, api, waves, fromIndex, 'super10');

    api.autoRuntime.stage = `wait-${from.id}`;
    api.autoRuntime.currentWaveId = from.id;
    api.autoRuntime.currentWaveIndex = from.ordinal;
    assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

    const commit = api.commitTerminalWaveResult(from.id, handoffFor(api, from, 'super10'), 'complete', `u-${from.id}`);
    assert.strictEqual(commit.ok, true, `${from.id} must commit COMPLETE`);
    assert.strictEqual(commit.nextWave, to.id);
    assert.strictEqual(api.autoRuntime.stage, `sending-${to.id}`);

    api.clearAutoTimers();
    await evaluateAndDeliver(h, api);

    const composer = composerText(h, api);
    assert.ok(
      composer.includes(to.title),
      `the ${to.id} wave prompt must be re-scheduled and sent (got: ${composer.slice(0, 120)})`
    );
    assert.strictEqual(api.autoRuntime.stage, `await-${to.id}-user`);
    assert.notStrictEqual(api.autoRuntime.stage, 'idle');
    assert.notStrictEqual(api.autoRuntime.stage, 'complete');
    assert.notStrictEqual(api.autoRuntime.stage, 'paused');
    assert.strictEqual(receiptCount(composer), 1, 'the previous wave must not be resent');
    assert.ok(!composer.includes(`WAVE_ID: ${from.id}`), 'the completed wave must not be resent');
    assert.strictEqual(api.getActiveProfile().profile_id, 'super10');
  });
}

// ---------------------------------------------------------------------------
// 3. Generic await registration stages
// ---------------------------------------------------------------------------

test('A10: autoAwaitStageForKind registers await-${wave}-user for every Super10 wave', () => {
  const { api } = super10Window();
  const waves = api.getActiveProfile().waves;
  for (const w of waves) {
    assert.strictEqual(api.autoAwaitStageForKind(w.id), `await-${w.id}-user`, w.id);
  }
  assert.strictEqual(api.autoAwaitStageForKind('correctness', true), 'await-continuation-user');
  assert.strictEqual(api.autoAwaitStageForKind('nonsense'), '');
});

test('A10: every Super10 await-<wave>-user registration state survives re-evaluation', async () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  seedWaves(h, api, waves, 1, 'super10');

  for (let k = 1; k < waves.length; k += 1) {
    const w = waves[k];
    api.autoRuntime.stage = `await-${w.id}-user`;
    api.autoRuntime.expectedKind = w.id;
    api.autoRuntime.pendingSendReceipt = `pend-${w.id}`;
    api.autoRuntime.pendingSendKind = w.id;
    api.autoRuntime.pendingSendStartedAt = Date.now();
    api.autoRuntime.pendingSendRetries = 0;
    api.autoRuntime.continuationKind = '';
    api.autoRuntime.pausedReason = '';
    api.autoRuntime.pausedFromStage = '';
    api.autoRuntime.waitStartedAt = Date.now();
    assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

    await api.evaluateAutoAudit();

    assert.strictEqual(api.autoRuntime.stage, `await-${w.id}-user`, `${w.id} must stay in its registration flow`);
    assert.notStrictEqual(api.autoRuntime.stage, 'paused', `${w.id} must not pause on the Architecture turn`);
    assert.notStrictEqual(api.autoRuntime.stage, 'idle', `${w.id} must not fall to READY`);
    assert.notStrictEqual(api.autoRuntime.stage, 'wait-architecture', `${w.id} must not restart Architecture`);
    assert.strictEqual(api.autoRuntime.continuationKind, '', `${w.id} must not queue a CONTINUE nudge`);
    assert.strictEqual(api.autoRuntime.profileId, 'super10');
  }

  assert.strictEqual(composerText(h, api), '', 'registration recovery must not send any wave');
});

// ---------------------------------------------------------------------------
// 4. Generic composer-hold recovery
// ---------------------------------------------------------------------------

test('A10: composer HOLD recognizes sending-correctness and recovers through the probe', async () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  seedWaves(h, api, waves, 1, 'super10');

  api.autoRuntime.stage = 'sending-correctness';
  api.autoRuntime.currentWaveId = 'correctness';
  api.autoRuntime.currentWaveIndex = 2;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  // The wave send found the composer not ready and deferred (soft HOLD).
  assert.strictEqual(
    api.deferAutoSendForComposer('correctness', 'The composer is not available while ChatGPT is hydrating.'),
    false
  );
  assert.strictEqual(api.autoComposerHoldKind(), 'correctness');
  assert.strictEqual(api.autoComposerHoldApplicable(), true);

  // The readiness probe fires, the composer is ready again, the hold clears
  // and evaluation continues into the generic sending dispatch.
  await pump(h, 6000);

  const composer = composerText(h, api);
  assert.match(composer, /AUDIT CORRECTNESS/, 'the held Correctness send must resume through the probe');
  assert.strictEqual(api.autoRuntime.stage, 'await-correctness-user');
});

// ---------------------------------------------------------------------------
// 5. Generic durable stale-pause recovery from the pinned profile
// ---------------------------------------------------------------------------

test('A10: stale-PAUSE recovery derives progress from the pinned Super10 profile', () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  // Architecture + Correctness durably COMPLETE; the whole DOM is still
  // virtualized after the reload, so only durable records can prove progress.
  seedWaves(h, api, waves, 2, 'super10', { mountTurns: false });

  api.autoRuntime.stage = 'paused';
  api.autoRuntime.pausedReason = 'Monitor error: simulated crash after the Correctness handoff committed.';
  api.autoRuntime.pausedFromStage = 'await-correctness-user';
  api.autoRuntime.waitStartedAt = 0;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.recoverStalePauseFromConversation(api.getChatGPTTurns()), true);
  assert.strictEqual(api.autoRuntime.stage, 'sending-state');
});

test('A10: stale-PAUSE recovery completes a fully recorded Super10 campaign', () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  seedWaves(h, api, waves, waves.length, 'super10', { mountTurns: false });

  api.autoRuntime.stage = 'paused';
  api.autoRuntime.pausedReason = 'Monitor error: simulated crash after the Red Team handoff committed.';
  api.autoRuntime.waitStartedAt = 0;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.recoverStalePauseFromConversation(api.getChatGPTTurns()), true);
  assert.strictEqual(api.autoRuntime.stage, 'complete');
});

test('A10: stale-PAUSE recovery refuses durable records from a foreign run', () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  // Records for another run in the same conversation must never adopt as
  // progress of the pinned current run.
  for (let i = 0; i < 2; i += 1) {
    const w = waves[i];
    assert.strictEqual(api.writeAuditResult({
      version: 1,
      conversationKey: CONVO,
      runId: 'run-a-foreign-run',
      kind: w.id,
      gateState: 'complete',
      text: handoffFor(api, w, 'super10'),
      completedAt: Date.now()
    }), true, `foreign record for ${w.id}`);
  }
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  api.autoRuntime.stage = 'paused';
  api.autoRuntime.pausedReason = 'Monitor error: simulated crash.';
  api.autoRuntime.waitStartedAt = 0;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.recoverStalePauseFromConversation(api.getChatGPTTurns()), false);
  assert.strictEqual(api.autoRuntime.stage, 'paused');
});

test('A10: stale-PAUSE recovery keeps Quick3 on its own three-wave path', () => {
  const { h, api } = campaignWindow('quick3');
  const waves = api.getActiveProfile().waves;
  seedWaves(h, api, waves, 2, 'quick3', { mountTurns: false });

  api.autoRuntime.stage = 'paused';
  api.autoRuntime.pausedReason = 'Monitor error: simulated crash after Second Wave committed.';
  api.autoRuntime.waitStartedAt = 0;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.recoverStalePauseFromConversation(api.getChatGPTTurns()), true);
  assert.strictEqual(api.autoRuntime.stage, 'sending-performance');
});

test('A10: stale-PAUSE recovery completes Compress after its single wave', () => {
  const { h, api } = campaignWindow('compress');
  const waves = api.getActiveProfile().waves;
  assert.strictEqual(waves.length, 1);
  seedWaves(h, api, waves, 1, 'compress', { mountTurns: false });

  api.autoRuntime.stage = 'paused';
  api.autoRuntime.pausedReason = 'Monitor error: simulated crash after the Compress handoff committed.';
  api.autoRuntime.waitStartedAt = 0;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.recoverStalePauseFromConversation(api.getChatGPTTurns()), true);
  assert.strictEqual(api.autoRuntime.stage, 'complete');
});

// ---------------------------------------------------------------------------
// 6. Profile pinning of the active campaign
// ---------------------------------------------------------------------------

test('A10: an active Super10 campaign stays pinned when the global default profile changes', () => {
  const { h, api } = super10Window();
  const waves = api.getActiveProfile().waves;
  seedWaves(h, api, waves, 1, 'super10');

  // Another manual Chromium window switched the global/default selection.
  api.state.auditProfile = 'quick3';

  assert.strictEqual(api.getActiveProfile().profile_id, 'super10');
  const commit = api.commitTerminalWaveResult('architecture', handoffFor(api, waves[0], 'super10'), 'complete', 'u-architecture');
  assert.strictEqual(commit.ok, true);
  assert.strictEqual(commit.nextWave, 'correctness');
  assert.strictEqual(api.autoRuntime.stage, 'sending-correctness');
  assert.strictEqual(api.getActiveProfile().profile_id, 'super10');
});

// ---------------------------------------------------------------------------
// 9. Quick3 and Compress gates
// ---------------------------------------------------------------------------

test('gates: Quick3 keeps core -> second -> performance and never a fourth wave', () => {
  const { api } = campaignWindow('quick3');
  const waves = api.getActiveProfile().waves;
  assert.strictEqual(waves.map(w => w.id).join(','), 'core,second,performance');

  assert.strictEqual(api.sendingStageWaveKind('sending-second'), 'second');
  assert.strictEqual(api.sendingStageWaveKind('sending-performance'), 'performance');
  assert.strictEqual(api.sendingStageWaveKind('sending-core'), '', 'the first wave is armed by START, not by a sending stage');
  assert.strictEqual(api.sendingStageWaveKind('sending-undefined'), '');
  assert.strictEqual(api.awaitStageWaveKind('await--user'), '');
  assert.strictEqual(api.awaitStageWaveKind('await-nonsense-user'), '');

  const last = waves[waves.length - 1];
  const commit = api.commitTerminalWaveResult(last.id, handoffFor(api, last, 'quick3'), 'complete', `u-${last.id}`);
  assert.strictEqual(commit.campaignComplete, true);
  assert.strictEqual(api.autoRuntime.stage, 'complete');
});

test('gates: Compress stays one wave and never gains a second', () => {
  const { api } = campaignWindow('compress');
  const waves = api.getActiveProfile().waves;
  assert.strictEqual(waves.length, 1);
  assert.strictEqual(waves[0].id, 'compress');

  assert.strictEqual(api.sendingStageWaveKind('sending-compress'), '', 'a single-wave profile has no wave to advance into');

  const commit = api.commitTerminalWaveResult('compress', handoffFor(api, waves[0], 'compress'), 'complete', 'u-compress');
  assert.strictEqual(commit.campaignComplete, true);
  assert.strictEqual(api.autoRuntime.stage, 'complete');
});
