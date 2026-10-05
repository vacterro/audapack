'use strict';

// T-261: the live A3 split brain. The Core Send SUCCEEDED, ChatGPT started
// generating, and AUDAPACK never adopted its own sent Core into the Auto3
// runtime -- so the widget sat at "enabled / stage idle / Armed. Waiting for a
// NEW AUDIT CORE." while the Bridge lane read AUDITING.
//
// Three different facts were collapsed into one boolean:
//   1. SUBMISSION_ATTEMPTED
//   2. PAYLOAD_LEFT_COMPOSER / TRANSPORT_ACCEPTED
//   3. MACHINE USER TURN REGISTERED / AUDIT LINEAGE ADOPTED
// Generation only ever proved (2). START treated (2) as (3) and moved the
// Bridge straight to AUDITING.
//
// This file is the exact reproduction and the exact contract. Generation is
// kept for liveness. It is never lineage identity.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, mainEl, userTurn, addTurns, composerFixture, runtimeFixture
} = require('./helpers');

const RECEIPT = 'startcore-abc123';
const RUN_ID = 'run-w7-012';
const ARCHIVE = 'TERMISAI_01.09.26-T00-00-00.zip';

const CORE_BODY = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  `CAMPAIGN_RUN_ID: ${RUN_ID}`,
  'PROJECT_NAME: TERMINSAI',
  'Instructions follow. '.repeat(400),
  `ACB_CHAIN_RECEIPT: ${RECEIPT}`
].join('\n');

// A real current-build composer: the form carries no data-type, the editor is
// the ProseMirror "Ask ChatGPT" box, and the trailing action slot is a plain
// div. Nothing here is a widget convenience.
function currentComposer(h) {
  const main = mainEl(h);
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  form.appendChild(input);
  const slot = h.el('div', { class: 'flex items-center' });
  form.appendChild(slot);
  main.appendChild(form);
  return { form, input, slot };
}

function mountGeneratingStop(h, form) {
  const stop = h.el('button', { type: 'button', 'aria-label': 'Stop' });
  form.appendChild(stop);
  h.mutate(form);
  return stop;
}

function userTurnCount(h) {
  return mainEl(h).querySelectorAll('[data-message-author-role="user"]').length;
}

// ChatGPT clamps a very long user bubble behind "Show more". The mounted text
// keeps only the visible head, so neither the header NOR the trailing receipt is
// readable, and raw textContent is not guaranteed to hold the hidden remainder.
function clampedCoreTurn(h, id) {
  const turn = userTurn(h, id, 'Instructions follow. Instructions follow.');
  const more = h.el('button', { 'aria-label': 'Show more' });
  turn.appendChild(more);
  return turn;
}

// ---------------------------------------------------------------------------
// 1 + INDEPENDENT CURRENT RED CONTROL
// ---------------------------------------------------------------------------

test('W7-012 RED CONTROL: generation alone is not Core turn registration', async () => {
  const { h, api } = setup();
  const composer = currentComposer(h);
  composer.input.textContent = CORE_BODY;
  mountGeneratingStop(h, composer.form);
  h.mutate(composer.form);

  assert.strictEqual(api.chatGPTIsGenerating(), true, 'the fixture must really be generating');
  assert.strictEqual(userTurnCount(h), 0, 'no user turn exists');
  assert.strictEqual(
    api.chatGPTComposerReceiptState(RECEIPT), 'present-with-receipt',
    'the Core and its receipt are still in the composer'
  );

  // The reproduced 0.0.90 truth. It is a TRANSPORT verdict and is kept as a
  // control so this fixture can never drift into proving nothing.
  assert.strictEqual(
    await api.chatGPTSendAccepted(RECEIPT, CORE_BODY, 900, true), true,
    'this exact shape is the current-red control: accepted from generation alone'
  );

  const outcome = await api.chatGPTSendOutcome(RECEIPT, CORE_BODY, 900, true);
  assert.strictEqual(outcome.userTurnRegistered, false,
    'generation does not register a user turn');
  assert.strictEqual(outcome.userTurnId, '', 'and there is no id to bind');
  assert.strictEqual(outcome.state, 'SUBMITTED_UNREGISTERED');
  assert.strictEqual(outcome.receiptVisible, true);
  assert.notStrictEqual(outcome.state, 'REGISTERED');
});

