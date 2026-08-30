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
