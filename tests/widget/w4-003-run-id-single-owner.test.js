'use strict';

// W4-003: the audit record is the single owner of the run id once a handoff
// is captured. The widget must never let a freshly-armed runtime id overwrite
// a record that already carries a durable run id, because the on-disk content
// was captured under the previous id and the Bridge v3 contract would refuse
// a payload whose run_id disagrees with the content's CAMPAIGN_RUN_ID header.
//
// Regression paths:
//   1. enqueueBridgeAuditRecord refuses to enqueue a record whose record.runId
//      disagrees with the durable record already on disk.
//   2. bridgeJobRequest emits payload.run_id that matches the CAMPAIGN_RUN_ID
//      header embedded in the queued content.
//   3. deliverBridgeJob refuses to POST a payload whose content's
//      CAMPAIGN_RUN_ID disagrees with the queued run_id.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function auditRecord(overrides = {}) {
  return {
    version: 1,
    conversationKey: 'c:w4-003',
    runId: 'run-canonical-001',
    bridgeReceipt: 'rcpt-w4-003-core',
    bridgeMaterializeReceipt: '',
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
      'CAMPAIGN_RUN_ID: run-canonical-001',
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

test('W4-003: enqueue preserves durable run id and rejects lineage-divergent re-arm', () => {
  const { api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });

  // 1. Capture a complete Core handoff under run-canonical-001.
  const first = auditRecord();
  assert.strictEqual(api.writeAuditResult(first), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(first, { deferFlush: true }), true);
  const firstJob = api.readBridgeJob(first.bridgeReceipt);
  assert.strictEqual(firstJob.runId, 'run-canonical-001');

  // 2. Runtime re-arms (simulates Reset / new Core). The audit record on disk
  //    already carries run-canonical-001, and the content body was captured
  //    under that id. A re-enqueue with a different runId must be refused so
  //    the Bridge never sees a payload whose run_id disagrees with the
  //    content's CAMPAIGN_RUN_ID header.
  const divergent = auditRecord({ runId: 'run-divergent-999' });
  assert.strictEqual(
    api.enqueueBridgeAuditRecord(divergent, { deferFlush: true }),
    false,
    'enqueue must refuse a record whose runId disagrees with the durable on-disk record'
  );
  assert.strictEqual(
    api.readAuditResultFresh('core', 'c:w4-003').runId,
    'run-canonical-001',
    'durable audit record must keep its original run id'
  );
});

test('W4-003: bridgeJobRequest payload.run_id matches content CAMPAIGN_RUN_ID', () => {
  const { api } = setup();
  api.state.auditProfile = 'quick3';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });

  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);

  const job = api.readBridgeJob(record.bridgeReceipt);
  const payload = api.bridgeJobRequest(job);
  assert.strictEqual(payload.run_id, 'run-canonical-001');
  assert.ok(
    /^CAMPAIGN_RUN_ID:\s*run-canonical-001\s*$/m.test(payload.content),
    'queued content must carry the same CAMPAIGN_RUN_ID header the payload promises'
  );
  assert.strictEqual(payload.run_id, 'run-canonical-001');
});

test('W4-003: deliverBridgeJob refuses payload with mismatching CAMPAIGN_RUN_ID', async () => {
  const { api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });

  // Hand-build a queued job whose queued runId disagrees with the content
  // header. This is the exact defect that the runtime-error report described
  // and that the new contract guard must catch before any HTTP POST.
  const record = auditRecord();
  assert.strictEqual(api.writeAuditResult(record), true);
  const tampered = {
    version: 1,
    jobId: 'rcpt-tampered',
    receipt: 'rcpt-tampered',
    runId: 'run-canonical-001',
    sourceRunId: 'run-canonical-001',
    deliveryRunId: 'run-canonical-001',
    conversationKey: record.conversationKey,
    conversationId: 'w4-003',
    project: 'AUDAPACK',
    wave: 'core',
    profileId: 'quick3',
    profileVersion: '1.0.0',
    waveIndex: 1,
    waveCount: 3,
    completedAt: Date.now(),
    content: record.text.replace(
      'CAMPAIGN_RUN_ID: run-canonical-001',
      'CAMPAIGN_RUN_ID: run-other-456'
    ),
    attempts: 0,
    nextAttemptAt: Date.now(),
    permanent: false,
    errorCode: '',
    lastError: '',
    materialize: false,
    staged: false,
    inFlightAt: 0,
    deliveredAwaitingAck: false,
    deliveredData: null,
    createdAt: Date.now(),
    updatedAt: Date.now()
  };
  assert.strictEqual(api.saveBridgeJob(tampered, { signal: false }), true);

  const delivered = await api.deliverBridgeJob(api.readBridgeJob(tampered.jobId));
  assert.strictEqual(delivered, false, 'tampered payload must be refused');

  const flagged = api.readBridgeJob(tampered.jobId);
  assert.strictEqual(flagged.permanent, true);
  assert.strictEqual(flagged.errorCode, 'run_id_mismatch');
});