test('W7-012: a real registered Core turn is REGISTERED with its exact id', async () => {
  const { h, api } = setup();
  const composer = currentComposer(h);
  composer.input.textContent = CORE_BODY;
  mountGeneratingStop(h, composer.form);
  const turn = userTurn(h, 'turn-core-1', CORE_BODY);
  addTurns(h, [turn]);

  const outcome = await api.chatGPTSendOutcome(RECEIPT, CORE_BODY, 900, true);
  assert.strictEqual(outcome.userTurnRegistered, true);
  assert.strictEqual(outcome.state, 'REGISTERED');
  assert.ok(outcome.userTurnId, 'the exact turn id is reported for binding');
});

test('W7-012: a refused send is REJECTED, never SUBMITTED', async () => {
  const { h, api } = setup();
  const composer = currentComposer(h);
  composer.input.textContent = CORE_BODY;
  h.mutate(composer.form);

  // The bounded acceptance window has to be walked on the harness clock.
  const promise = api.chatGPTSendOutcome(RECEIPT, CORE_BODY, 400, true);
  await h.settle();
  const outcome = await promise;

  assert.strictEqual(outcome.state, 'REJECTED');
  assert.strictEqual(outcome.submitted, false);
  assert.strictEqual(outcome.userTurnRegistered, false);
  assert.strictEqual(outcome.receiptVisible, true, 'the Core never left the composer');
});

// ---------------------------------------------------------------------------
// TRANSACTION BOUNDARY ADOPTION
// ---------------------------------------------------------------------------

// A plain in-memory committed handoff, for the pure boundary-rule assertions.
function committedHandoff(api, overrides = {}) {
  const handoff = {
    version: 1,
    tabId: 'tab-1',
    sourceKey: 'draft:new',
    lastKey: 'draft:new',
    phase: 'sent',
    startedAt: Date.now() - 5000,
    sentAt: Date.now(),
    armedAt: Date.now() - 4000,
    destinationKey: '',
    receipt: RECEIPT,
    expiresAt: Date.now() + 600000,
    runtime: null,
    expectedKind: 'core',
    // The marker the real pre-send hook stamps. An empty id with a zero count is
    // still a baseline: it means "no user turn existed before the Send".
    preSendAt: Date.now() - 1000,
    preSendLatestUserTurnId: 'turn-prior-1',
    preSendUserTurnCount: 1,
    ...overrides
  };
  return handoff;
}

// The REAL production sequence: arm the handoff at the pre-send hook (which is
// where the baseline and the transaction identity are captured), then mark the
// irreversible click. Nothing here is hand-written state.
function commitRealStartHandoff(api) {
  const handoff = api.beginStartAuditHandoff();
  api.armStartAuditHandoffForSend(handoff);
  return api.markStartAuditHandoffSent(handoff) || api.readStartAuditHandoff();
}

function runtimeWithIdentity(api, overrides = {}) {
  const runtime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
  runtime.runId = RUN_ID;
  runtime.projectName = 'TERMINSAI';
  runtime.projectNameSource = 'artifact';
  runtime.projectId = 'termisai';
  runtime.archiveName = ARCHIVE;
  runtime.archiveSize = 4242;
  runtime.archiveModifiedAt = Date.now();
  runtime.archiveTimestampSource = 'tile';
  return { ...runtime, ...overrides };
}

