'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function addAttachmentTile(h, form, label) {
  const tile = h.el('div', { role: 'group', 'aria-label': label });
  const remove = h.el('button', { 'aria-label': 'Remove file' });
  tile.appendChild(remove);
  form.appendChild(tile);
  return tile;
}

test('P0-10: waitForExactProjectAttachment waits through empty-first-probe', async () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  api.state.bridgeEnabled = true;

  const resultPromise = api.waitForExactProjectAttachment({
    filename: 'TERMISAI_30.08.26-T17-20-40.zip',
    expectedSize: 0,
    timeoutMs: 10000
  });

  // First probe sees zero tiles (simulated by delay). Then add tile.
  h.timers.advance(500);
  await new Promise(resolve => setImmediate(resolve));
  addAttachmentTile(h, form, 'TERMISAI_30.08.26-T17-20-40.zip');
  h.mutate(form);

  // Advance timers to let the next probe find the tile.
  for (let i = 0; i < 20; i++) {
    h.timers.advance(200);
    await new Promise(resolve => setImmediate(resolve));
  }

  const result = await resultPromise;
  assert.ok(result.ok, 'should succeed after tile appears: ' + result.reason);
  assert.strictEqual(result.reason, 'exact-match');
  assert.ok(result.observedNames.includes('TERMISAI_30.08.26-T17-20-40.zip'));
});

test('P0-10: waitForExactProjectAttachment rejects wrong filename', async () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  api.state.bridgeEnabled = true;

  const resultPromise = api.waitForExactProjectAttachment({
    filename: 'TERMISAI_30.08.26-T17-20-40.zip',
    expectedSize: 0,
    timeoutMs: 5000
  });

  h.timers.advance(500);
  await new Promise(resolve => setImmediate(resolve));
  addAttachmentTile(h, form, 'OTHER_PROJECT.zip');
  h.mutate(form);

  for (let i = 0; i < 30; i++) {
    h.timers.advance(200);
    await new Promise(resolve => setImmediate(resolve));
  }

  const result = await resultPromise;
  assert.ok(!result.ok, 'should reject wrong attachment');
  assert.strictEqual(result.reason, 'attachment-identity-mismatch');
});

test('P0-10: waitForExactProjectAttachment times out when tile never appears', async () => {
  const { h, api } = setup();
  composerFixture(h);
  api.state.bridgeEnabled = true;

  const resultPromise = api.waitForExactProjectAttachment({
    filename: 'missing.zip',
    expectedSize: 0,
    timeoutMs: 3000
  });

  for (let i = 0; i < 30; i++) {
    h.timers.advance(200);
    await new Promise(resolve => setImmediate(resolve));
  }

  const result = await resultPromise;
  assert.ok(!result.ok, 'should time out');
  assert.strictEqual(result.reason, 'attachment-registration-timeout');
});

test('P0-6: exact attachment retries locally without reinjecting the ZIP', async () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  const resultPromise = api.waitForExactProjectAttachmentWithRetry({
    filename: 'TERMISAI.zip',
    expectedSize: 0,
    timeoutMs: 3000,
    maxAttempts: 3
  });

  for (let i = 0; i < 7; i++) {
    h.timers.advance(200);
    await new Promise(resolve => setImmediate(resolve));
  }
  addAttachmentTile(h, form, 'TERMISAI.zip');
  h.mutate(form);
  for (let i = 0; i < 10; i++) {
    h.timers.advance(200);
    await new Promise(resolve => setImmediate(resolve));
  }

  const result = await resultPromise;
  assert.ok(result.ok);
  assert.strictEqual(result.reason, 'exact-match');
  assert.strictEqual(result.attempts, 2);
});

test('P0-10: leased consume performs one exact attach and one irreversible START', async () => {
  const { api } = setup();
  const transitions = [];
  let injectionCount = 0;
  let sendCount = 0;
  const input = {};
  const root = { contains: node => node === input };
  const archiveFile = { name: 'TERMISAI_30.08.26-T17-20-40.zip', size: 1234 };

  const ok = await api.browserWorkerConsume({
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'worker-1',
    lease_id: 'lease-1',
    project_id: 'termisai',
    project_name: 'TERMISAI',
    campaign_run_id: 'run-1',
    archive_filename: archiveFile.name,
    archive_size: archiveFile.size
  }, {
    transition: async (state, payload = {}) => {
      transitions.push({ state, payload });
      return { ok: true };
    },
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => input,
    composerRoot: () => root,
    injectFiles: (target, files) => {
      injectionCount += 1;
      assert.strictEqual(target, input);
      assert.strictEqual(files.length, 1);
      assert.strictEqual(files[0], archiveFile);
      return true;
    },
    waitForAttachment: async expected => {
      assert.strictEqual(expected.filename, archiveFile.name);
      assert.strictEqual(expected.expectedSize, archiveFile.size);
      return { ok: true, reason: 'exact-match', observedNames: [archiveFile.name] };
    },
    startAudit: async ({ beforeIrreversibleSend }) => {
      const permitted = await beforeIrreversibleSend({ receipt: 'receipt-1', campaignRunId: 'run-1' });
      assert.ok(permitted);
      sendCount += 1;
      return true;
    }
  });

  assert.ok(ok);
  assert.strictEqual(injectionCount, 1);
  assert.strictEqual(sendCount, 1);
  assert.deepStrictEqual(transitions.map(item => item.state), [
    'ARTIFACT_FETCHED', 'ATTACHED', 'START_PREPARED', 'STARTED', 'AUDITING'
  ]);
});

