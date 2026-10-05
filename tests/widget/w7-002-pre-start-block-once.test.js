'use strict';

// T-248 Milestones B + I. A deterministic DOM incompatibility must not burn the
// pre-start retry budget six times and then report one opaque
// `pre-start retries exhausted: file-injection-rejected` row per project. It
// blocks once, with the exact reason and the observed composer shape.

const test = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function job(archiveFile) {
  return {
    dispatch_id: 'dsp-2480aabbccddeeff',
    worker_id: 'audapack-managed-1-1',
    lease_id: 'lease-248',
    project_id: 'limisaw',
    project_name: '_LIMISAW',
    campaign_run_id: '',
    archive_filename: archiveFile.name,
    archive_size: archiveFile.size
  };
}

function recorder() {
  const transitions = [];
  return {
    transitions,
    transition: async (state, payload = {}) => {
      transitions.push({ state, payload });
      return { ok: true };
    }
  };
}

// A composer with NO file input at all and no attachment control to open: the
// current ChatGPT build offers no supported upload surface.
function composerWithoutUploadSurface(h) {
  const { form, upload, send } = composerFixture(h);
  upload.remove();
  return { form, send };
}

test('T-248-20: an unsupported upload surface BLOCKS once with the exact reason', async () => {
  const { h, api } = setup();
  composerWithoutUploadSurface(h);
  const archiveFile = { name: '_LIMISAW_27.09.26-T18-14-32.zip', size: 6765528 };
  const record = recorder();

  const ok = await api.browserWorkerConsume(job(archiveFile), {
    transition: record.transition,
    fetchArtifact: async () => ({ ok: true, file: archiveFile })
  });
  await h.settle();

  assert.strictEqual(ok, false);
  const states = record.transitions.map(item => item.state);
  assert.deepStrictEqual(states, ['ARTIFACT_FETCHED', 'BLOCKED'],
    'one structural refusal, never a RETRYABLE storm across the lane');
  const blocked = record.transitions[1].payload;
  assert.strictEqual(blocked.error, 'upload-input-unavailable');
  assert.match(blocked.detail, /file_inputs=0/);
  assert.match(blocked.detail, /verdict=upload-input-unavailable/);
});

test('T-248-21: the Bridge receives a short code plus a bounded detail, never a blob as the code', async () => {
  const { h, api } = setup();
  composerWithoutUploadSurface(h);
  const archiveFile = { name: 'Wintage_27.09.26-T18-14-28.zip', size: 10198378 };
  const record = recorder();

  await api.browserWorkerConsume(job(archiveFile), {
    transition: record.transition,
    fetchArtifact: async () => ({ ok: true, file: archiveFile })
  });
  await h.settle();

  const payload = record.transitions.at(-1).payload;
  assert.strictEqual(payload.error, 'upload-input-unavailable');
  assert.ok(payload.detail.length < 500, `detail was ${payload.detail.length} chars`);
  assert.ok(!payload.detail.includes('conversation'), 'the detail carries structure only');
});

test('T-248-22: a missing composer root is its own code, not a file-injection failure', async () => {
  const { h, api } = setup();
  const archiveFile = { name: '_ZAICODE_27.09.26-T18-13-29.zip', size: 1560455 };
  const record = recorder();

  const ok = await api.browserWorkerConsume(job(archiveFile), {
    transition: record.transition,
    fetchArtifact: async () => ({ ok: true, file: archiveFile })
  });
  await h.settle();

  assert.strictEqual(ok, false);
  const states = record.transitions.map(item => item.state);
  assert.deepStrictEqual(states, ['ARTIFACT_FETCHED', 'BLOCKED']);
  assert.strictEqual(record.transitions.at(-1).payload.error, 'composer-root-unavailable');
});

test('T-248-23: a legacy seam with no upload input reports upload-input-unavailable, not file-injection-rejected', async () => {
  const { api } = setup();
  const archiveFile = { name: 'FastPrompter_27.09.26-T18-13-47.zip', size: 80215308 };
  const record = recorder();

  const ok = await api.browserWorkerConsume(job(archiveFile), {
    transition: record.transition,
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => null,
    composerRoot: () => ({ contains: () => true }),
    injectFiles: () => true
  });

  assert.strictEqual(ok, false);
  assert.deepStrictEqual(record.transitions.map(item => item.state), ['ARTIFACT_FETCHED', 'BLOCKED']);
  assert.strictEqual(record.transitions.at(-1).payload.error, 'upload-input-unavailable');
});

test('T-248-24: a transient refusal stays RETRYABLE with its own code', async () => {
  const { h, api } = setup();
  const fixture = composerFixture(h);
  const archiveFile = { name: '__SAIMAIL___27.09.26-T18-14-24.zip', size: 15856974 };
  const record = recorder();

  const ok = await api.browserWorkerConsume(job(archiveFile), {
    transition: record.transition,
    fetchArtifact: async () => ({ ok: true, file: archiveFile }),
    uploadInput: () => fixture.upload,
    composerRoot: () => fixture.form,
    injectFiles: () => false
  });
  await h.settle();

  assert.strictEqual(ok, false);
  const states = record.transitions.map(item => item.state);
  assert.deepStrictEqual(states, ['ARTIFACT_FETCHED', 'RETRYABLE'],
    'a refused assignment is worth another attempt; an unsupported build is not');
  assert.strictEqual(record.transitions.at(-1).payload.error, 'file-injection-rejected');
});
