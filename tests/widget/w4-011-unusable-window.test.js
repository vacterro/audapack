'use strict';

// A managed worker window signed out of ChatGPT lands on the marketing page:
// no composer, no eligibility, so startBrowserWorker refused and the window
// never registered at all -- invisible to the pool and to the operator, who
// saw W 1/6 with nothing anywhere saying why.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function managedWindow(options = {}) {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: options.pathname || '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  return { h, api };
}

test('W11: a signed-out window names the reason', () => {
  const { h, api } = managedWindow();
  // No composer at all, and a way in offered: the logged-out landing page.
  const login = h.el('button', { 'aria-label': 'Log in' });
  h.dom.querySelector('body').appendChild(login);

  assert.strictEqual(api.chatGPTSignedOut(), true);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'signed-out');
});

test('W11: a signed-in window with a composer is not called signed out', () => {
  const { h, api } = managedWindow();
  composerFixture(h);
  assert.strictEqual(api.chatGPTSignedOut(), false);
  assert.notStrictEqual(api.browserWorkerUnusableReason(), 'signed-out');
});

test('W11: a window parked on a conversation says so, not "signed out"', () => {
  const { h, api } = managedWindow({ pathname: '/c/abc123' });
  composerFixture(h);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'not-on-root-chat');
});

test('W11: a window with no composer and no login offer is not guessed at', () => {
  const { api } = managedWindow();
  assert.strictEqual(api.chatGPTSignedOut(), false);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'no-composer');
});

test('W11: a dispatched run arms the audit engine', () => {
  // The Bridge sends the Core and moves the lane to AUDITING, but harvesting
  // the response and committing the wave is the widget's own auto engine, and
  // a worker window's engine is off by default: a CM audit ran for 11m34s,
  // produced a valid terminal handoff in the chat, and nothing was saved.
  const { h, api } = managedWindow();
  composerFixture(h);
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  assert.strictEqual(Boolean(api.autoRuntime.enabled), false);

  api.browserWorkerArmAuditEngine();
  assert.strictEqual(Boolean(api.autoRuntime.enabled), true);
});

test('W11: arming an engine that is already on is a no-op', () => {
  const { h, api } = managedWindow();
  composerFixture(h);
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), runId: 'acb-live' };
  assert.strictEqual(api.browserWorkerArmAuditEngine(), true);
  assert.strictEqual(api.autoRuntime.runId, 'acb-live', 'an armed run must not be disturbed');
});

test('W11: consuming a job arms the engine and applies the dispatched profile', async () => {
  // The full path, not just the helpers: a CM audit ran to a valid handoff in
  // the chat and saved nothing, because the engine that harvests and commits
  // the wave was never armed and the window was still on A3.
  const { api } = setup();
  api.state.auditProfile = 'quick3';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  const transitions = [];
  let armedBeforeSend = false;
  const input = {};
  const root = { contains: node => node === input };
  const archiveFile = { name: 'SMART_VAC.zip', size: 99 };

  const ok = await api.browserWorkerConsume({
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'worker-1',
    lease_id: 'lease-1',
    project_id: 'smart_vac_cleaner',
    project_name: 'Smart VAC Cleaner',
    campaign_run_id: 'acb-cm-1',
    profile: 'compress',
    archive_filename: archiveFile.name,
    archive_size: archiveFile.size
  }, {
    transition: async (state, payload = {}) => {
      transitions.push(state);
      return { ok: true };
    },
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => input,
    composerRoot: () => root,
    injectFiles: () => true,
    waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: [archiveFile.name] }),
    startAudit: async ({ beforeIrreversibleSend }) => {
      armedBeforeSend = Boolean(api.autoRuntime && api.autoRuntime.enabled);
      await beforeIrreversibleSend({ receipt: 'receipt-1', campaignRunId: 'acb-cm-1' });
      return true;
    }
  });

  assert.ok(ok);
  assert.strictEqual(api.getActiveProfile().profile_id, 'compress', 'the job names the profile');
  assert.strictEqual(Boolean(api.autoRuntime.enabled), true, 'the engine must be armed to commit the wave');
  assert.ok(armedBeforeSend, 'arming after the send leaves startAudit running unarmed: no wave stage, nothing harvested');
  assert.deepStrictEqual(transitions, [
    'ARTIFACT_FETCHED', 'ATTACHED', 'START_PREPARED', 'STARTED', 'AUDITING'
  ]);
});

test('W11: a live run whose engine went idle is put back on its own audit turn', () => {
  // The Bridge says this window owns a post-start run and the engine is at
  // stage idle: the Core was sent before the engine was armed, or route
  // hydration dropped the runtime as the draft became /c/<id>. Either way the
  // chat already holds the machine-authored turn.
  const { h, api } = managedWindow();
  composerFixture(h);
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef', worker_id: 'w', lease_id: 'l'
  };
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  assert.strictEqual(String(api.autoRuntime.stage || 'idle'), 'idle');
  api.browserWorkerRecoverIdleEngine();
  assert.strictEqual(Boolean(api.autoRuntime.enabled), true);
});

test('W11: a run already mid-wave is never disturbed by the recovery', () => {
  const { h, api } = managedWindow();
  composerFixture(h);
  api.browserWorkerLease = { dispatch_id: 'dsp-0123456789abcdef', worker_id: 'w', lease_id: 'l' };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'wait-compress', runId: 'acb-live' };

  assert.strictEqual(api.browserWorkerRecoverIdleEngine(), false);
  assert.strictEqual(api.autoRuntime.stage, 'wait-compress');
  assert.strictEqual(api.autoRuntime.runId, 'acb-live');
});
