'use strict';

// PERF-003 (audit/4.md): Bridge-job storage stays bounded and the membership
// index is only rewritten on create/delete. Permanent invalid_auth jobs are
// compacted against a proven durable audit record, rebuilt byte-identically on
// token replacement, and never lose their sole surviving copy.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

const BRIDGE_JOB_INDEX_KEY = 'ai_chatbuttons_bridge_job_index_v1';

function auditRecord(overrides = {}) {
  return {
    version: 1,
    conversationKey: 'c:abc123',
    runId: 'run-perf003',
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
      `CAMPAIGN_RUN_ID: ${overrides.runId || 'run-perf003'}`,
      'WAVE_ID: core',
      'STATUS: AUDIT_CORE: COMPLETE',
      'TICKETS: 0',
      'HANDOFF: IMPLEMENTATION_AGENT',
      'x'.repeat(1500)
    ].join('\n'),
    ...overrides
  };
}

function auth401Responder() {
  return {
    status: 401,
    responseText: JSON.stringify({
      ok: false,
      error: { code: 'invalid_auth', message: 'replacement token needed', retriable: false }
    })
  };
}

function deleteStoredAuditResult(h, conversationKey) {
  for (const [key, raw] of Array.from(h.gmStore.entries())) {
    try {
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && parsed.version === 1 && parsed.conversationKey === conversationKey && parsed.kind) {
        h.gmStore.delete(key);
      }
    } catch (_) { /* not a JSON record */ }
  }
}

function ready(api) {
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
}

test('PERF-003: state updates to an already-indexed job never rewrite the membership index', () => {
  const { h, api } = setup();
  const job = {
    version: 1,
    jobId: 'idx-1',
    receipt: 'idx-1',
    conversationKey: 'c:idx',
    wave: 'core',
    runId: 'r-idx',
    sourceRunId: 'r-idx',
    profileId: 'quick3',
    content: 'audit body',
    attempts: 0,
    createdAt: Date.now(),
    updatedAt: Date.now()
  };
  assert.strictEqual(api.saveBridgeJob(job, { signal: false }), true);

  // Create wrote the job key once plus one index membership write.
  const indexAfterCreate = api.storage.gmGet(BRIDGE_JOB_INDEX_KEY, '');
  assert.deepStrictEqual(JSON.parse(indexAfterCreate), ['idx-1']);

  h.counters.gmSet = 0;
  for (let i = 0; i < 200; i += 1) {
    assert.strictEqual(api.saveBridgeJob({ ...job, lastError: `update ${i}`, updatedAt: Date.now() + i }, { signal: false }), true);
  }
  assert.strictEqual(h.counters.gmSet, 200,
    '200 state updates must write exactly 200 job payloads -- never the index again');
  assert.strictEqual(api.storage.gmGet(BRIDGE_JOB_INDEX_KEY, ''), indexAfterCreate,
    'index bytes must be untouched by pure state updates');
  assert.strictEqual(api.readBridgeJob('idx-1').lastError, 'update 199');
});

test('PERF-003: a 401 permanent failure compacts only against a proven canonical record', async () => {
  const { h, api } = setup();
  ready(api);
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');

  // Case A: canonical record proves the identity -> content compacts.
  const recordA = auditRecord({ conversationKey: 'c:auth-c', runId: 'run-c', bridgeReceipt: 'r-auth-c' });
  assert.strictEqual(api.writeAuditResult(recordA), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(recordA, { deferFlush: true }), true);
  const jobA = api.readBridgeJob('r-auth-c');
  assert.ok(jobA, 'job must be queued');

  h.httpResponder = auth401Responder;
  const pendingA = api.deliverBridgeJob(jobA);
  await h.settle();
  await pendingA;

  const storedA = api.readBridgeJob('r-auth-c');
  assert.strictEqual(storedA.permanent, true);
  assert.strictEqual(storedA.errorCode, 'invalid_auth');
  assert.strictEqual(storedA.contentOmitted, true);
  assert.strictEqual(storedA.content, '');
  // The durable canonical record still holds the full body.
  assert.strictEqual(api.readAuditResultFresh('core', 'c:auth-c').text, recordA.text);
  // The stored job is metadata-sized, not a second full copy.
  const rawA = api.storage.gmGet(api.constants.BRIDGE_JOB_PREFIX + 'r-auth-c', '');
  assert.ok(rawA.length < recordA.text.length, `job payload (${rawA.length}B) must be smaller than the duplicated body (${recordA.text.length}B)`);

  // Case B: no canonical record -> full content is kept as the sole copy.
  const recordB = auditRecord({ conversationKey: 'c:auth-s', runId: 'run-s', bridgeReceipt: 'r-auth-s' });
  assert.strictEqual(api.writeAuditResult(recordB), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(recordB, { deferFlush: true }), true);
  deleteStoredAuditResult(h, 'c:auth-s');
  assert.strictEqual(api.readAuditResultFresh('core', 'c:auth-s'), null, 'canonical must be gone for case B');

  const pendingB = api.deliverBridgeJob(api.readBridgeJob('r-auth-s'));
  await h.settle();
  await pendingB;

  const storedB = api.readBridgeJob('r-auth-s');
  assert.strictEqual(storedB.permanent, true);
  assert.strictEqual(storedB.errorCode, 'invalid_auth');
  assert.strictEqual(storedB.contentOmitted, undefined, 'no canonical proof means the body is never marked omitted');
  assert.strictEqual(storedB.content, recordB.text, 'sole surviving copy must never be dropped');

  // Case C: canonical exists but run identity mismatches -> content retained.
  const mismatchJob = {
    version: 1,
    jobId: 'r-auth-m',
    receipt: 'r-auth-m',
    projectId: 'p-x',
    conversationKey: 'c:auth-c',
    wave: 'core',
    runId: 'run-mismatch',
    sourceRunId: 'run-mismatch',
    profileId: 'quick3',
    content: 'MISMATCH BODY MUST SURVIVE',
    attempts: 0,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    nextAttemptAt: 0,
    inFlightAt: 0,
    permanent: false
  };
  assert.strictEqual(api.saveBridgeJob(mismatchJob, { signal: false }), true);
  const pendingC = api.deliverBridgeJob(mismatchJob);
  await h.settle();
  await pendingC;

  const storedC = api.readBridgeJob('r-auth-m');
  assert.strictEqual(storedC.permanent, true);
  assert.strictEqual(storedC.contentOmitted, undefined, 'a mismatched identity is never compacted');
  assert.strictEqual(storedC.content, 'MISMATCH BODY MUST SURVIVE',
    'an unprovable identity must keep its only content copy');
});

