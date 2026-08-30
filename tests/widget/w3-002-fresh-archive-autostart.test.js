'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, composerFixture, runtimeFixture } = require('./helpers');

function cacheCompletedWaves(api, kinds, runId = 'run-complete') {
  api.autoRuntime.runId = runId;
  for (const kind of kinds) {
    assert.strictEqual(api.writeAuditResult({
      version: 1,
      conversationKey: 'c:abc123',
      runId,
      kind,
      gateState: 'complete',
      completedAt: Date.now(),
      text: `${kind} complete evidence`
    }), true);
  }
}

test('W3-002: projectNameFromArtifactFilename parses leading underscores and timestamps cleanly', () => {
  const { api } = setup();
  assert.strictEqual(api.projectNameFromArtifactFilename('_SAICONT_27.08.26-T06-28-02.zip'), 'SAICONT');
  assert.strictEqual(api.projectNameFromArtifactFilename('AUDAPACK_27.08.26-T06-28-01.zip'), 'AUDAPACK');
  assert.strictEqual(api.projectNameFromArtifactFilename('FastPrompter_27.08.26.zip'), 'FastPrompter');
  assert.strictEqual(api.projectNameFromArtifactFilename('_TERMISAI_2026-08-27.zip'), 'TERMISAI');
});

test('W3-002: DONE requires complete durable profile evidence; a new project archive returns READY', () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  api.state.auditProfile = 'quick3';
  
  api.autoRuntime = runtimeFixture({
    stage: 'complete',
    enabled: true,
    profileId: 'quick3',
    runId: 'run-complete'
  });

  assert.strictEqual(api.superCompactAutoLabel(), '0/3', 'stage=complete alone must never claim DONE');
  cacheCompletedWaves(api, ['core', 'second', 'performance']);
  assert.strictEqual(api.superCompactAutoLabel(), 'DONE');

  const generated = h.el('div', { role: 'group', 'aria-label': 'AUDIT_CORE_ABC123.md' });
  generated.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' }));
  form.appendChild(generated);
  assert.strictEqual(api.superCompactAutoLabel(), 'DONE', 'generated audit prompt file is not a new project archive');

  const tile = h.el('div', { role: 'group', 'aria-label': '_SAICONT_27.08.26-T06-28-02.zip' });
  const expand = h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' });
  tile.appendChild(expand);
  form.appendChild(tile);

  assert.strictEqual(api.superCompactAutoLabel(), 'READY');
});

test('W3-002: A10 rejects premature DONE at 1/10 and repairs to wave 2', () => {
  const { api } = setup();
  api.state.auditProfile = 'super10';
  api.autoRuntime = runtimeFixture({
    stage: 'complete',
    enabled: true,
    profileId: 'super10',
    runId: 'run-a10',
    completeAt: Date.now()
  });
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);
  cacheCompletedWaves(api, ['architecture'], 'run-a10');

  assert.strictEqual(api.superCompactAutoLabel(), '1/10');
  assert.deepStrictEqual(
    { done: api.campaignCompletionSnapshot().doneCount, total: api.campaignCompletionSnapshot().totalWaves },
    { done: 1, total: 10 }
  );
  assert.strictEqual(api.reconcilePrematureCampaignCompletion(), true);
  assert.strictEqual(api.autoRuntime.stage, 'sending-correctness');
  assert.strictEqual(api.autoRuntime.currentWaveIndex, 2);
});

test('W3-002: miniStartAuditState enables START for new attachment when stage is complete', () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  api.state.superCompact = true;

  api.autoRuntime = runtimeFixture({
    stage: 'complete',
    enabled: true
  });

  const tile = h.el('div', { role: 'group', 'aria-label': '_SAICONT_27.08.26-T06-28-02.zip' });
  const expand = h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' });
  tile.appendChild(expand);
  form.appendChild(tile);

  const startState = api.miniStartAuditState();
  assert.strictEqual(startState.available, true, 'START must be available for new attachment');
  assert.strictEqual(startState.isNewAudit, true, 'Must flag as new audit');
  const progress = api.autoProgressSnapshot();
  assert.strictEqual(progress.newAuditPending, true);
  assert.strictEqual(progress.activeStep, 0, 'old 3/3 progress must disappear as soon as a new archive is attached');
});

test('W3-002: archive freshness is visible from archive filename timestamp', () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  const name = '_SAICONT_27.08.26-T06-28-02.zip';
  const tile = h.el('div', { role: 'group', 'aria-label': name });
  tile.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' }));
  form.appendChild(tile);

  const modifiedAt = api.archiveTimestampFromFilename(name);
  assert.ok(modifiedAt > 0);
  const freshness = api.composerArchiveFreshness(modifiedAt + (5 * 60 * 1000));
  assert.strictEqual(freshness.name, name);
  assert.strictEqual(freshness.short, 'ZIP 5m');
  assert.strictEqual(freshness.freshness, 'fresh');
  assert.strictEqual(freshness.source, 'filename');
});

test('W3-002: one START waits for delayed Send readiness and submits automatically', async () => {
  const { h, api } = setup();
  const { form, send } = composerFixture(h);
  api.state.superCompact = true;
  api.state.auditProfile = 'quick3';
  api.state.chatgptPromptDelivery = 'text';

  const tile = h.el('div', { role: 'group', 'aria-label': '_SAICONT_27.08.26-T06-28-02.zip' });
  tile.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' }));
  form.appendChild(tile);

  send.disabled = true;
  h.timers.setTimeout(() => { send.disabled = false; }, 700);
  const promise = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  const started = await promise;

  assert.strictEqual(started, true);
  assert.strictEqual(send._clicked, true, 'START must click Send after ChatGPT enables it');
  assert.strictEqual(api.autoRuntime.enabled, true, 'START owns and keeps A3 enabled');
});

test('W3-002: pre-click checkpoint preserves A3 across draft-to-chat hydration', () => {
  const { api } = setup();
  const now = Date.now();
  const handoff = {
    phase: 'armed',
    receipt: 'start-receipt',
    sourceKey: 'draft:one',
    lastKey: 'draft:one',
    destinationKey: '',
    clickAt: now,
    expiresAt: now + 60000
  };

  assert.strictEqual(api.startHandoffOwnsA3Intent(handoff), true);
  assert.strictEqual(api.startHandoffRouteProven(handoff), true);
  assert.strictEqual(api.committedStartOwnsConversationKey(handoff, 'c:new-chat'), true);

  handoff.phase = 'clicking';
  assert.strictEqual(api.startHandoffOwnsA3Intent(handoff), true);
  assert.strictEqual(api.committedStartOwnsConversationKey(handoff, 'c:new-chat'), true);
});

test('W3-002: auditHandoffIntegrity rejects mismatched ticket count', () => {
  const { api } = setup();
  
  const body = `
AUDIT SECOND WAVE
TICKETS: 25
STATUS: SECOND_WAVE: COMPLETE
HANDOFF: IMPLEMENTATION_AGENT

[P1] [W2-001] src/Program.cs startup
EVIDENCE: something
DEFECT: bug
REPAIR: fix
VERIFY: test

[P2] [W2-002] src/Watcher.cs loop
EVIDENCE: something
ISSUE: flaw
OPTIMIZE: solution
GUARDRAIL: check

SECOND_WAVE_DONE_WHEN: All issues resolved.
`;

  const integrity = api.auditHandoffIntegrity('wait-second', body);
  assert.strictEqual(integrity.valid, false);
  assert.strictEqual(integrity.reason, 'ticket-count-mismatch');
  assert.strictEqual(integrity.declared, 25);
  assert.strictEqual(integrity.found, 2);
});
