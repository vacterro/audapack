'use strict';

// SRC-098: managed A3 ownership reconciliation.
//
// The production defect this suite exists for is a CROSS-LAYER split brain.
// The Bridge Project Room showed a live managed lane (FastPrompter, AUDIT 0/3,
// worker Chrome #1) while the browser widget for that exact window rendered
// A3 OFF and the compact label CHAT, with the canonical AUDIT CORE user turn
// and the project ZIP visibly present in the conversation.
//
// A valid non-terminal managed dispatch outranks a stale local
// `autoRuntime.enabled === false`. The widget must never report CHAT while the
// Bridge still considers that worker/conversation part of an active A3 campaign.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, assistantTurn, addTurns } = require('./helpers');
const { createHarness } = require('./harness');

const CONVERSATION_KEY = 'c:fastprompter-run';

function chatLocation(conversation = CONVERSATION_KEY) {
  const path = `/c/${String(conversation).replace(/^c:/, '')}`;
  return { href: `https://chatgpt.com${path}`, pathname: path, search: '' };
}

// The exact production fixture: same worker, same dispatch, same campaign,
// same conversation, canonical Core visible, assistant stopped mid-response,
// and a NON-BLANK persisted runtime that came back disabled.
function splitBrainHarness(overrides = {}) {
  // A distinct worker identity has to exist BEFORE load, because that is when
  // the widget adopts (or mints) its tab id. Seeding it afterwards would leave
  // two "workers" sharing one id, which is not a multi-worker test at all.
  const h = createHarness({ location: chatLocation(overrides.pathname) });
  if (overrides.tabId) h.sessionStore.set('ai_chatbuttons_auto_tab_id_v1', String(overrides.tabId));
  // The managed stop is a real Bridge call; bridgeRequest() refuses to dial
  // without a token, exactly as it does in production.
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  const api = h.load();
  if (h.loadError) throw h.loadError;
  api.state.bridgeEnabled = overrides.bridgeEnabled !== false;

  api.browserWorkerLease = overrides.noLease ? null : {
    dispatch_id: overrides.dispatchId || 'dsp-fastprompter01',
    worker_id: String(h.sessionStore.get('ai_chatbuttons_auto_tab_id_v1') || 'audapack-managed-1-1-abc'),
    lease_id: 'lease-fastprompter01',
    project_id: 'fastprompter',
    project_name: 'FastPrompter',
    campaign_run_id: 'acb-run-fast-1',
    start_receipt: 'startcore-fast-1',
    profile: 'quick3',
    ...(overrides.lease || {})
  };
  api.browserWorkerDispatchState = overrides.dispatchState || 'AUDITING';

  // A dispatched worker runs the profile the Bridge named, not whatever this
  // window happened to be set to. Applied before the runtime exists, exactly
  // as the real claim path does.
  if (api.browserWorkerLease) api.browserWorkerApplyDispatchedProfile('quick3');

  // Non-blank on purpose: this is not the "route lost everything" case that
  // runtimeIsBlankDisabled() already repairs. The runtime carries the Core
  // turn, the run id and the project, and is disabled all the same.
  api.autoRuntime = {
    ...api.emptyAutoRuntime({ enabled: false }),
    version: 5,
    conversationKey: overrides.conversationKey || CONVERSATION_KEY,
    stage: overrides.stage || 'wait-core',
    runId: 'acb-run-fast-1',
    coreUserId: 'core-1',
    expectedKind: 'core',
    projectName: 'FastPrompter',
    projectId: 'fastprompter',
    startedAt: Date.now() - 60000,
    waitStartedAt: Date.now() - 30000,
    ...(overrides.runtime || {})
  };

  addTurns(h, [
    userTurn(h, 'core-1', 'AUDIT CORE — full sweep. ACB_CHAIN_RECEIPT: startcore-fast-1'),
    assistantTurn(h, 'asst-1', el => { el._text = 'Stopped thinking'; })
  ]);
  if (overrides.finishedCampaign) {
    // A finished 3/3 campaign on screen: every wave asked AND answered.
    addTurns(h, [
      userTurn(h, 'w2', 'AUDIT SECOND WAVE — narrow. ACB_CHAIN_RECEIPT: startcore-fast-1'),
      assistantTurn(h, 'a2', el => { el._text = '## FINDINGS\n\ndone'; }),
      userTurn(h, 'w3', 'AUDIT PERFORMANCE — measure. ACB_CHAIN_RECEIPT: startcore-fast-1'),
      assistantTurn(h, 'a3', el => { el._text = '## FINDINGS\n\ndone'; })
    ]);
  }

  return { h, api };
}