test('PERF-003: saving a replacement token rebuilds compacted invalid_auth jobs byte-identically', () => {
  const { api } = setup();
  ready(api);

  const record = auditRecord({ conversationKey: 'c:auth-r', runId: 'run-r', bridgeReceipt: 'r-auth-r' });
  assert.strictEqual(api.writeAuditResult(record), true);
  assert.strictEqual(api.enqueueBridgeAuditRecord(record, { deferFlush: true }), true);
  // Compact it exactly as a 401 permanent failure does.
  const latest = api.readBridgeJob('r-auth-r');
  assert.strictEqual(api.saveBridgeJob({
    ...latest,
    content: '',
    contentOmitted: true,
    permanent: true,
    errorCode: 'invalid_auth',
    lastError: 'replacement token needed',
    nextAttemptAt: 0,
    inFlightAt: 0
  }), true);

  // Token replacement calls resetBridgeFailedJobs('invalid_auth').
  assert.strictEqual(api.resetBridgeFailedJobs('invalid_auth'), 1);
  const rebuilt = api.readBridgeJob('r-auth-r');
  assert.strictEqual(rebuilt.permanent, false);
  assert.strictEqual(rebuilt.contentOmitted, false);
  assert.strictEqual(rebuilt.content, record.text, 'rebuilt payload must be byte-identical');
  assert.strictEqual(rebuilt.attempts, 0);
  assert.strictEqual(rebuilt.errorCode, '');

  // The queue can now deliver the restored payload.
  const payload = api.bridgeJobRequest(rebuilt);
  assert.strictEqual(payload.content, record.text);
});

test('PERF-003: manual retry of an unprovable compacted job stays permanent instead of sending empty', () => {
  const { api } = setup();
  ready(api);

  const orphan = {
    version: 1,
    jobId: 'orphan-1',
    receipt: 'orphan-1',
    conversationKey: 'c:orphan',
    wave: 'core',
    runId: 'run-orphan',
    sourceRunId: 'run-orphan',
    profileId: 'quick3',
    content: '',
    contentOmitted: true,
    permanent: true,
    errorCode: 'http_503',
    attempts: 0,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    nextAttemptAt: 0,
    inFlightAt: 0
  };
  assert.strictEqual(api.saveBridgeJob(orphan), true);

  const outcome = api.retryAllBridgeFailedJobs();
  assert.deepStrictEqual({ ...outcome }, { retried: 0, skipped: 1 });
  assert.strictEqual(api.readBridgeJob('orphan-1').permanent, true,
    'an unprovable compacted job must not be republished as an empty body');
});