test('T54: real worker START treats its canonical Core draft as owned and clicks Send', async () => {
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
  addAttachmentTile(h, form, 'TERMISAI_01.09.26-T00-00-00.zip');

  const transitions = [];
  const archiveFile = { name: 'TERMISAI_01.09.26-T00-00-00.zip', size: 1234 };
  const promise = api.browserWorkerConsume({
    dispatch_id: 'dsp-fedcba9876543210',
    worker_id: 'audapack-managed-1-11',
    lease_id: 'lease-real-start',
    project_id: 'termisai',
    project_name: 'TERMISAI',
    campaign_run_id: '',
    archive_filename: archiveFile.name,
    archive_size: archiveFile.size
  }, {
    transition: async (state, payload = {}) => {
      transitions.push({ state, payload });
      return { ok: true };
    },
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => input,
    composerRoot: () => form,
    injectFiles: () => true,
    waitForAttachment: async () => ({
      ok: true,
      reason: 'exact-match',
      observedNames: [archiveFile.name]
    })
  });

  await h.settle();
  const ok = await promise;

  assert.strictEqual(ok, true, JSON.stringify(transitions));
  assert.strictEqual(send._clicked, true, 'managed START must click Send without operator help');
  assert.strictEqual(api.autoRuntime.enabled, true, 'managed START must retain A3 ownership');
  assert.deepStrictEqual(transitions.map(item => item.state), [
    'ARTIFACT_FETCHED', 'ATTACHED', 'START_PREPARED', 'STARTED', 'AUDITING'
  ]);
});

test('T55: a non-retriable artifact rejection blocks once with its exact Bridge code', async () => {
  const { h, api } = setup();
  const { input, root } = composerFixture(h);
  api.state.bridgeEnabled = true;

  const transitions = [];
  const ok = await api.browserWorkerConsume({
    dispatch_id: 'dsp-artifact-gone',
    worker_id: 'worker-1',
    lease_id: 'lease-1',
    project_id: 'saipenview',
    project_name: 'saipenview',
    archive_filename: 'saipenview_01.09.26-T00-00-00.zip',
    archive_size: 1024
  }, {
    transition: async (state, payload = {}) => {
      transitions.push({ state, payload });
      return { ok: true };
    },
    fetchArtifact: async () => ({
      ok: false,
      reason: 'artifact-http-400:missing_archive',
      status: 400,
      code: 'missing_archive',
      message: 'the recorded archive no longer exists',
      retriable: false
    }),
    uploadInput: () => input,
    composerRoot: () => root,
    injectFiles: () => true,
    waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: [] }),
    startAudit: async () => true
  });

  assert.strictEqual(ok, false);
  assert.deepStrictEqual(transitions.map(item => item.state), ['BLOCKED']);
  assert.strictEqual(transitions[0].payload.error, 'artifact-http-400:missing_archive');
});

test('T55: a retriable artifact failure still uses the pre-start retry budget', async () => {
  const { h, api } = setup();
  const { input, root } = composerFixture(h);
  api.state.bridgeEnabled = true;

  const transitions = [];
  const ok = await api.browserWorkerConsume({
    dispatch_id: 'dsp-artifact-flaky',
    worker_id: 'worker-1',
    lease_id: 'lease-1',
    project_id: 'saipenview',
    project_name: 'saipenview',
    archive_filename: 'saipenview_01.09.26-T00-00-00.zip',
    archive_size: 1024
  }, {
    transition: async (state, payload = {}) => {
      transitions.push({ state, payload });
      return { ok: true };
    },
    fetchArtifact: async () => ({
      ok: false,
      reason: 'artifact-request-timeout',
      retriable: true
    }),
    uploadInput: () => input,
    composerRoot: () => root,
    injectFiles: () => true,
    waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: [] }),
    startAudit: async () => true
  });

  assert.strictEqual(ok, false);
  assert.deepStrictEqual(transitions.map(item => item.state), ['RETRYABLE']);
  assert.strictEqual(transitions[0].payload.error, 'artifact-request-timeout');
});