test('SRC-098: an active managed A3 dispatch never renders CHAT', () => {
  // Milestone L, the production screenshot verbatim.
  const { api } = splitBrainHarness();
  const label = api.superCompactAutoLabel();
  assert.notStrictEqual(
    label,
    'CHAT',
    `a managed worker holding dispatch ${api.browserWorkerLease.dispatch_id} with a visible ` +
    'canonical Core turn rendered CHAT; a live Bridge campaign outranks a stale local enabled bit'
  );
});

test('SRC-098: a managed dispatch re-arms a NON-BLANK disabled runtime', () => {
  // Milestone F. The old gate repaired only a "blank" disabled runtime or a
  // runtime that was never persisted at all. This runtime holds the Core turn,
  // the run id and the project, and is disabled all the same.
  const { api } = splitBrainHarness();
  const repaired = api.reassertA3FromMachineReceipt(CONVERSATION_KEY);
  assert.strictEqual(repaired, true, 'a non-blank disabled runtime under a live managed dispatch was not repaired');
  assert.strictEqual(api.autoRuntime.enabled, true, 'A3 stayed disabled after repair');
});

test('SRC-098: the managed worker poll re-arms a mid-wave disabled engine', () => {
  // browserWorkerRecoverIdleEngine() is the per-poll owner of managed A3
  // ownership. It gated on stage idle|complete only, so a runtime parked at
  // wait-core (Core sent, response not complete) was never re-armed.
  const { api } = splitBrainHarness();
  const repaired = api.browserWorkerRecoverIdleEngine();
  assert.strictEqual(repaired, true, 'the managed poll never re-armed a mid-wave disabled engine');
  assert.strictEqual(api.autoRuntime.enabled, true, 'A3 stayed disabled after the managed poll recovery');
});

test('SRC-098: managed ownership is reported as one canonical snapshot', () => {
  const { api } = splitBrainHarness();
  const snapshot = api.managedA3OwnershipSnapshot();
  assert.ok(snapshot, 'managedA3OwnershipSnapshot() must exist');
  assert.strictEqual(snapshot.managed, true);
  assert.strictEqual(snapshot.ownsDispatch, true);
  assert.strictEqual(snapshot.campaignActive, true);
  assert.strictEqual(snapshot.conversationMatches, true);
  assert.strictEqual(snapshot.explicitOperatorStop, false);
  assert.strictEqual(
    snapshot.shouldOwnA3,
    true,
    `a live managed dispatch must own A3, got reason=${snapshot.reason}`
  );
});

// ---------------------------------------------------------------------------
// Milestone K: the matrix. Every case names the authority that decides it.
// ---------------------------------------------------------------------------

test('K1: active managed dispatch + DISABLED BLANK runtime re-arms', () => {
  const { api } = splitBrainHarness({ runtime: { coreUserId: '', expectedKind: '', stage: 'idle' } });
  assert.strictEqual(api.recoverManagedA3Ownership(), true);
  assert.strictEqual(api.autoRuntime.enabled, true);
});

test('K2: active managed dispatch + DISABLED NON-BLANK runtime re-arms', () => {
  // The old "non-blank means the human meant it" assumption is exactly the bug.
  const { api } = splitBrainHarness();
  assert.strictEqual(api.recoverManagedA3Ownership(), true);
  assert.strictEqual(api.autoRuntime.enabled, true);
});

