'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, composerFixture, runtimeFixture, userTurn, addTurns } = require('./helpers');

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

test('T54: manual Send of a prepared canonical Core checkpoints A3 before route hydration', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=12',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=12'
    }
  });
  const { input, send } = composerFixture(h);
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  api.autoRuntime.enabled = true;
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  const handoff = api.beginStartAuditHandoff();
  assert.ok(handoff?.receipt);
  assert.ok(api.armStartAuditHandoffForSend(handoff));
  input.textContent = `AUDIT CORE — wave 1/3 of Quick 3 Waves.\n\nACB_CHAIN_RECEIPT: ${handoff.receipt}`;

  assert.strictEqual(api.preservePreparedStartBeforeManualSend(send), true);
  assert.strictEqual(api.readStartAuditHandoff().phase, 'clicking');
  assert.strictEqual(api.readA3Intent().startTransaction, true);

  h.location.pathname = '/c/manual-start-route';
  h.location.href = 'https://chatgpt.com/c/manual-start-route';
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  assert.strictEqual(api.autoRuntime.enabled, true, 'A3 must stay enabled during manual Send route hydration');
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

test('T58: the exact START receipt is still proven when ChatGPT clamps the user bubble', () => {
  const { h, api } = setup();
  const receipt = 'startcore-clamped-1';
  const full = [
    'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    '',
    'ROLE',
    'You are the auditor.',
    'CAMPAIGN_RUN_ID: run-clamped',
    '',
    `ACB_CHAIN_RECEIPT: ${receipt}`
  ].join('\n');

  const turn = userTurn(h, 'core-clamped', full);
  addTurns(h, [turn]);

  // A long Core prompt is collapsed behind "Show more": innerText stops at the
  // fold, so the receipt on the last line is invisible to the visual read.
  Object.defineProperty(turn, 'innerText', {
    configurable: true,
    get() { return full.split('\n').slice(0, 3).join('\n'); }
  });

  assert.strictEqual(turn.innerText.includes(receipt), false, 'fixture must actually clamp');
  assert.strictEqual(turn.textContent.includes(receipt), true);
  assert.strictEqual(api.classifyAuditTurn(turn), 'core');

  // The receipt proof drives startHandoffCanFollowRoute(); losing it is what
  // disarmed A3 right after a successful send.
  assert.strictEqual(api.userTurnContainsReceipt(turn, receipt), true);
  assert.strictEqual(api.userTurnContainsReceipt(turn, 'startcore-other'), false);
});

test('T62: a chat that visibly holds the START receipt keeps A3 armed no matter what', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/c/6a96148c-e5ec-83eb',
      pathname: '/c/6a96148c-e5ec-83eb',
      search: ''
    }
  });
  composerFixture(h);
  const receipt = 'startcore-invariant-1';
  const full = [
    'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    '',
    'CAMPAIGN_RUN_ID: run-invariant',
    `ACB_CHAIN_RECEIPT: ${receipt}`
  ].join('\n');
  const turn = userTurn(h, 'core-invariant', full);
  addTurns(h, [turn]);

  // The bubble is clamped, exactly as ChatGPT renders a long Core prompt.
  Object.defineProperty(turn, 'innerText', {
    configurable: true,
    get() { return full.split('\n').slice(0, 2).join('\n'); }
  });

  // A live START handoff still owns the A3 intent for this tab...
  const now = Date.now();
  h.sessionStore.set('ai_chatbuttons_auto_start_handoff_v1', JSON.stringify({
    version: 1,
    tabId: h.sessionStore.get('ai_chatbuttons_auto_tab_id_v1'),
    sourceKey: 'draft:x:y',
    lastKey: 'draft:x:y',
    destinationKey: '',
    phase: 'sent',
    startedAt: now,
    sentAt: now,
    armedAt: now,
    receipt,
    expiresAt: now + 300000,
    runtime: null
  }));

  // ...but the runtime for this route came back disabled.
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  api.autoRuntime.enabled = false;

  assert.strictEqual(api.readStartAuditHandoff()?.receipt, receipt, 'handoff fixture must load');
  assert.strictEqual(api.enforceStartReceiptA3Ownership(), true);
  assert.strictEqual(api.autoRuntime.enabled, true);
});

test('T62: the receipt invariant never arms A3 in an unrelated chat', () => {
  const { h, api } = setup({
    location: { href: 'https://chatgpt.com/c/someone-elses', pathname: '/c/someone-elses', search: '' }
  });
  composerFixture(h);
  addTurns(h, [userTurn(h, 'other-1', 'just a normal question about cats')]);

  const now = Date.now();
  h.sessionStore.set('ai_chatbuttons_auto_start_handoff_v1', JSON.stringify({
    version: 1,
    tabId: h.sessionStore.get('ai_chatbuttons_auto_tab_id_v1'),
    sourceKey: 'draft:x:y',
    lastKey: 'draft:x:y',
    destinationKey: '',
    phase: 'sent',
    startedAt: now,
    sentAt: now,
    armedAt: now,
    receipt: 'startcore-not-here',
    expiresAt: now + 300000,
    runtime: null
  }));

  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  api.autoRuntime.enabled = false;

  assert.strictEqual(api.enforceStartReceiptA3Ownership(), false);
  assert.strictEqual(api.autoRuntime.enabled, false);

  // The disarm is still recorded so it can never be invisible again.
  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'a3_disarmed_with_live_start'), JSON.stringify(log));
});

test('T63: A3 survives a lost runtime even after the START handoff is cleared', () => {
  const { h, api } = setup({
    location: { href: 'https://chatgpt.com/c/6a96148c-e5ec', pathname: '/c/6a96148c-e5ec', search: '' }
  });
  composerFixture(h);
  const full = [
    'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    '',
    'CAMPAIGN_RUN_ID: run-adopted',
    'ACB_CHAIN_RECEIPT: startcore-adopted-1'
  ].join('\n');
  const turn = userTurn(h, 'core-adopted', full);
  addTurns(h, [turn]);
  Object.defineProperty(turn, 'innerText', {
    configurable: true,
    get() { return full.split('\n').slice(0, 2).join('\n'); }
  });

  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  api.autoRuntime.conversationKey = 'c:6a96148c-e5ec';

  // No handoff at all: it is cleared as soon as the Core turn is adopted.
  assert.strictEqual(api.readStartAuditHandoff(), null);
  assert.ok(api.machineAuthoredAuditTurn(), 'the machine receipt must be recognised');
  assert.strictEqual(api.enforceStartReceiptA3Ownership('c:6a96148c-e5ec'), true);
  assert.strictEqual(api.autoRuntime.enabled, true);

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'a3_reasserted_from_machine_receipt'), JSON.stringify(log));
});

test('T63: an operator who unchecks A3 by hand is never overridden', () => {
  const { h, api } = setup({
    location: { href: 'https://chatgpt.com/c/manual-off', pathname: '/c/manual-off', search: '' }
  });
  composerFixture(h);
  const full = [
    'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    'ACB_CHAIN_RECEIPT: startcore-manual-off'
  ].join('\n');
  addTurns(h, [userTurn(h, 'core-manual', full)]);

  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  // Unchecking A3 persists a runtime with enabled=false. That record is a
  // decision, not lost state, and must survive every later render.
  api.setAutoAuditEnabled(false);
  assert.strictEqual(api.autoRuntime.enabled, false);

  assert.strictEqual(api.enforceStartReceiptA3Ownership('c:manual-off'), false);
  assert.strictEqual(api.autoRuntime.enabled, false);
});