test('W7-012: a LONG CLAMPED Core is adopted by transaction boundary', () => {
  const { h, api } = setup();
  addTurns(h, [userTurn(h, 'turn-prior-1', 'earlier human question')]);
  const handoff = committedHandoff(api);
  addTurns(h, [clampedCoreTurn(h, 'turn-core-clamped')]);

  // The DOM carries no canonical header and no receipt. The transaction knows
  // the pre-send baseline, so ownership does not depend on re-parsing the giant
  // prompt back out of ChatGPT.
  const result = api.startAuditCoreTransactionTurn(handoff, api.getChatGPTTurns());
  assert.strictEqual(result.adopted, 'boundary', result.reason);
  assert.strictEqual(result.id, 'turn-core-clamped');
});

test('W7-012: a turn BEFORE the pre-send baseline is never the Core', () => {
  const { h, api } = setup();
  addTurns(h, [userTurn(h, 'turn-prior-1', 'earlier human question')]);
  const handoff = committedHandoff(api);
  const result = api.startAuditCoreTransactionTurn(handoff, api.getChatGPTTurns());
  assert.strictEqual(result.adopted, 'none', result.reason);
});

test('W7-012: a COMPETING human turn after the baseline fails closed', () => {
  const { h, api } = setup();
  addTurns(h, [userTurn(h, 'turn-prior-1', 'earlier human question')]);
  const handoff = committedHandoff(api);
  addTurns(h, [
    userTurn(h, 'turn-human-1', 'actually, also check the docs'),
    userTurn(h, 'turn-human-2', 'and the CI config')
  ]);

  const result = api.startAuditCoreTransactionTurn(handoff, api.getChatGPTTurns());
  assert.strictEqual(result.adopted, 'none', 'two competing turns are ambiguous');
  assert.strictEqual(result.competing, true, 'the ambiguity is named, not guessed through');
});

test('W7-012: an exact receipt turn still wins over the boundary rule', () => {
  const { h, api } = setup();
  const handoff = committedHandoff(api);
  addTurns(h, [
    userTurn(h, 'turn-prior-1', 'earlier question'),
    userTurn(h, 'turn-core-exact', CORE_BODY)
  ]);
  const result = api.startAuditCoreTransactionTurn(handoff, api.getChatGPTTurns());
  assert.strictEqual(result.adopted, 'exact');
  assert.strictEqual(result.id, 'turn-core-exact');
});

// ---------------------------------------------------------------------------
// BRIDGE CONTRACT
// ---------------------------------------------------------------------------

test('W7-012 BRIDGE: AUDITING is refused while the runtime owns no active wave', () => {
  const { api } = setup();
  api.autoRuntime = runtimeFixture({ enabled: true, stage: 'idle', runId: RUN_ID });
  assert.strictEqual(api.runtimeOwnsCurrentAuditWave(), false,
    'stage idle is not wave ownership -- this is the live split brain');

  api.autoRuntime.stage = 'await-core-user';
  assert.strictEqual(api.runtimeOwnsCurrentAuditWave(), false,
    'a submitted-but-unregistered Core is not adoption');

  api.autoRuntime.stage = 'wait-core';
  assert.strictEqual(api.runtimeOwnsCurrentAuditWave(), true,
    'a bound wait stage IS concrete current wave ownership');
});

test('W7-012: a committed send with no turn enters await-core-user, never idle', () => {
  const { h, api } = setup();
  api.state.auditProfile = 'quick3';
  api.autoRuntime = runtimeFixture({ enabled: true, stage: 'idle', runId: RUN_ID });
  // The prior conversation state, then the real pre-send -> click sequence.
  addTurns(h, [userTurn(h, 'turn-prior-1', 'earlier question')]);
  const handoff = commitRealStartHandoff(api);
  assert.ok(handoff.preSendUserTurnCount >= 1, 'the baseline was captured before the Send');

  const result = api.recoverSentStartCore({ source: 'unit' });
  assert.strictEqual(typeof result, 'object', 'recovery reports adoption, not a bare boolean');
  assert.strictEqual(result.adopted, false, 'nothing to adopt yet');
  assert.notStrictEqual(api.autoRuntime.stage, 'idle',
    'the live defect: enabled + idle + "Waiting for a NEW AUDIT CORE"');
  assert.strictEqual(api.autoRuntime.stage, 'await-core-user');
  assert.strictEqual(api.autoRuntime.expectedKind, 'core');
  assert.strictEqual(api.readStartAuditHandoff().receipt, handoff.receipt,
    'the exact receipt is preserved: no second Core can be prepared');
});