test('K3: active managed dispatch + EXPLICIT user off is never auto re-armed', () => {
  const { api } = splitBrainHarness();
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });

  assert.strictEqual(api.autoRuntime.a3OperatorExplicitOff, true, 'the operator intent must be durable');
  assert.strictEqual(api.managedA3OwnershipSnapshot().explicitOperatorStop, true);
  assert.strictEqual(api.recoverManagedA3Ownership(), false, 'managed recovery overrode an operator stop');
  assert.strictEqual(api.autoRuntime.enabled, false, 'A3 came back on against the operator');
  // Milestone C supersedes the original CHAT expectation here. Between the
  // operator's click and the Bridge's ACK the campaign has NOT disappeared --
  // it is still a live lane until the Bridge says so -- so ATTN is the honest
  // label and ordinary CHAT would be the split brain in its other direction.
  assert.strictEqual(api.superCompactAutoLabel(), 'ATTN');
});

// ---------------------------------------------------------------------------
// K3a-K3e: the CROSS-LAYER half of the same scenario. Local intent was already
// respected in K3; the Bridge was not. An explicit operator OFF on a managed
// worker left the Bridge holding a live AUDIT 0/3 lane for a campaign the
// widget had stopped running -- a second split brain, in the opposite
// direction, and the one that pins a worker out of the pool forever.
// ---------------------------------------------------------------------------

// The canonical operator stop. The Bridge already has this exact semantic:
// `abandon_job()` moves a post-start dispatch to terminal FAILED with
// operator_abandoned, clears the lease so the lane is not pinned, keeps the
// record in Audit Runs history, and never re-leases or re-STARTs. CANCELLED is
// deliberately NOT the answer: it asserts no Core was sent and is illegal from
// AUDITING.
const STOP_PATH = /\/v1\/browser\/jobs\/dsp-fastprompter01\/abandon$/;
const RUNTIME_KEY = `ai_chatbuttons_auto_audit_runtime_v4:${CONVERSATION_KEY}`;
const LEASE_SESSION_KEY = 'audapack_browser_worker_lease_v1';

function stopRequests(h) {
  return h.httpRequests.filter(request => request.method === 'POST' && STOP_PATH.test(String(request.url || '')));
}

test('K3a: an explicit operator OFF on a managed dispatch issues ONE canonical Bridge stop', async () => {
  const { h, api } = splitBrainHarness();
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();

  const stops = stopRequests(h);
  assert.strictEqual(
    stops.length,
    1,
    `explicit operator OFF must retire the managed dispatch through exactly one canonical ` +
    `Bridge transition, saw ${stops.length} (${JSON.stringify(h.httpRequests.map(r => r.url))})`
  );
  assert.strictEqual(
    String(stops[0].body || stops[0].data || ''),
    stops[0].body || stops[0].data,
    'the stop body must be recorded for inspection'
  );
});

test('K3b: the stop ACK makes the dispatch terminal, so the lane is no longer AUDITING', async () => {
  const { h, api } = splitBrainHarness();
  h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 200, responseText: JSON.stringify({ ok: true, dispatch_id: 'dsp-fastprompter01', state: 'FAILED', error: 'operator stopped A3' }) }
    : { status: 200, responseText: JSON.stringify({ ok: true }) });

  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();

  assert.strictEqual(
    api.browserWorkerDispatchState,
    'FAILED',
    `the Bridge ACK must retire the dispatch locally, got ${api.browserWorkerDispatchState}`
  );
  assert.strictEqual(api.browserWorkerLease, null, 'a retired dispatch must not pin this window out of the worker pool');
  assert.strictEqual(api.managedA3OwnershipSnapshot().campaignActive, false, 'the campaign still reads as active after the stop');
  assert.strictEqual(api.autoRuntime.a3OperatorStopPending, null, 'the pending stop was not cleared by its ACK');
  assert.strictEqual(api.autoRuntime.enabled, false, 'the stop ACK must never re-arm A3');
});