test('T55: decodeArtifactErrorBody recovers the Bridge error code from the response body', () => {
  const { api } = setup();
  const body = new TextEncoder().encode(JSON.stringify({
    ok: false,
    error: { code: 'changed_archive', message: 'the recorded archive digest changed', retriable: false }
  })).buffer;

  const decoded = api.decodeArtifactErrorBody(body);
  assert.strictEqual(decoded.code, 'changed_archive');
  assert.strictEqual(decoded.retriable, false);
  assert.match(decoded.message, /digest changed/);

  const empty = api.decodeArtifactErrorBody(null);
  assert.strictEqual(empty.code, '');
  assert.strictEqual(empty.retriable, null);
});

test('T55: a detailed worker code still resolves a human blocked explanation', () => {
  const { api } = setup();

  const missing = api.formatBrowserWorkerBlockedMessage('artifact-http-400:missing_archive');
  assert.match(missing.headline, /packed project ZIP is gone/);

  const rejected = api.formatBrowserWorkerBlockedMessage('canonical-start-rejected: START AUDITING is not ready: composer busy');
  assert.match(rejected.headline, /canonical START receipt/);
});

test('T68: a job the worker cannot consume is handed straight back, never dropped', async () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');


  const pending = api.browserWorkerReleaseUnclaimableJob(
    { dispatch_id: 'dsp-unclaimable', lease_id: 'lease-unclaimable' },
    'worker-has-conversation'
  );
  await h.settle();
  await pending;

  const sent = h.httpRequests.find(item => /\/v1\/browser\/jobs\/dsp-unclaimable\/state$/.test(String(item.url || '')));
  assert.ok(sent, JSON.stringify(h.httpRequests.map(item => item.url)));
  assert.strictEqual(sent.method, 'POST');
  const body = JSON.parse(sent.data);
  assert.strictEqual(body.state, 'RETRYABLE');
  assert.strictEqual(body.lease_id, 'lease-unclaimable');
  assert.strictEqual(body.error, 'worker-has-conversation');
});

test('T68: a local lease the Bridge no longer knows about is dropped', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  api.browserWorkerLease = { dispatch_id: 'dsp-ghost', worker_id: 'w', lease_id: 'l' };

  // The Bridge still owns a job for this worker: keep the lease.
  assert.strictEqual(api.browserWorkerDropStaleLease({ dispatch_id: 'dsp-ghost' }), false);
  assert.ok(api.browserWorkerLease);

  // The Bridge owns nothing: the lease is stale bookkeeping that would keep
  // browserWorkerCanClaim() false forever.
  assert.strictEqual(api.browserWorkerDropStaleLease(null), true);
  assert.strictEqual(api.browserWorkerLease, null);
});

test('T70: a revoked START makes the window stand down instead of looping forever', async () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { form, input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.state.auditProfile = 'quick3';
  api.state.chatgptPromptDelivery = 'text';
  const archiveFile = { name: 'SAITALK_31.08.26-T14-52-02.zip', size: 777477 };
  addAttachmentTile(h, form, archiveFile.name);

  const transitions = [];
  const promise = api.browserWorkerConsume({
    dispatch_id: 'dsp-revoked',
    worker_id: 'audapack-managed-1-1',
    lease_id: 'lease-revoked',
    project_id: 'saitalk',
    project_name: 'SAITALK',
    archive_filename: archiveFile.name,
    archive_size: archiveFile.size
  }, {
    transition: async (state, payload = {}) => {
      transitions.push(state);
      if (state === 'START_PREPARED') {
        // Another window owns this dispatch now.
        return { ok: false, error: { code: 'not_leased_owner', message: 'lease is not owned', retriable: false } };
      }
      return { ok: true };
    },
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => input,
    composerRoot: () => form,
    injectFiles: () => true,
    waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: [archiveFile.name] })
  });

  await h.settle();
  const ok = await promise;

  assert.strictEqual(ok, false);
  assert.ok(transitions.includes('START_PREPARED'), transitions.join(','));
  // No prepared receipt may survive: that is what made two windows sit on one
  // identical Core prompt, each retrying the same irreversible Send.
  assert.strictEqual(api.readStartAuditHandoff(), null);
  assert.strictEqual(api.browserWorkerLease, null);

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_stood_down'), JSON.stringify(log));
});
