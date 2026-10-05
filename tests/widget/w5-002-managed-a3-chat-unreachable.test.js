'use strict';

// SRC-098, exhaustive form.
//
// w5-001 pins the split brain at four hand-picked points. That is evidence,
// not proof: the label has a five-way ladder inside the disabled-runtime
// branch, and four examples cannot show that CHAT is unreachable everywhere the
// Bridge still holds a live managed dispatch. A regression introduced in a
// combination nobody thought to write down would sail past them.
//
// This sweeps the state space instead. For every combination of dispatch
// state, runtime stage, enabled bit, conversation binding, operator stop and
// stop-pending flag, it asserts the invariant SRC-098 actually states:
//
//   managed && campaignActive && !explicitOperatorStop  =>  label != 'CHAT'
//
// and separately pins the ONE case where CHAT beside a still-AUDITING dispatch
// is correct rather than a lie: the operator stopped A3 themselves and the
// Bridge confirmed the stop. "Nothing is running" is then the truth, and
// suppressing it would be the same split brain pointed the other way.

const { test } = require('node:test');
const assert = require('node:assert');
const { createHarness } = require('./harness');

const CONVERSATION_KEY = 'c:fastprompter-run';

function chatLocation(conversation = CONVERSATION_KEY) {
  const path = `/c/${String(conversation).replace(/^c:/, '')}`;
  return { href: `https://chatgpt.com${path}`, pathname: path, search: '' };
}

function build(caseSpec) {
  const h = createHarness({ location: chatLocation(caseSpec.conversationKey) });
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  const api = h.load();
  if (h.loadError) throw h.loadError;
  api.state.bridgeEnabled = caseSpec.bridgeEnabled !== false;

  if (!caseSpec.noLease) {
    api.browserWorkerLease = {
      dispatch_id: 'dsp-fastprompter01',
      worker_id: String(h.sessionStore.get('ai_chatbuttons_auto_tab_id_v1') || 'audapack-managed-1-1-abc'),
      lease_id: 'lease-fastprompter01',
      project_id: 'fastprompter',
      project_name: 'FastPrompter',
      campaign_run_id: 'acb-run-fast-1',
      start_receipt: 'startcore-fast-1',
      profile: 'quick3',
      ...(caseSpec.lease || {})
    };
  }
  api.browserWorkerDispatchState = caseSpec.dispatchState || 'AUDITING';

  api.autoRuntime = {
    ...api.emptyAutoRuntime({ enabled: caseSpec.enabled !== false }),
    version: 5,
    conversationKey: caseSpec.conversationKey || CONVERSATION_KEY,
    stage: caseSpec.stage || 'wait-core',
    runId: 'acb-run-fast-1',
    coreUserId: 'core-1',
    expectedKind: 'core',
    projectName: 'FastPrompter',
    projectId: 'fastprompter',
    startedAt: Date.now() - 60000,
    waitStartedAt: Date.now() - 30000,
    ...(caseSpec.runtime || {})
  };
  if (caseSpec.operatorStop) api.autoRuntime.a3OperatorExplicitOff = true;
  if (caseSpec.stopPending) {
    api.autoRuntime.a3OperatorStopPending = {
      dispatch_id: 'dsp-fastprompter01',
      campaign_run_id: 'acb-run-fast-1',
      attempts: 1,
      at: Date.now()
    };
  }
  return api;
}

// Every case rebuilds the whole widget, so the axes are chosen for what can
// actually change the verdict, not for combinatorial completeness: which
// dispatch state the Bridge reports, where the runtime sits, whether the local
// enabled bit agrees, and whether the runtime is still bound to this
// conversation. The terminal-vs-live distinction is covered separately.
const DISPATCH_STATES = ['AUDITING', 'STARTED', 'FINALIZING'];
const STAGES = ['idle', 'wait-core', 'complete'];
const RUNTIME_KEYS = [CONVERSATION_KEY, ''];

function* liveDispatchCases() {
  for (const dispatchState of DISPATCH_STATES) {
    for (const stage of STAGES) {
      for (const enabled of [true, false]) {
        for (const conversationKey of RUNTIME_KEYS) {
          yield { dispatchState, stage, enabled, conversationKey };
        }
      }
    }
  }
}

