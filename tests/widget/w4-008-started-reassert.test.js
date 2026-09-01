'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function pollHarness(overrides = {}) {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-3-1-xyz',
    lease_id: 'lease-0123456789abcdef',
    project_id: 'saitalk',
    project_name: 'SAITALK',
    campaign_run_id: 'acb-run-1',
    start_receipt: 'startcore-1'
  };
  api.autoRuntime = {
    ...api.emptyAutoRuntime({ enabled: true }),
    stage: 'running',
    runId: 'acb-run-1'
  };

  const posted = [];
  h.httpResponder = options => {
    const url = String(options.url || '');
    let body = {};
    try { body = JSON.parse(options.data || '{}'); } catch (_) { }
    if (url.includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          job: null,
          owned_job: {
            dispatch_id: 'dsp-0123456789abcdef',
            state: overrides.ownedState || 'STARTED',
            recovery_state: overrides.recoveryState || '',
            campaign_run_id: 'acb-run-1',
            lease_id: 'lease-0123456789abcdef',
            error: overrides.ownedError || ''
          },
          worker_state: 'AUDITING',
          status: {}
        })
      };
    }
    if (url.includes('/state')) {
      posted.push(String(body.state || ''));
      return { status: 200, responseText: JSON.stringify({ ok: true, job: { state: body.state } }) };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };
  return { h, api, posted };
}

test('T87: a poll re-asserts AUDITING for a dispatch stranded in STARTED', async () => {
  // STARTED -> AUDITING is sent once, right after the irreversible Send, and
  // was never retried. A single lost response left the audit running in the
  // browser under a dispatch that said STARTED forever.
  const { h, api, posted } = pollHarness();

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('AUDITING'), `expected an AUDITING re-assert, saw ${JSON.stringify(posted)}`);
  assert.ok(api.browserWorkerLease, 'the lease must survive the re-assert');
});

test('T87: a dispatch already in AUDITING is not re-asserted', async () => {
  const { h, api, posted } = pollHarness({ ownedState: 'AUDITING' });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.deepStrictEqual(posted, [], `no transition expected, saw ${JSON.stringify(posted)}`);
});

test('T87: a terminal BLOCKED dispatch is acknowledged instead of throwing', async () => {
  // The ACK used `transition`, a local of browserWorkerConsume that does not
  // exist in the poll scope, so this path threw ReferenceError and the block
  // was never acknowledged.
  const { h, api, posted } = pollHarness({
    ownedState: 'BLOCKED',
    recoveryState: '',
    ownedError: 'file-injection-rejected'
  });
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('BLOCKED'), `expected a BLOCKED ack, saw ${JSON.stringify(posted)}`);
  assert.strictEqual(api.browserWorkerLease, null, 'a terminal block must clear the lease');
});

test('T92: a worker told its build is stale reloads to pick the new one up', async () => {
  // Tampermonkey injects the installed build at page load, so an already-open
  // worker window keeps the old script after an update: the Bridge rejects it
  // as STALE_WIDGET and the lane idles until a human reloads six windows.
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = null;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  h.httpResponder = options => {
    if (String(options.url || '').includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          job: null,
          owned_job: null,
          worker_state: 'FREE',
          worker_widget_stale: true,
          required_widget_build: '0.0.26',
          status: {}
        })
      };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const before = h.location.reloaded;
  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.strictEqual(h.location.reloaded, before + 1, 'a stale worker must reload itself');
});

test('T92: a worker mid-audit never reloads to chase a build', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-1-1-xyz',
    lease_id: 'lease-0123456789abcdef'
  };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'running', runId: 'acb-run-1' };

  const before = h.location.reloaded;
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.26'), false);
  assert.strictEqual(h.location.reloaded, before, 'a run in flight outranks a build update');
});