test('K3c: a failed stop delivery keeps A3 OFF and says ATTN, never ordinary CHAT', async () => {
  const { h, api } = splitBrainHarness();
  h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 500, responseText: JSON.stringify({ ok: false, error: { code: 'bridge_down', message: 'no' } }) }
    : { status: 200, responseText: JSON.stringify({ ok: true }) });

  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();

  assert.strictEqual(api.autoRuntime.enabled, false, 'local operator intent is authoritative even when delivery fails');
  assert.strictEqual(api.autoRuntime.a3OperatorExplicitOff, true, 'the operator intent must survive a failed stop');
  assert.ok(
    api.autoRuntime.a3OperatorStopPending,
    'a stop that was never acknowledged must stay durable so a reload resumes it'
  );
  assert.strictEqual(
    String(api.autoRuntime.a3OperatorStopPending.reason),
    'a3-checkbox',
    'Project Room must carry the actionable reason the stop is still pending'
  );
  assert.strictEqual(api.recoverManagedA3Ownership(), false, 'a failed stop delivery must not re-arm A3');

  const label = api.superCompactAutoLabel();
  assert.notStrictEqual(label, 'CHAT', 'an unacknowledged operator stop must not read as ordinary CHAT');
  assert.ok(/ATTN/i.test(label), `expected an attention label while the Bridge stop is pending, got ${label}`);
});

test('K3d: repeated OFF clicks are idempotent -- one stop, then ACK', async () => {
  const { h, api } = splitBrainHarness();
  api.setAutoAuditEnabled(true, { source: 'test' });

  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();
  const first = stopRequests(h).length;
  assert.strictEqual(first, 1, 'the first OFF must issue exactly one Bridge stop');

  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();

  assert.strictEqual(
    stopRequests(h).length,
    1,
    `repeat OFF clicks must not create contradictory transitions, saw ${stopRequests(h).length}`
  );
});

test('K3e: INTERNAL disarms never touch the Bridge dispatch', async () => {
  // Milestone F. Every one of these is a repair path, not a human decision.
  // Only an explicit operator action may retire a managed run.
  const { h, api } = splitBrainHarness();

  api.setAutoAuditEnabled(false, { source: 'internal' });
  await h.settle();
  assert.strictEqual(stopRequests(h).length, 0, 'an internal disarm cancelled the Bridge dispatch');
  assert.ok(api.browserWorkerLease, 'an internal disarm dropped the worker lease');

  // Route hydration drift, stale build recovery, worker stand-down, storage
  // repair and stale-key repair all disarm through the same internal path.
  api.setAutoAuditEnabled(false, { source: 'route-hydration' });
  await h.settle();
  assert.strictEqual(stopRequests(h).length, 0, 'route hydration drift cancelled the Bridge dispatch');

  api.setAutoAuditEnabled(false, { source: 'worker-stand-down' });
  await h.settle();
  assert.strictEqual(stopRequests(h).length, 0, 'worker stand-down cancelled the Bridge dispatch');

  // The recovery disarm this path exists to be repairable by. Either the
  // observer already re-armed it during settle(), or this call does.
  const repaired = api.recoverManagedA3Ownership();
  assert.ok(
    api.autoRuntime.enabled === true || repaired === true,
    'an internally disarmed managed dispatch must stay repairable'
  );
  assert.strictEqual(api.autoRuntime.a3OperatorExplicitOff, false, 'an internal disarm recorded an operator stop');
  assert.strictEqual(stopRequests(h).length, 0, 'managed recovery cancelled the Bridge dispatch');
});

test('K3f: an unmanaged chat with an operator OFF issues no Bridge stop', async () => {
  const { h, api } = splitBrainHarness({ noLease: true });
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();

  assert.strictEqual(stopRequests(h).length, 0, 'ordinary unmanaged A3 toggling must stay local-only');
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT', 'an unmanaged operator OFF is ordinary CHAT');
});

// Milestone E: the reload cases. Same button, three different moments.

