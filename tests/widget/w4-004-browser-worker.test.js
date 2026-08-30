'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

test('SRC-005 worker snapshot identifies stable tab and safe FREE state', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  h.location.href = 'https://chatgpt.com/';
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  const snapshot = api.browserWorkerSnapshot();
  assert.ok(snapshot.worker_id);
  assert.strictEqual(snapshot.site, 'chatgpt');
  assert.strictEqual(snapshot.browser_name, 'Brave');
  assert.strictEqual(snapshot.is_brave, true);
  assert.strictEqual(snapshot.page_eligible, true);
  assert.strictEqual(snapshot.state, 'FREE');
  assert.strictEqual(snapshot.generating, false);
  assert.strictEqual(snapshot.has_manual_draft, false);
  assert.strictEqual(snapshot.has_attachments, false);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
});

test('SRC-005 worker refuses FREE claim while runtime is active', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'running', runId: 'run-active-1' };
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerSnapshot().state, 'AUDITING');
});

test('SRC-005 worker start and stop are idempotent controls', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  assert.strictEqual(api.startBrowserWorker(), true);
  assert.strictEqual(api.stopBrowserWorker(), true);
  assert.strictEqual(api.stopBrowserWorker(), true);
});

test('SRC-005 worker refuses Chrome and non-root ChatGPT pages', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  delete h.navigator.brave;
  assert.strictEqual(api.browserWorkerSnapshot().browser_name, 'Chrome');
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.startBrowserWorker(), false);

  h.navigator.brave = { isBrave: () => Promise.resolve(true) };
  h.location.pathname = '/c/existing-chat';
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.startBrowserWorker(), false);
});

test('W8: stop preserves active non-terminal lease for recovery', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  h.navigator.brave = { isBrave: () => Promise.resolve(true) };
  api.state.bridgeEnabled = true;
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: String(api.browserWorkerSnapshot().worker_id),
    lease_id: 'lease-1',
    project_id: 'p1',
    project_name: 'P1',
    campaign_run_id: '',
    start_receipt: ''
  };
  api.persistBrowserWorkerLease();
  assert.strictEqual(api.stopBrowserWorker(), true);
  // Active lease checkpoint must survive a plain stop.
  assert.ok(api.browserWorkerLease, 'active lease must survive stop');
  assert.strictEqual(api.browserWorkerLease.dispatch_id, 'dsp-0123456789abcdef');
});

test('W8: stop clears lease when no active dispatch', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  api.browserWorkerLease = null;
  api.persistBrowserWorkerLease();
  assert.strictEqual(api.stopBrowserWorker(), true);
  assert.strictEqual(api.browserWorkerLease, null);
});