// ---------------------------------------------------------------------------
// ROUTE MIGRATION / ARCHIVE IDENTITY
// ---------------------------------------------------------------------------

test('W7-012 ROUTE MIGRATION: a blank destination never erases transaction identity', () => {
  const { api } = setup();
  const source = runtimeWithIdentity(api);
  const poor = api.emptyAutoRuntime({ enabled: false, profileId: 'quick3' });
  poor.conversationKey = 'c:destination';

  const merged = api.mergeStartHandoffRuntime(source, poor, 'c:destination');
  for (const field of ['runId', 'projectName', 'projectNameSource', 'projectId',
    'archiveName', 'archiveSize', 'archiveModifiedAt', 'archiveTimestampSource',
    'profileId', 'enabled']) {
    assert.ok(
      merged[field] !== '' && merged[field] !== 0 && merged[field] !== undefined && merged[field] !== false,
      `route migration preserved ${field} (got ${JSON.stringify(merged[field])})`
    );
  }
  assert.strictEqual(merged.runId, RUN_ID);
  assert.strictEqual(merged.archiveName, ARCHIVE);
  assert.strictEqual(merged.enabled, true);
  assert.strictEqual(merged.conversationKey, 'c:destination');
});

test('W7-012: a richer destination runtime is never degraded by a poorer source', () => {
  const { api } = setup();
  const source = runtimeWithIdentity(api);
  const rich = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
  rich.runId = 'run-already-bound';
  rich.archiveName = 'OTHER_01.09.26-T00-00-00.zip';
  rich.conversationKey = 'c:destination';

  const merged = api.mergeStartHandoffRuntime(source, rich, 'c:destination');
  assert.strictEqual(merged.runId, 'run-already-bound');
  assert.strictEqual(merged.archiveName, 'OTHER_01.09.26-T00-00-00.zip');
});

test('W7-012 ARCHIVE AFTER SEND: the durable transaction keeps the archive visible', () => {
  const { api } = setup();
  // The composer tile is gone: submission naturally detaches the ZIP.
  api.autoRuntime = runtimeFixture({
    enabled: true,
    stage: 'await-core-user',
    runId: RUN_ID,
    archiveName: ARCHIVE,
    archiveSize: 4242,
    archiveModifiedAt: Date.now(),
    archiveTimestampSource: 'tile'
  });

  const fresh = api.currentAuditArchiveFreshness();
  assert.strictEqual(fresh.present, true,
    'after Send the archive must come from the durable runtime transaction');
  assert.strictEqual(fresh.name, ARCHIVE);
});

// ---------------------------------------------------------------------------
// COMPACT / FULL CONSISTENCY
// ---------------------------------------------------------------------------

test('W7-012: an active Core generation never renders BUSY over "Waiting for a NEW AUDIT CORE"', () => {
  const { h, api } = setup();
  api.state.auditProfile = 'quick3';
  const composer = currentComposer(h);
  mountGeneratingStop(h, composer.form);

  // The live state, verbatim: enabled, stage idle, ChatGPT generating.
  api.autoRuntime = runtimeFixture({ enabled: true, stage: 'idle', runId: RUN_ID });
  const idleText = api.autoStageSummary().text;
  assert.ok(idleText.includes('Waiting for a NEW'), idleText);

  // The same tab one boundary later.
  api.autoRuntime.stage = 'await-core-user';
  api.autoRuntime.expectedKind = 'core';
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
  const pendingText = api.autoStageSummary().text;
  assert.ok(!/Waiting for a NEW AUDIT CORE/.test(pendingText), pendingText);
  assert.ok(/register/i.test(pendingText), pendingText);

  api.autoRuntime.stage = 'wait-core';
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
  const runningText = api.autoStageSummary().text;
  assert.ok(/Core is running/.test(runningText), runningText);
});