test('K3g: a reload AFTER an acknowledged stop does not resurrect A3 or the dispatch', async () => {
  const { h, api } = splitBrainHarness();
  h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 200, responseText: JSON.stringify({ ok: true, dispatch_id: 'dsp-fastprompter01', state: 'FAILED' }) }
    : { status: 200, responseText: JSON.stringify({ ok: true }) });
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();
  assert.strictEqual(api.autoRuntime.a3OperatorStopPending, null, 'the stop was never acknowledged');

  const raw = h.gmStore.get(RUNTIME_KEY);
  const leaseRaw = h.sessionStore.get(LEASE_SESSION_KEY);
  const h2 = createHarness({ location: chatLocation() });
  h2.gmStore.set(RUNTIME_KEY, raw);
  if (leaseRaw) h2.sessionStore.set(LEASE_SESSION_KEY, leaseRaw);
  h2.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  const api2 = h2.load();
  if (h2.loadError) throw h2.loadError;
  api2.state.bridgeEnabled = true;
  await h2.settle();

  assert.strictEqual(api2.autoRuntime.enabled, false, 'a reload resurrected A3 against the operator');
  assert.strictEqual(api2.autoRuntime.a3OperatorExplicitOff, true, 'the operator intent did not survive the reload');
  assert.strictEqual(api2.recoverManagedA3Ownership(), false, 'recovery re-armed an acknowledged operator stop');
  assert.strictEqual(
    h2.httpRequests.filter(r => r.method === 'POST' && STOP_PATH.test(String(r.url || ''))).length,
    0,
    'an acknowledged stop was delivered twice after a reload'
  );
});

test('K3h: a reload BEFORE the acknowledgement resumes the stop, bounded, and stays OFF', async () => {
  const { h, api } = splitBrainHarness();
  h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 500, responseText: JSON.stringify({ ok: false, error: { code: 'bridge_down', message: 'no' } }) }
    : { status: 200, responseText: JSON.stringify({ ok: true }) });
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await h.settle();
  assert.ok(api.autoRuntime.a3OperatorStopPending, 'a refused stop must stay pending across a reload');

  const raw = h.gmStore.get(RUNTIME_KEY);
  const leaseRaw = h.sessionStore.get(LEASE_SESSION_KEY);

  // The reloaded window now has a healthy Bridge.
  const h2 = createHarness({ location: chatLocation() });
  h2.gmStore.set(RUNTIME_KEY, raw);
  if (leaseRaw) h2.sessionStore.set(LEASE_SESSION_KEY, leaseRaw);
  h2.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  h2.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 200, responseText: JSON.stringify({ ok: true, dispatch_id: 'dsp-fastprompter01', state: 'FAILED' }) }
    : { status: 200, responseText: JSON.stringify({ ok: true, job: { state: 'AUDITING' } }) });
  const api2 = h2.load();
  if (h2.loadError) throw h2.loadError;
  api2.state.bridgeEnabled = true;
  api2.browserWorkerDispatchState = 'AUDITING';
  // The managed worker poll delegates to exactly this call on every tick. Not
  // awaited: the Bridge reply only arrives when the fake clock is advanced.
  api2.managedA3OperatorStop({ reason: 'a3-checkbox' });
  await h2.settle();

  assert.strictEqual(api2.autoRuntime.enabled, false, 'the resumed stop re-armed A3');
  assert.strictEqual(api2.autoRuntime.a3OperatorExplicitOff, true, 'the operator intent did not survive the reload');
  assert.strictEqual(api2.autoRuntime.a3OperatorStopPending, null, 'the resumed stop was never acknowledged');
  assert.strictEqual(api2.browserWorkerDispatchState, 'FAILED', 'the resumed stop did not retire the dispatch');
  assert.strictEqual(
    h2.httpRequests.filter(r => r.method === 'POST' && STOP_PATH.test(String(r.url || ''))).length,
    1,
    'the resumed stop must be exactly one delivery, not a retry storm'
  );
});

test('K3i: the stop retry budget is finite', async () => {
  const { h, api } = splitBrainHarness();
  h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 500, responseText: JSON.stringify({ ok: false, error: { code: 'bridge_down', message: 'no' } }) }
    : { status: 200, responseText: JSON.stringify({ ok: true, job: { state: 'AUDITING' } }) });
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });

  for (let round = 0; round < 8; round += 1) {
    api.managedA3OperatorStop({ reason: 'a3-checkbox' });
    await h.settle();
  }

  const stops = stopRequests(h).length;
  assert.ok(stops > 0, 'a refused stop was never retried at all');
  assert.ok(stops <= 4, `a dead Bridge produced ${stops} stop attempts; the retry budget is not bounded`);
  assert.strictEqual(api.autoRuntime.enabled, false, 'a dead Bridge re-armed A3');
});

// Milestone K: multi-worker isolation. One operator stop, one lane.

