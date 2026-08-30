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