test('PERF-003: permanent job metadata is bounded and only canonical-backed jobs are pruned', () => {
  const { api } = setup();
  ready(api);

  const max = api.constants.BRIDGE_PERMANENT_JOBS_MAX;
  assert.strictEqual(max, 400, 'explicit permanent-job retention bound must exist');
  const now = Date.now();
  const total = max + 20;

  for (let i = 0; i < total; i += 1) {
    const conversationKey = `c:bound-${i}`;
    const record = auditRecord({ conversationKey, runId: 'run-bound', profileId: 'quick3' });
    assert.strictEqual(api.writeAuditResult(record), true);
    assert.strictEqual(api.saveBridgeJob({
      version: 1,
      jobId: `bound-${i}`,
      receipt: `bound-${i}`,
      conversationKey,
      wave: 'core',
      runId: 'run-bound',
      sourceRunId: 'run-bound',
      profileId: 'quick3',
      content: '',
      contentOmitted: true,
      permanent: true,
      errorCode: 'invalid_auth',
      attempts: 0,
      createdAt: now + i,
      updatedAt: now + i,
      nextAttemptAt: 0,
      inFlightAt: 0
    }, { signal: false }), true);
  }

  // A sole surviving copy with no canonical backing must survive any prune.
  const sole = {
    version: 1,
    jobId: 'bound-sole',
    receipt: 'bound-sole',
    conversationKey: 'c:sole',
    wave: 'core',
    runId: 'run-sole',
    sourceRunId: 'run-sole',
    profileId: 'quick3',
    content: 'THE ONLY SURVIVING COPY OF AN UNDELIVERED AUDIT',
    contentOmitted: false,
    permanent: true,
    errorCode: 'invalid_auth',
    attempts: 0,
    createdAt: now + total,
    updatedAt: now + total,
    nextAttemptAt: 0,
    inFlightAt: 0
  };
  // Default signal: clears the startup-empty queue cache so the prune below
  // enumerates the freshly-written jobs.
  assert.strictEqual(api.saveBridgeJob(sole), true);

  // total + sole = max + 21 permanent jobs -> the 21-job overshoot of
  // canonical-backed jobs is reclaimed and the count lands exactly on the
  // bound, with the sole unproven copy retained among the survivors.
  const pruned = api.pruneBridgePermanentJobs();
  assert.strictEqual(pruned, 21, 'exactly the overshoot of canonical-backed jobs is reclaimed');

  const survivors = api.bridgeQueueStats('').jobs.filter(job => job.permanent);
  assert.strictEqual(survivors.length, max, 'the count returns exactly to the bound');

  const soleSurvivor = api.readBridgeJob('bound-sole');
  assert.ok(soleSurvivor);
  assert.strictEqual(soleSurvivor.content, 'THE ONLY SURVIVING COPY OF AN UNDELIVERED AUDIT',
    'the sole unproven copy is never an eviction victim');

  // Oldest canonical-backed jobs were evicted first, but their durable audit
  // records remain the recoverable source of the body.
  assert.strictEqual(api.readBridgeJob('bound-0'), null);
  assert.strictEqual(api.readBridgeJob('bound-20'), null);
  assert.ok(api.readAuditResultFresh('core', 'c:bound-0'),
    'the durable audit record of a pruned job must still exist');
  assert.ok(api.readBridgeJob(`bound-${total - 1}`), 'the newest canonical-backed job survives');

  for (const survivor of survivors) {
    if (survivor.jobId === 'bound-sole') continue;
    assert.strictEqual(survivor.contentOmitted, true, 'surviving canonical-backed jobs stay compacted');
    assert.strictEqual(survivor.content, '');
  }
});

test('PERF-003: unprovable sole copies survive pruning even past the bound', () => {
  const { api } = setup();
  ready(api);

  const max = api.constants.BRIDGE_PERMANENT_JOBS_MAX;
  const now = Date.now();

  // max + 5 permanent jobs that are the ONLY copy of their undelivered body:
  // no canonical record exists, so none may be pruned even though the count
  // exceeds the retention bound.
  for (let i = 0; i < max + 5; i += 1) {
    const job = {
      version: 1,
      jobId: `sole-${i}`,
      receipt: `sole-${i}`,
      conversationKey: `c:sole-only-${i}`,
      wave: 'core',
      runId: `run-sole-${i}`,
      sourceRunId: `run-sole-${i}`,
      profileId: 'quick3',
      content: `THE ONLY COPY ${i}`,
      contentOmitted: false,
      permanent: true,
      errorCode: 'invalid_auth',
      attempts: 0,
      createdAt: now + i,
      updatedAt: now + i,
      nextAttemptAt: 0,
      inFlightAt: 0
    };
    const last = i === max + 4;
    assert.strictEqual(api.saveBridgeJob(job, last ? {} : { signal: false }), true);
  }

  assert.strictEqual(api.pruneBridgePermanentJobs(), 0,
    'pruning must never discard the only surviving copy of an undelivered payload');

  const survivors = api.bridgeQueueStats('').jobs.filter(job => job.permanent);
  assert.strictEqual(survivors.length, max + 5,
    'unprovable evidence stays over the bound rather than being erased');
  assert.strictEqual(api.readBridgeJob('sole-0').content, 'THE ONLY COPY 0');
  assert.strictEqual(api.readBridgeJob(`sole-${max + 4}`).content, `THE ONLY COPY ${max + 4}`);
});