test('K3j: one explicit stop affects only its own lane', async () => {
  const one = splitBrainHarness({ pathname: 'c:worker-one', tabId: 'audapack-managed-1-1-abc' });
  one.h.httpResponder = request => (STOP_PATH.test(String(request.url || ''))
    ? { status: 200, responseText: JSON.stringify({ ok: true, dispatch_id: 'dsp-fastprompter01', state: 'FAILED' }) }
    : { status: 200, responseText: JSON.stringify({ ok: true }) });

  // A second managed worker: different tab id, different dispatch, different
  // conversation, different campaign run.
  const two = splitBrainHarness({
    pathname: 'c:worker-two',
    tabId: 'audapack-managed-1-2-xyz',
    dispatchId: 'dsp-second02',
    lease: { campaign_run_id: 'acb-run-fast-2' },
    conversationKey: 'c:worker-two',
    runtime: { runId: 'acb-run-fast-2', coreUserId: 'core-2' }
  });

  one.api.setAutoAuditEnabled(true, { source: 'test' });
  one.api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  await one.h.settle();

  assert.strictEqual(one.api.browserWorkerDispatchState, 'FAILED', 'the stopped lane did not retire');
  assert.strictEqual(
    one.h.httpRequests.filter(r => r.method === 'POST' && /dsp-second02/.test(String(r.url || ''))).length,
    0,
    'the stopped worker spoke to another worker\'s dispatch'
  );

  assert.strictEqual(
    two.api.browserWorkerDispatchState,
    'AUDITING',
    'a stop on one worker retired another worker\'s dispatch'
  );
  assert.ok(two.api.browserWorkerLease, 'a stop on one worker dropped another worker\'s lease');
  assert.strictEqual(
    two.api.managedA3OwnershipSnapshot().shouldOwnA3,
    true,
    `the surviving worker lost A3 ownership, got reason=${two.api.managedA3OwnershipSnapshot().reason}`
  );
  assert.strictEqual(
    two.api.recoverManagedA3Ownership(),
    true,
    'the surviving worker could not recover A3 because its neighbour was stopped'
  );
});

test('K4: active dispatch + CORE visible + enabled false is not CHAT', () => {
  const { api } = splitBrainHarness();
  assert.notStrictEqual(api.superCompactAutoLabel(), 'CHAT');
});

test('K5: ZIP + CORE + STOPPED assistant keeps the campaign at 0/3 and is not DONE', () => {
  const { api } = splitBrainHarness();
  const interrupted = api.interruptedAuditResponseSnapshot();
  assert.strictEqual(interrupted.interrupted, true, `expected an interrupted Core, got ${interrupted.reason}`);

  assert.strictEqual(api.recoverManagedA3Ownership(), true);
  assert.strictEqual(api.autoRuntime.currentWaveIndex, 0, 'the wave must not advance');
  const completion = api.campaignCompletionSnapshot();
  assert.strictEqual(completion.doneCount, 0, 'a stopped answer is not a completed wave');
  assert.strictEqual(completion.complete, false);
  assert.strictEqual(api.autoRuntime.stage, 'paused', 'the interrupted wave is held, not abandoned');

  const label = api.superCompactAutoLabel();
  assert.notStrictEqual(label, 'CHAT');
  assert.notStrictEqual(label, 'DONE');
  assert.ok(['CORE', 'HOLD', 'RETRY', 'PAUSE', 'ATTN'].includes(label), `unexpected label ${label}`);
});

test('K5b: re-arming an interrupted Core never sends anything', () => {
  const { h, api } = splitBrainHarness();
  const before = h.httpRequests.length;
  api.recoverManagedA3Ownership();
  const sent = h.httpRequests.slice(before).filter(request => /send/i.test(String(request.url || '')));
  assert.deepStrictEqual(sent, [], `recovery performed an irreversible send: ${JSON.stringify(sent)}`);
});

