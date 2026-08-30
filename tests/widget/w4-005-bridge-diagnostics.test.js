'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function failedJob(overrides = {}) {
  const now = Date.now();
  return {
    version: 1,
    jobId: 'receipt-diag-001',
    receipt: 'receipt-diag-001',
    runId: 'run-diag-001',
    deliveryRunId: 'run-diag-001',
    conversationKey: 'c:diag',
    project: 'AUDAPACK',
    wave: 'core',
    attempts: 3,
    permanent: true,
    errorCode: 'run_id_mismatch',
    lastError: "Payload run_id 'wrong' does not match content CAMPAIGN_RUN_ID 'right'.",
    createdAt: now - 1000,
    updatedAt: now,
    ...overrides
  };
}

test('W4-005: Bridge diagnostics expose the persisted failure cause without secrets or audit content', () => {
  const { api } = setup();
  const job = failedJob();
  api.storage.gmSet('ai_chatbuttons_bridge_token_v1', 'TOP-SECRET-TOKEN');
  assert.strictEqual(api.saveBridgeJob(job), true);
  assert.strictEqual(api.appendBridgeDiagnostic('job_failed', {
    severity: 'error',
    status: 409,
    code: job.errorCode,
    message: job.lastError,
    job
  }), true);

  const text = api.bridgeDiagnosticsText();
  assert.match(text, /queue_total=1 queued=0 failed=1/);
  assert.match(text, /\[FAILED\].*code=run_id_mismatch attempts=3/);
  assert.match(text, /project=AUDAPACK wave=core run_id=run-diag-001/);
  assert.match(text, /receipt=receipt-diag-001/);
  assert.match(text, /cause=Payload run_id 'wrong' does not match content CAMPAIGN_RUN_ID 'right'\./);
  assert.match(text, /token=stored/);
  assert.doesNotMatch(text, /TOP-SECRET-TOKEN/);
  assert.doesNotMatch(text, /PROJECT_NAME:/);
});

test('W4-005: Bridge diagnostic history is durable, bounded, and collapses retry spam', () => {
  const { api } = setup();
  const job = failedJob();

  assert.strictEqual(api.appendBridgeDiagnostic('job_retry_scheduled', {
    severity: 'warning', code: 'offline', message: 'Bridge is not reachable.', job
  }), true);
  assert.strictEqual(api.appendBridgeDiagnostic('job_retry_scheduled', {
    severity: 'warning', code: 'offline', message: 'Bridge is not reachable.', job
  }), true);
  let entries = api.readBridgeDiagnosticLog();
  assert.strictEqual(entries.length, 1);
  assert.strictEqual(entries[0].repeats, 2);

  for (let index = 0; index < api.constants.BRIDGE_DIAGNOSTIC_LOG_MAX + 5; index += 1) {
    assert.strictEqual(api.appendBridgeDiagnostic(`event_${index}`, {
      severity: 'info', message: `bounded event ${index}`
    }), true);
  }
  entries = api.readBridgeDiagnosticLog();
  assert.strictEqual(entries.length, api.constants.BRIDGE_DIAGNOSTIC_LOG_MAX);
  assert.strictEqual(entries.at(-1).event, `event_${api.constants.BRIDGE_DIAGNOSTIC_LOG_MAX + 4}`);
  assert.ok(api.storage.gmGet(api.constants.BRIDGE_DIAGNOSTIC_LOG_KEY, '').includes('bounded event'));
});
