'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function auditRecord(overrides = {}) {
  return {
    version: 1,
    conversationKey: 'c:abc123',
    runId: 'run-bridge-recovery',
    bridgeReceipt: 'receipt-bridge-recovery',
    kind: 'core',
    projectName: 'AUDAPACK',
    profileId: 'quick3',
    profileVersion: '1.0.0',
    waveIndex: 1,
    waveCount: 3,
    completedAt: Date.now(),
    text: [
      'PROJECT_NAME: AUDAPACK',
      'CAMPAIGN_PROFILE: quick3',
      'CAMPAIGN_RUN_ID: run-bridge-recovery',
      'WAVE_ID: core',
      'STATUS: AUDIT_CORE: COMPLETE',
      'TICKETS: 0',
      'HANDOFF: IMPLEMENTATION_AGENT',
      'NO VERIFIED CORE DEFECTS.',
      'CORE_DONE_WHEN: verified'
    ].join('\n'),
    ...overrides
  };
}

test('W4-002: queued Bridge payload keeps its original profile after UI profile switch', () => {
  const { api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });

  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);

  const job = api.readBridgeJob(record.bridgeReceipt);
  assert.strictEqual(job.profileId, 'quick3');
  assert.strictEqual(job.waveCount, 3);

  api.state.auditProfile = 'super10';
  api.autoRuntime.profileId = 'super10';
  const payload = api.bridgeJobRequest(job);

  assert.strictEqual(payload.profile_id, 'quick3');
  assert.strictEqual(payload.profile_version, '1.0.0');
  assert.strictEqual(payload.wave_index, 1);
  assert.strictEqual(payload.wave_count, 3);

  const legacyPayload = api.bridgeJobRequest({
    ...job,
    profileId: '',
    profileVersion: '',
    waveIndex: 0,
    waveCount: 0
  });
  assert.strictEqual(legacyPayload.profile_id, 'quick3', 'legacy queued jobs recover profile from cached handoff text');
  assert.strictEqual(legacyPayload.wave_count, 3);
});

test('W4-002: manual Retry rebuilds compacted permanent job while automatic recovery stays bounded', () => {
  const { api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;

  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);

  const queued = api.readBridgeJob(record.bridgeReceipt);
  assert.strictEqual(api.saveBridgeJob({
    ...queued,
    profileId: '',
    profileVersion: '',
    content: '',
    contentOmitted: true,
    permanent: true,
    errorCode: 'campaign_profile_conflict',
    lastError: 'Profile changed while the Bridge was offline.',
    nextAttemptAt: 0
  }), true);

  assert.strictEqual(api.resetBridgeFailedJobs(''), 0, 'automatic recovery must not loop semantic failures');
  assert.strictEqual(api.readBridgeJob(record.bridgeReceipt).permanent, true);

  const retried = api.retryAllBridgeFailedJobs();
  assert.deepStrictEqual({ ...retried }, { retried: 1, skipped: 0 });

  const rebuilt = api.readBridgeJob(record.bridgeReceipt);
  assert.strictEqual(rebuilt.permanent, false);
  assert.strictEqual(rebuilt.content, record.text);
  assert.strictEqual(rebuilt.contentOmitted, false);
  assert.strictEqual(rebuilt.profileId, 'quick3');
  assert.strictEqual(rebuilt.waveCount, 3);
  assert.strictEqual(rebuilt.errorCode, '');
});

// T-184 replaces the over-broad T83 regression. `completed_wave_immutable` was
// asserted to be "not a failure", which locked in false durability: the Bridge
// emits that code ONLY when the submitted content DIFFERS from the committed
// wave, so it means the audit was refused, not stored. Identical content
// already answers 200 duplicate. Both halves are pinned separately below.

function immutableJob(overrides = {}) {
  const now = Date.now();
  return {
    version: 1,
    jobId: 'job-already-complete',
    receipt: 'performance-abc-123',
    wave: 'performance',
    project: 'VACZEN Calendar (CalendarTask)',
    conversationKey: 'c:vaczen',
    runId: 'acb-real',
    sourceRunId: 'acb-real',
    materialize: false,
    content: 'audit body',
    attempts: 0,
    permanent: false,
    errorCode: '',
    lastError: '',
    createdAt: now,
    updatedAt: now,
    ...overrides
  };
}

test('T-184: identical committed content answers 200 duplicate and marks the record durable', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');

  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);
  const job = api.readBridgeJob(record.bridgeReceipt);

  h.httpResponder = () => ({
    status: 200,
    responseText: JSON.stringify({
      ok: true,
      duplicate: true,
      run_id: record.runId,
      files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']
    })
  });

  const pending = api.deliverBridgeJob(job);
  await h.settle();
  assert.strictEqual(await pending, true);

  assert.strictEqual(api.readBridgeJob(job.jobId), null, 'a proven duplicate retires its delivery job');
  const saved = api.readAuditResultFresh('core', record.conversationKey);
  assert.ok(Number(saved.bridgeSavedAt) > 0, 'an exact duplicate IS durability proof');
  assert.strictEqual(saved.bridgeError, '');
});

test('T-184: completed_wave_immutable is a non-retriable content conflict, never success', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');

  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);
  const job = api.readBridgeJob(record.bridgeReceipt);

  h.httpResponder = () => ({
    status: 409,
    responseText: JSON.stringify({
      ok: false,
      error: {
        code: 'completed_wave_immutable',
        message: "Wave 'core' is already complete in run acb-real; start a fresh run for replacement",
        retriable: false
      }
    })
  });

  const pending = api.deliverBridgeJob(job);
  await h.settle();
  assert.strictEqual(await pending, false, 'a refused write is not a successful delivery');

  const stored = api.readBridgeJob(job.jobId);
  assert.ok(stored, 'the actionable conflict evidence must survive');
  assert.strictEqual(stored.permanent, true);
  assert.strictEqual(stored.errorCode, 'completed_wave_immutable');

  const after = api.readAuditResultFresh('core', record.conversationKey);
  assert.strictEqual(Number(after.bridgeSavedAt) || 0, 0, 'a rejected wave is never durable');
  assert.ok(after.bridgeError, 'the operator-visible error must not be cleared');
  assert.strictEqual(after.text, record.text, 'cached audit text is never discarded');

  const log = api.readBridgeDiagnosticLog();
  assert.strictEqual(log.some(entry => entry.event === 'job_already_complete'), false);
  assert.ok(log.some(entry => entry.event === 'job_failed'), JSON.stringify(log));
});