test('K6: an interrupted Core reconstructs managed ownership after a reload', () => {
  const { h, api } = splitBrainHarness();
  api.recoverManagedA3Ownership();

  // A fresh window seeded with exactly the bytes the first one wrote.
  const runtimeKey = `ai_chatbuttons_auto_audit_runtime_v4:${CONVERSATION_KEY}`;
  const raw = h.gmStore.get(runtimeKey);
  assert.ok(raw, 'the re-armed runtime must be durable');
  const h2 = createHarness({ location: chatLocation() });
  h2.gmStore.set(runtimeKey, raw);
  const api2 = h2.load();
  if (h2.loadError) throw h2.loadError;

  const restored = JSON.parse(String(h2.gmStore.get(runtimeKey)));
  assert.strictEqual(restored.enabled, true, 'ownership did not survive the reload');
  assert.strictEqual(restored.stage, 'paused', 'the held wave did not survive the reload');
  assert.strictEqual(api2.autoRuntime.enabled, true, 'A3 came back off after a reload');
});

test('K7: a stale draft conversation key is repaired to the dispatch destination', () => {
  const { api } = splitBrainHarness({ conversationKey: `draft:${'0'.repeat(8)}-ffff` });
  const snapshot = api.managedA3OwnershipSnapshot();
  assert.strictEqual(snapshot.conversationMatches, true, 'proven lineage must repair a stale binding');
  assert.strictEqual(snapshot.conversationKey, CONVERSATION_KEY);

  assert.strictEqual(api.recoverManagedA3Ownership(), true);
  assert.strictEqual(api.autoRuntime.conversationKey, CONVERSATION_KEY, 'the binding was not repaired');
});

test('K8: a terminal dispatch never resurrects A3', () => {
  for (const terminal of ['COMPLETE', 'FAILED', 'CANCELLED']) {
    const { api } = splitBrainHarness({ dispatchState: terminal });
    const snapshot = api.managedA3OwnershipSnapshot();
    assert.strictEqual(snapshot.shouldOwnA3, false, `${terminal} still claimed A3`);
    assert.strictEqual(api.recoverManagedA3Ownership(), false, `${terminal} resurrected A3`);
    assert.strictEqual(api.autoRuntime.enabled, false);
    assert.strictEqual(api.superCompactAutoLabel(), 'CHAT');
  }
});

test('K9: an unmanaged chat with a disabled runtime is still CHAT', () => {
  const { api } = splitBrainHarness({ noLease: true });
  assert.strictEqual(api.managedA3OwnershipSnapshot().managed, false);
  assert.strictEqual(api.recoverManagedA3Ownership(), false);
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT', 'ordinary local A3 semantics must not change');
});

test('K9b: a chat with the bridge switched off is still CHAT', () => {
  const { api } = splitBrainHarness({ bridgeEnabled: false });
  assert.strictEqual(api.managedA3OwnershipSnapshot().shouldOwnA3, false);
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT');
});

test('K10: machine-looking text with no managed proof never arms A3', () => {
  const { h, api } = setup({ location: chatLocation('c:ordinary-chat') });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  addTurns(h, [
    userTurn(h, 'u1', 'AUDIT CORE — ACB_CHAIN_RECEIPT: not-mine'),
    assistantTurn(h, 'a1', el => { el._text = 'sure'; })
  ]);
  const snapshot = api.managedA3OwnershipSnapshot();
  assert.strictEqual(snapshot.managed, false, 'a window with no lease is not a managed worker');
  assert.strictEqual(snapshot.shouldOwnA3, false);
  assert.strictEqual(api.recoverManagedA3Ownership(), false);
  assert.strictEqual(api.autoRuntime.enabled, false);
});

test('K11: a completed 3/3 campaign is never resurrected', () => {
  const { api } = splitBrainHarness({ stage: 'complete', finishedCampaign: true, runtime: { currentWaveIndex: 3 } });
  assert.strictEqual(api.visibleCampaignAlreadyFinished(), true, 'the fixture must look finished');
  assert.strictEqual(api.recoverManagedA3Ownership(), false, 'a finished campaign was resurrected');
  assert.strictEqual(api.autoRuntime.enabled, false);
});

