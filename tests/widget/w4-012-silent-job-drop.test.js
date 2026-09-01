'use strict';

// A job the widget refuses must be handed back, never dropped in silence.
// /v1/browser/poll moves QUEUED -> LEASED atomically before it answers, so a
// bare `return false` left the job leased to a window that would never touch
// it. Because every poll of that same worker renewed the lease, the job never
// expired either: it sat LEASED for as long as the window stayed open, while
// /v1/browser/status reported that very worker FREE and clean.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function consumeWith(api, jobOverrides, released) {
  return api.browserWorkerConsume({
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'worker-1',
    lease_id: 'lease-1',
    project_id: 'smart_vac_cleaner',
    project_name: 'Smart VAC Cleaner',
    archive_filename: 'SMART_VAC.zip',
    archive_size: 99,
    ...jobOverrides
  }, {
    releaseJob: async (job, reason) => {
      released.push({ dispatch_id: job.dispatch_id, reason });
      return { ok: true };
    },
    transition: async () => ({ ok: true }),
    fetchArtifact: async () => ({ ok: true, file: { name: 'SMART_VAC.zip', size: 99 } }),
    uploadInput: () => ({}),
    composerRoot: () => ({ contains: () => true }),
    injectFiles: () => true,
    waitForAttachment: async () => ({ ok: true, reason: 'exact-match', observedNames: ['SMART_VAC.zip'] }),
    startAudit: async () => ({ ok: true, sent: true })
  });
}

test('W12: a path-shaped archive name is handed back, not dropped', async () => {
  const { api } = setup();
  const released = [];
  // A Windows path, spelled with real backslashes rather than escapes.
  const winPath = ['C:', 'packs', 'SMART_VAC.zip'].join(String.fromCharCode(92));
  const ok = await consumeWith(api, { archive_filename: winPath }, released);

  assert.strictEqual(ok, false);
  assert.strictEqual(released.length, 1, 'the job must be released back to the queue');
  assert.strictEqual(released[0].dispatch_id, 'dsp-0123456789abcdef');
  assert.strictEqual(released[0].reason, 'archive-filename-not-a-bare-name');
  assert.strictEqual(api.browserWorkerLease, null, 'no lease may be taken for a refused job');
});

test('W12: a non-zip archive name is handed back, not dropped', async () => {
  const { api } = setup();
  const released = [];
  const ok = await consumeWith(api, { archive_filename: 'SMART_VAC.tar' }, released);

  assert.strictEqual(ok, false);
  assert.strictEqual(released.length, 1);
  assert.strictEqual(released[0].reason, 'archive-filename-not-a-zip');
});

test('W12: an empty archive name is handed back, not dropped', async () => {
  const { api } = setup();
  const released = [];
  const ok = await consumeWith(api, { archive_filename: '' }, released);

  assert.strictEqual(ok, false);
  assert.strictEqual(released.length, 1);
  assert.strictEqual(released[0].reason, 'archive-filename-not-a-bare-name');
});

test('W12: a job with no lease to hand back is not released', async () => {
  // Nothing to release against: the Bridge never took a lease for this, so a
  // release POST would be unauthenticated noise.
  const { api } = setup();
  const released = [];
  const ok = await consumeWith(api, { lease_id: '', archive_filename: 'bad.tar' }, released);

  assert.strictEqual(ok, false);
  assert.strictEqual(released.length, 0);
});

test('W12: a well-formed job still consumes normally', async () => {
  const { api } = setup();
  const released = [];
  const ok = await consumeWith(api, {}, released);

  assert.strictEqual(released.length, 0, 'a runnable job must never be released');
  assert.ok(ok !== false || api.browserWorkerLease, 'the lease must be taken for a runnable job');
});