// ---------------------------------------------------------------------------
// THE LIVE REPRODUCTION, END TO END THROUGH THE MANAGED WORKER
// ---------------------------------------------------------------------------

test('W7-012 LIVE: an adopted managed START audits; the same send that registers no turn stays STARTED', async () => {
  const run = async registerTurn => {
    const { h, api } = setup({
      location: {
        href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=11',
        pathname: '/',
        search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=11'
      }
    });
    const { form, input, send } = composerFixture(h);
    api.state.bridgeEnabled = true;
    api.state.auditProfile = 'quick3';
    api.state.chatgptPromptDelivery = 'text';

    // Real ChatGPT: the click consumes the composer, the ZIP tile detaches, and
    // generation starts. The user turn hydrates afterwards -- and in the live
    // defect it never did.
    send.addEventListener('click', () => {
      const text = String(input.textContent || '');
      if (!text.trim()) return;
      input.textContent = '';
      for (const tile of Array.from(form.children)) {
        if (tile.getAttribute && tile.getAttribute('role') === 'group') tile.remove();
      }
      form.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop' }));
      h.mutate(form);
      if (registerTurn) {
        const turn = userTurn(h, 'accepted-1', text);
        mainEl(h).appendChild(turn);
        h.mutate(form);
      }
    });

    const tile = h.el('div', { role: 'group', 'aria-label': ARCHIVE });
    tile.appendChild(h.el('button', { 'aria-label': 'Remove file' }));
    form.appendChild(tile);

    const transitions = [];
    const archiveFile = { name: ARCHIVE, size: 4242 };
    const promise = api.browserWorkerConsume({
      dispatch_id: 'dsp-fedcba9876543210',
      worker_id: 'audapack-managed-1-11',
      lease_id: 'lease-live',
      project_id: 'termisai',
      project_name: 'TERMINSAI',
      campaign_run_id: '',
      archive_filename: archiveFile.name,
      archive_size: archiveFile.size
    }, {
      transition: async state => { transitions.push(state); return { ok: true }; },
      fetchArtifact: async () => ({ ok: true, file: archiveFile }),
      uploadInput: () => input,
      composerRoot: () => form,
      injectFiles: () => true,
      waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: [ARCHIVE] })
    });
    await h.settle();
    const ok = await promise;
    return { ok, transitions, stage: api.autoRuntime.stage, api, h };
  };

  const audited = await run(true);
  assert.strictEqual(audited.ok, true);
  assert.ok(audited.transitions.includes('AUDITING'),
    `a registered Core audits: ${JSON.stringify(audited.transitions)}`);
  assert.strictEqual(audited.stage, 'wait-core');
  assert.strictEqual(audited.api.superCompactAutoLabel(), 'CORE');

  // The live defect, exactly: the send succeeded, ChatGPT is generating, and no
  // user turn exists. Bridge AUDITING over this runtime was the split brain.
  const stranded = await run(false);
  assert.strictEqual(stranded.ok, true, 'the lane itself is fine');
  assert.ok(!stranded.transitions.includes('AUDITING'),
    `no adoption means no AUDITING: ${JSON.stringify(stranded.transitions)}`);
  assert.ok(stranded.transitions.includes('STARTED'), 'it is STARTED, pending registration');
  assert.notStrictEqual(stranded.stage, 'idle',
    'enabled + idle + generating is the split brain in one line');
  assert.strictEqual(stranded.stage, 'await-core-user');

  const panel = stranded.api.autoStageSummary().text;
  assert.ok(!/Waiting for a NEW AUDIT CORE/.test(panel), panel);
  assert.ok(/register/i.test(panel), panel);
  assert.strictEqual(stranded.api.superCompactAutoLabel(), 'CORE');

  // The archive tile is gone from the composer, exactly as after a real Send.
  const fresh = stranded.api.currentAuditArchiveFreshness();
  assert.strictEqual(fresh.present, true, 'archive identity comes from the durable transaction');
  assert.strictEqual(fresh.name, ARCHIVE);
});