test('J: a split brain is reported as a warning, and the heal is reported too', () => {
  const { h, api } = splitBrainHarness();
  api.recoverManagedA3Ownership();
  const log = api.readBridgeDiagnosticLog();
  const split = log.find(entry => entry.event === 'managed_a3_split_brain');
  assert.ok(split, 'the split brain was not recorded');
  assert.strictEqual(split.severity, 'warning', 'a split brain is not heartbeat noise');
  assert.strictEqual(split.facts.dispatch_id, 'dsp-fastprompter01');
  assert.strictEqual(split.facts.campaign_run_id, 'acb-run-fast-1');
  assert.strictEqual(split.facts.conversation_key, CONVERSATION_KEY);
  assert.strictEqual(split.facts.explicit_operator_stop, false);
  assert.strictEqual(split.facts.recovery_action, 'rearm');
  assert.ok(h.gmStore.size >= 0);

  const healed = log.find(entry => entry.event === 'managed_a3_ownership_recovered');
  assert.ok(healed, 'the successful heal was not recorded');
  assert.strictEqual(healed.facts.recovery_action, 'rearmed');
});

test('A: a disarm records where it came from, and only an operator sets the flag', () => {
  // Unmanaged on purpose: under a live managed dispatch the recovery below
  // would (correctly) re-arm the drift and retire its own provenance.
  const { api } = splitBrainHarness({ noLease: true });
  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { source: 'internal-drift' });
  assert.strictEqual(api.autoRuntime.a3OperatorExplicitOff, false, 'an internal disarm claimed operator intent');
  assert.strictEqual(api.autoRuntime.a3DisabledSource, 'internal-drift');

  api.setAutoAuditEnabled(true, { source: 'test' });
  api.setAutoAuditEnabled(false, { operator: true, source: 'a3-checkbox' });
  assert.strictEqual(api.autoRuntime.a3OperatorExplicitOff, true);
  assert.strictEqual(api.autoRuntime.a3DisabledSource, 'a3-checkbox');

  // The provenance survives persistence: an old runtime carrying only
  // enabled=false stays repairable, a real stop does not.
  const round = api.normalizeAutoRuntime(JSON.parse(JSON.stringify(api.autoRuntime)), CONVERSATION_KEY);
  assert.strictEqual(round.a3OperatorExplicitOff, true);
  assert.strictEqual(api.normalizeAutoRuntime({ version: 5, stage: 'idle' }, CONVERSATION_KEY).a3OperatorExplicitOff, false);
});

test('N: one worker never adopts another worker dispatch', () => {
  const first = splitBrainHarness({ pathname: 'c:worker-one' });
  assert.strictEqual(first.api.managedA3OwnershipSnapshot().shouldOwnA3, true);

  // A second window: a different tab, a different conversation, no lease of
  // its own. The first window's dispatch must mean nothing to it.
  const { h, api } = setup({ location: chatLocation('c:worker-two') });
  api.state.bridgeEnabled = true;
  api.browserWorkerLease = null;
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: false }), conversationKey: 'c:worker-two' };
  addTurns(h, [userTurn(h, 'u', 'hello'), assistantTurn(h, 'a', el => { el._text = 'hi'; })]);

  const snapshot = api.managedA3OwnershipSnapshot();
  assert.strictEqual(snapshot.ownsDispatch, false);
  assert.strictEqual(snapshot.shouldOwnA3, false, 'a worker adopted a dispatch it does not hold');
  assert.strictEqual(api.recoverManagedA3Ownership(), false);
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT');
});

test('G: an interrupted wave reports its reason to the lane without moving its state', async () => {
  const { h, api } = splitBrainHarness();
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  h.httpResponder = () => ({ status: 200, responseText: JSON.stringify({ ok: true, job: { state: 'AUDITING' } }) });

  api.recoverManagedA3Ownership();
  await h.settle();

  // The lane may receive more than one /state report; the one this test exists
  // for is the one that carries the interruption reason.
  const report = h.httpRequests
    .filter(request => String(request.url || '').includes('/state'))
    .find(request => /"attention":"interrupted-core/.test(String(request.data || ''))) ||
    h.httpRequests.find(request => String(request.url || '').includes('/state'));
  assert.ok(report, 'the interrupted wave was never reported to the lane');
  const body = JSON.parse(String(report.data || '{}'));
  assert.strictEqual(body.state, 'AUDITING', 'reporting an interruption must not move the campaign state machine');
  assert.match(String(body.attention || ''), /^interrupted-core: /, `unexpected lane reason ${body.attention}`);
});
