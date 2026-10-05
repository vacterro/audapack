'use strict';

// A dispatch the Bridge has already retired FAILED must not keep its window.
//
// Measured live on dsp-c6763cc27a1242a9: the Core was sent and ACKed, ChatGPT
// never created the conversation, and the only honest way to clear a stuck
// post-START lane is the Bridge's abandon. The widget half of that handshake is
// pinned here so it cannot regress into holding a dead dispatch; the Bridge half
// (a window heartbeating a terminal dispatch must not stay RESERVED) is pinned in
// tests/test_browser_dispatch.py, where dsp-c6763cc27a1242a9 stayed pinned with
// page_eligible=false until it was fixed.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function failedHarness(overrides = {}) {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-1-1-xyz',
    lease_id: 'lease-0123456789abcdef',
    project_id: 'fastprompter',
    project_name: 'FastPrompter',
    campaign_run_id: 'acb-run-1',
    start_receipt: 'startcore-1'
  };
  api.autoRuntime = {
    ...api.emptyAutoRuntime({ enabled: true }),
    stage: overrides.stage || 'idle',
    runId: overrides.runId === undefined ? 'acb-run-1' : overrides.runId
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
            state: overrides.ownedState || 'FAILED',
            recovery_state: '',
            campaign_run_id: 'acb-run-1',
            lease_id: 'lease-0123456789abcdef',
            error: overrides.ownedError || 'operator_abandoned'
          },
          worker_state: 'RESERVED',
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

test('a terminal FAILED dispatch releases the lease even with a live run id', async () => {
  const { h, api } = failedHarness();

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.strictEqual(api.browserWorkerLease, null,
    'an abandoned dispatch must not keep the window reserved forever');
});

test('a live STARTED dispatch keeps its lease', async () => {
  const { h, api } = failedHarness({ ownedState: 'STARTED' });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(api.browserWorkerLease, 'a running dispatch must never be dropped by this branch');
});