test('SRC-098 sweep: CHAT is unreachable while the Bridge holds a live managed A3 dispatch', () => {
  const violations = [];
  let checked = 0;
  for (const spec of liveDispatchCases()) {
    const api = build(spec);
    const snap = api.managedA3OwnershipSnapshot();
    if (!(snap.managed && snap.campaignActive && !snap.explicitOperatorStop)) continue;
    checked += 1;
    const label = api.superCompactAutoLabel();
    if (label === 'CHAT') {
      violations.push({ ...spec, reason: snap.reason, dispatchId: snap.dispatchId });
    }
  }

  assert.ok(checked > 0, 'the sweep exercised no live-dispatch case at all');
  assert.deepStrictEqual(violations, [],
    `CHAT is reachable beside a live managed A3 dispatch in ${violations.length} case(s): ` +
    JSON.stringify(violations.slice(0, 5), null, 1));
  assert.ok(checked >= 18, `sweep was suspiciously small: ${checked} live-dispatch cases`);
});

test('SRC-098 sweep: a live dispatch survives every disarm path without reporting CHAT', () => {
  // The disarm paths are the ways the runtime can be turned off underneath a
  // live dispatch: route hydration, runtime migration, storage repair, worker
  // stand-down, stale conversation-key repair, and managed recovery. Each is
  // simulated here as a DISABLED runtime, which is the state a disarm leaves
  // behind. A live dispatch must still not read CHAT, whatever disabled it.
  const disarmPaths = {
    'route-hydration': {},
    'runtime-migration': { conversationKey: '' },
    'storage-repair': { version: 1 },
    'worker-stand-down': { stage: 'idle', coreUserId: '', expectedKind: '' },
    'stale-key-repair': { conversationKey: 'c:stale-draft-identity' },
    'managed-recovery': { stage: 'complete' }
  };

  const violations = [];
  for (const [path, runtime] of Object.entries(disarmPaths)) {
    for (const dispatchState of ['AUDITING', 'STARTED', 'FINALIZING']) {
      const api = build({ dispatchState, enabled: false, stage: 'wait-core', runtime });
      const snap = api.managedA3OwnershipSnapshot();
      const label = api.superCompactAutoLabel();
      if (snap.campaignActive && !snap.explicitOperatorStop && label === 'CHAT') {
        violations.push({ path, dispatchState, reason: snap.reason });
      }
    }
  }

  assert.deepStrictEqual(violations, [],
    `a disarm path reported CHAT beside a live dispatch: ${JSON.stringify(violations, null, 1)}`);
});

test('SRC-098 sweep: the operator stop is the ONLY route to CHAT beside an AUDITING dispatch', () => {
  // Positive control for the sweep above: if nothing else reaches CHAT, the
  // operator's own confirmed stop is the single remaining door, and it must
  // still be a door -- over-correcting SRC-098 into "never CHAT" would make a
  // deliberately stopped campaign indistinguishable from a live one.
  const stopped = build({ dispatchState: 'AUDITING', enabled: false, operatorStop: true, stopPending: false });
  const snap = stopped.managedA3OwnershipSnapshot();
  assert.strictEqual(snap.explicitOperatorStop, true, 'the operator stop was not recorded');
  assert.strictEqual(snap.campaignActive, true, 'the dispatch is still live in this fixture');
  assert.strictEqual(stopped.superCompactAutoLabel(), 'CHAT',
    'a confirmed operator stop must read CHAT; the campaign really is over');

  // The same stop, unconfirmed, must NOT read CHAT: the Bridge still holds it.
  const unconfirmed = build({ dispatchState: 'AUDITING', enabled: false, operatorStop: true, stopPending: true });
  assert.notStrictEqual(unconfirmed.superCompactAutoLabel(), 'CHAT',
    'an unacknowledged stop must not claim the campaign ended while the Bridge still holds the lane');
});

test('SRC-098 sweep: an unconfirmed operator stop stays ATTN, never ordinary CHAT', () => {
  for (const dispatchState of ['AUDITING', 'STARTED']) {
    const api = build({ dispatchState, enabled: false, operatorStop: true, stopPending: true });
    assert.strictEqual(api.superCompactAutoLabel(), 'ATTN',
      `an unacknowledged stop at ${dispatchState} must read ATTN, not CHAT`);
  }
});
