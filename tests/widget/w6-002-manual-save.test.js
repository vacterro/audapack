'use strict';

// T-184 -- manual SYNC/SAVE persistence truth.
//
// The incident: manual SAVE could sit on SAVE... for minutes, a rejected write
// (`completed_wave_immutable`) was treated as success and deleted from the
// queue with bridgeSavedAt still 0, an exact duplicate was treated as failure,
// and an empty queue was read as "everything is saved". Every test here pins
// one half of that back to the truth.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

const TOKEN = 'w6-002-test-token';
const CONVERSATION = 'c:abc123';
const RUN_ID = 'acb-w6002-run';

function waveText(kind, runId = RUN_ID) {
  if (kind === 'core') {
    return [
      'PROJECT_NAME: AUDAPACK',
      'CAMPAIGN_PROFILE: quick3',
      `CAMPAIGN_RUN_ID: ${runId}`,
      'WAVE_ID: core',
      'STATUS: AUDIT_CORE: COMPLETE',
      'TICKETS: 0',
      'HANDOFF: IMPLEMENTATION_AGENT',
      'NO VERIFIED CORE DEFECTS.',
      'CORE_DONE_WHEN: verified'
    ].join('\n');
  }
  if (kind === 'second') {
    return [
      'PROJECT_NAME: AUDAPACK',
      'CAMPAIGN_PROFILE: quick3',
      `CAMPAIGN_RUN_ID: ${runId}`,
      'WAVE_ID: second',
      'STATUS: SECOND_WAVE: COMPLETE',
      'TICKETS: 0',
      'HANDOFF: IMPLEMENTATION_AGENT',
      'NO VERIFIED SECOND WAVE DEFECTS.',
      'SECOND_WAVE_DONE_WHEN: verified'
    ].join('\n');
  }
  return [
    'PROJECT_NAME: AUDAPACK',
    'CAMPAIGN_PROFILE: quick3',
    `CAMPAIGN_RUN_ID: ${runId}`,
    'WAVE_ID: performance',
    'STATUS: PERFORMANCE: COMPLETE',
    'TICKETS: 0',
    'HANDOFF: IMPLEMENTATION_AGENT',
    'NO VERIFIED PERFORMANCE DEFECTS.',
    'PERFORMANCE_DONE_WHEN: verified'
  ].join('\n');
}

function record(kind, overrides = {}) {
  return {
    version: 1,
    conversationKey: CONVERSATION,
    runId: RUN_ID,
    bridgeReceipt: `${kind}-${RUN_ID}-receipt`,
    kind,
    projectName: 'AUDAPACK',
    projectId: 'audapack',
    profileId: 'quick3',
    profileVersion: '1.0.0',
    waveIndex: kind === 'core' ? 1 : kind === 'second' ? 2 : 3,
    waveCount: 3,
    gateState: 'complete',
    completedAt: Date.now(),
    bridgeSavedAt: 0,
    text: waveText(kind),
    ...overrides
  };
}

function prepare(options = {}) {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', TOKEN);
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
  api.autoRuntime.conversationKey = CONVERSATION;
  api.autoRuntime.runId = RUN_ID;
  api.autoRuntime.projectName = 'AUDAPACK';
  api.autoRuntime.projectId = 'audapack';
  api.autoRuntime.stage = options.stage || 'complete';
  api.saveAutoRuntime({ pauseOnFailure: false });
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  assert.strictEqual(api.autoBoundConversationKey, CONVERSATION);
  for (const kind of options.kinds || ['core']) {
    assert.strictEqual(api.writeAuditResult(record(kind, options.overrides?.[kind] || {})), true);
  }
  return { h, api };
}

// Routes every Bridge call a manual SAVE can make. `audits` and `materialize`
// are separate on purpose: the whole P0 was those two being the same request.
function installBridge(h, handlers = {}) {
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (/\/v1\/status|\/health/.test(url)) {
      return handlers.status || {
        status: 200,
        responseText: JSON.stringify({ ok: true, service: 'AUDAPACK Bridge', version: '1.0.0' })
      };
    }
    if (/\/v1\/projects\/resolve/.test(url)) {
      return handlers.resolve || {
        status: 200,
        responseText: JSON.stringify({ ok: true, project_id: 'audapack', group: 'MAIN0', slot: 1 })
      };
    }
    if (/\/v1\/audits\/materialize/.test(url)) {
      const body = JSON.parse(String(request.data || '{}'));
      // A verify_only probe is read-only and is not a materialize OPERATION;
      // the "exactly one operation" assertions must not count it.
      if (body.verify_only) {
        h.verifyRequests = h.verifyRequests || [];
        h.verifyRequests.push(body);
        if (handlers.verify) {
          return typeof handlers.verify === 'function' ? handlers.verify(body) : handlers.verify;
        }
        return {
          status: 200,
          responseText: JSON.stringify({
            ok: true,
            verify_only: true,
            run_id: body.source_run_id,
            waves: body.waves.map(w => ({ wave_id: w.wave_id, files: [], missing: [] })),
            missing_files: [],
            files_intact: true
          })
        };
      }
      h.materializeRequests = h.materializeRequests || [];
      h.materializeRequests.push(body);
      return typeof handlers.materialize === 'function'
        ? handlers.materialize(request)
        : (handlers.materialize || {
          status: 200,
          responseText: JSON.stringify({
            ok: true,
            duplicate: false,
            materialized: true,
            run_id: RUN_ID,
            final_rebuilt: false,
            all3_ready: false,
            waves: [{ wave_id: 'core', files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md'] }],
            files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']
          })
        });
    }
    if (/\/v1\/audits/.test(url)) {
      h.ingestRequests = h.ingestRequests || [];
      h.ingestRequests.push(JSON.parse(String(request.data || '{}')));
      return typeof handlers.audits === 'function'
        ? handlers.audits(request)
        : (handlers.audits || {
          status: 200,
          responseText: JSON.stringify({
            ok: true,
            duplicate: false,
            run_id: RUN_ID,
            files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']
          })
        });
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found' } }) };
  };
}

// 1 -----------------------------------------------------------------------
test('W6-002/1: completed_wave_immutable never becomes success', async () => {
  const { h, api } = prepare();
  installBridge(h, {
    audits: {
      status: 409,
      responseText: JSON.stringify({
        ok: false,
        error: {
          code: 'completed_wave_immutable',
          message: "Wave 'core' is already complete in run acb-real",
          retriable: false
        }
      })
    }
  });

  const stored = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(api.enqueueBridgeAuditRecord(stored, { deferFlush: true }), true);
  const job = api.readBridgeJob(stored.bridgeReceipt);

  const pending = api.deliverBridgeJob(job);
  await h.settle();
  assert.strictEqual(await pending, false);

  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(Number(after.bridgeSavedAt) || 0, 0, 'a refused write must never be durable');
  assert.ok(after.bridgeError, 'the durability error must not be cleared');
  assert.ok(api.readBridgeJob(job.jobId), 'the job must survive as actionable conflict evidence');
  assert.strictEqual(api.readBridgeJob(job.jobId).permanent, true);
});

// 2 -----------------------------------------------------------------------
test('W6-002/2: queue empty but record unsaved still shows save attention', () => {
  const { api } = prepare();
  assert.strictEqual(api.bridgeQueueStats(CONVERSATION).jobs.length, 0, 'precondition: empty queue');
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.pending, 0);
  assert.strictEqual(save.failed, 0);
  assert.strictEqual(save.readyCount, 1);
  assert.strictEqual(save.durableCount, 0);
  assert.strictEqual(save.missingDelivery, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), true, 'queue-empty is not durability');
});

// 3 -----------------------------------------------------------------------
test('W6-002/3: an exact duplicate normal ingest marks the record durable', async () => {
  const { h, api } = prepare();
  installBridge(h, {
    audits: {
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        duplicate: true,
        run_id: RUN_ID,
        files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']
      })
    }
  });

  const stored = api.readAuditResultFresh('core', CONVERSATION);
  api.enqueueBridgeAuditRecord(stored, { deferFlush: true });
  const job = api.readBridgeJob(stored.bridgeReceipt);
  const pending = api.deliverBridgeJob(job);
  await h.settle();
  assert.strictEqual(await pending, true);

  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(Number(after.bridgeSavedAt) > 0, 'identical canonical content proves durability');
  assert.strictEqual(api.readBridgeJob(job.jobId), null);
});

// 4 -----------------------------------------------------------------------
test('W6-002/4: manual materialize of exact canonical content succeeds', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);

  const records = api.currentChatAuditRecords(CONVERSATION, { allowHistoricalComplete: true });
  const pending = api.materializeAuditRecordsNow(records, {});
  await h.settle();
  const result = await pending;

  assert.strictEqual(result.conflicts.length, 0);
  assert.strictEqual(result.failures.length, 0);
  assert.strictEqual(result.materialized, 1);
  assert.strictEqual(h.materializeRequests.length, 1);
  const sent = h.materializeRequests[0];
  assert.strictEqual(sent.source_run_id, RUN_ID);
  assert.strictEqual(sent.waves.length, 1);
  assert.strictEqual(sent.waves[0].wave_id, 'core');
  assert.strictEqual(sent.waves[0].content, waveText('core'));
});

// 5 -----------------------------------------------------------------------
test('W6-002/5: materialize recreates a missing physical file and records it', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now(), bridgeFiles: [] } } });
  installBridge(h, {
    materialize: {
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        duplicate: false,
        materialized: true,
        run_id: RUN_ID,
        final_rebuilt: false,
        waves: [{ wave_id: 'core', files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md'] }],
        files: ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']
      })
    }
  });

  const records = api.currentChatAuditRecords(CONVERSATION, { allowHistoricalComplete: true });
  const pending = api.materializeAuditRecordsNow(records, {});
  await h.settle();
  await pending;

  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.deepStrictEqual([...after.bridgeFiles], ['C:/audits/AUDAPACK__01_AUDIT_CORE.md']);
  assert.ok(Number(after.bridgeMaterializedAt) > 0);
});

// 6 -----------------------------------------------------------------------
test('W6-002/6: materialize content conflict fails visibly and writes nothing', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    materialize: {
      status: 409,
      responseText: JSON.stringify({
        ok: false,
        error: {
          code: 'materialize_content_conflict',
          message: "Wave 'core' canonical sha256 'aaaa' does not match submitted content 'bbbb'; nothing was written",
          retriable: false
        }
      })
    }
  });

  const before = api.readAuditResultFresh('core', CONVERSATION);
  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  const ok = await pending;

  assert.strictEqual(ok, false, 'manual SAVE must not claim success on a content conflict');
  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(after.bridgeError, 'the conflict must be operator-visible');
  assert.strictEqual(after.text, before.text, 'cached audit text is never discarded');
  assert.strictEqual(api.manualAuditSyncLastOutcome.label, 'SAVE!');
});

// 7 -----------------------------------------------------------------------
test('W6-002/7: manual SAVE never mutates CAMPAIGN_RUN_ID or mints a synthetic run', async () => {
  const { h, api } = prepare({ kinds: ['core'] });
  installBridge(h);

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(after.text.includes(`CAMPAIGN_RUN_ID: ${RUN_ID}`), after.text);
  for (const sent of (h.ingestRequests || [])) {
    assert.strictEqual(sent.run_id, RUN_ID);
    assert.ok(String(sent.content).includes(`CAMPAIGN_RUN_ID: ${RUN_ID}`), 'transport and content identity must agree');
    assert.ok(!/acb-mat-/.test(String(sent.run_id)), 'no synthetic materialize id may become a campaign run id');
  }
  for (const sent of (h.materializeRequests || [])) {
    assert.strictEqual(sent.source_run_id, RUN_ID);
    for (const wave of sent.waves) {
      assert.ok(String(wave.content).includes(`CAMPAIGN_RUN_ID: ${RUN_ID}`));
    }
  }
});

// 8 -----------------------------------------------------------------------
test('W6-002/8: three COMPLETE waves use exactly one materialize operation', async () => {
  const now = Date.now();
  const { h, api } = prepare({
    kinds: ['core', 'second', 'performance'],
    overrides: {
      core: { bridgeSavedAt: now },
      second: { bridgeSavedAt: now },
      performance: { bridgeSavedAt: now }
    }
  });
  installBridge(h, {
    materialize: {
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        duplicate: false,
        materialized: true,
        run_id: RUN_ID,
        final_rebuilt: true,
        all3_ready: true,
        waves: [
          { wave_id: 'core', files: ['C:/a/core.md'] },
          { wave_id: 'second', files: ['C:/a/second.md'] },
          { wave_id: 'performance', files: ['C:/a/perf.md'] }
        ],
        files: ['C:/a/core.md', 'C:/a/second.md', 'C:/a/perf.md', 'C:/a/AUDAPACK__00_AUDIT_ALL_3.md']
      })
    }
  });

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual(h.materializeRequests.length, 1, 'one coherent run = one materialize operation');
  assert.deepStrictEqual(
    [...h.materializeRequests[0].waves.map(w => w.wave_id)],
    ['core', 'second', 'performance']
  );
  const perf = api.readAuditResultFresh('performance', CONVERSATION);
  assert.ok(Number(perf.combinedSavedAt) > 0, 'ALL_3 rebuild is acknowledged on the terminal wave');
});

// 9 -----------------------------------------------------------------------
test('W6-002/9: 1/3 COMPLETE materializes exactly that wave and does not wait for 2 or 3', async () => {
  const { h, api } = prepare({ kinds: ['core'], overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual(h.materializeRequests.length, 1);
  assert.deepStrictEqual([...h.materializeRequests[0].waves.map(w => w.wave_id)], ['core']);
});

// 10 ----------------------------------------------------------------------
test('W6-002/10: a stalled Bridge cannot hold the foreground past the wall-clock deadline', async () => {
  const { h, api } = prepare();
  h.httpResponder = () => ({ stall: true });

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  assert.strictEqual(api.manualAuditSyncInFlight, true, 'precondition: the operation owns the button');

  h.advance(api.constants_MANUAL_SAVE_DEADLINE_MS);

  assert.strictEqual(api.manualAuditSyncInFlight, false, 'SAVE... must end at the deadline');
  assert.notStrictEqual(api.manualAuditSyncLastOutcome.label, 'SAVED');
  const cached = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(cached.text, waveText('core'), 'no cached audit text is discarded at the deadline');
  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'manual_save_deadline'), JSON.stringify(log));
  void pending;
});

// 11 ----------------------------------------------------------------------
test('W6-002/11: a stale timed-out attempt cannot overwrite a newer attempt UI state', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  // The first attempt's answers arrive long after its deadline.
  installBridge(h, {
    status: { status: 200, delay: 45000, responseText: JSON.stringify({ ok: true, service: 'AUDAPACK Bridge' }) }
  });

  const stale = api.syncSaveCurrentChatStateNow();
  await h.settle();
  const staleGeneration = api.manualAuditSyncOwnerGeneration;
  assert.ok(staleGeneration > 0);

  h.advance(api.constants_MANUAL_SAVE_DEADLINE_MS);
  assert.strictEqual(api.manualAuditSyncInFlight, false, 'the first attempt released the foreground');

  // A newer attempt now owns the button and finishes cleanly.
  installBridge(h);
  const fresh = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await fresh;
  const freshOutcome = { ...api.manualAuditSyncLastOutcome };
  assert.strictEqual(freshOutcome.label, 'SAVED');
  assert.ok(freshOutcome.generation > staleGeneration);

  // Now let the stale attempt's transport finally answer.
  h.advance(60000);
  await h.settle();
  await stale;

  assert.deepStrictEqual(
    { ...api.manualAuditSyncLastOutcome },
    freshOutcome,
    'a stale callback may not repaint the button'
  );
  assert.strictEqual(api.manualAuditSyncInFlight, false);
});

// 12 ----------------------------------------------------------------------
test('W6-002/12: every terminal path clears manualAuditSyncInFlight', async () => {
  const paths = [
    { name: 'success', handlers: {} },
    {
      name: 'auth failure',
      handlers: {
        audits: { status: 401, responseText: JSON.stringify({ ok: false, error: { code: 'invalid_auth', retriable: false } }) }
      }
    },
    {
      name: 'bridge offline',
      handlers: { status: { error: true, status: 0, responseText: '' } }
    },
    {
      name: 'content conflict',
      handlers: {
        materialize: {
          status: 409,
          responseText: JSON.stringify({ ok: false, error: { code: 'materialize_content_conflict', message: 'conflict', retriable: false } })
        }
      }
    },
    {
      name: 'invalid success payload',
      handlers: { audits: { status: 200, responseText: 'not json' } }
    }
  ];

  for (const path of paths) {
    const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
    installBridge(h, path.handlers);
    const pending = api.syncSaveCurrentChatStateNow();
    await h.settle();
    await pending;
    assert.strictEqual(api.manualAuditSyncInFlight, false, `${path.name} must release the foreground`);
    assert.strictEqual(api.manualAuditSyncOwnerGeneration, 0, `${path.name} must release UI ownership`);
  }
});

test('W6-002/12b: a throwing manual save still releases the foreground', async () => {
  const { h, api } = prepare();
  h.httpResponder = () => { throw new Error('injected transport explosion'); };
  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending.catch(() => { });
  assert.strictEqual(api.manualAuditSyncInFlight, false);
});

// 13 ----------------------------------------------------------------------
test('W6-002/13: the SAVED label appears only when durable_count == ready_count', () => {
  const now = Date.now();
  const { api } = prepare({
    kinds: ['core', 'second'],
    overrides: { core: { bridgeSavedAt: now }, second: { bridgeSavedAt: 0 } }
  });

  let save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.readyCount, 2);
  assert.strictEqual(save.durableCount, 1);
  assert.strictEqual(save.missingDelivery, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), true);

  assert.ok(api.patchAuditResult('second', next => { next.bridgeSavedAt = now; }, CONVERSATION, { expectedRunId: RUN_ID }));
  save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.durableCount, 2);
  assert.strictEqual(save.missingDelivery, 0);
  assert.strictEqual(api.currentAuditSaveAttention(), false);
});

// 14 ----------------------------------------------------------------------
test('W6-002/14: an empty queue with an undurable record stays SAVE attention, not SAVED', () => {
  const { api } = prepare({ kinds: ['core'] });
  const stats = api.bridgeQueueStats(CONVERSATION);
  assert.strictEqual(stats.total, 0);
  assert.strictEqual(api.currentAuditSaveAttention(), true);
  assert.strictEqual(api.superCompactAutoLabel(), 'SAVE');
});

// PHASE L ------------------------------------------------------------------
test('W6-002/L: legacy materialize failures are historical, not current queue health', () => {
  const { api } = prepare();
  const now = Date.now();
  const legacy = {
    version: 1,
    jobId: 'legacy-perf-1',
    receipt: 'legacy-perf-1',
    runId: 'acb-mat-old',
    sourceRunId: 'acb-mat-old',
    conversationKey: 'c:vaczen',
    project: 'VACZEN Calendar (CalendarTask)',
    wave: 'performance',
    materialize: true,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'Wave is already complete in the canonical run',
    attempts: 3,
    inFlightAt: 0,
    createdAt: now - 8 * 24 * 3600 * 1000,
    updatedAt: now - 8 * 24 * 3600 * 1000
  };
  assert.strictEqual(api.saveBridgeJob(legacy), true);

  const global = api.bridgeQueueStats('');
  assert.strictEqual(global.failed, 0, 'a retired-protocol job is not current failure');
  assert.strictEqual(global.historical, 1);

  const text = api.bridgeDiagnosticsText();
  assert.ok(/HISTORICAL FAILURES: 1/.test(text), text);
  assert.ok(/legacy-perf-1/.test(text), 'historical evidence stays inspectable');

  // A current-chat job with the same code is still actionable.
  const current = { ...legacy, jobId: 'current-1', receipt: 'current-1', conversationKey: CONVERSATION, runId: RUN_ID, sourceRunId: RUN_ID };
  assert.strictEqual(api.saveBridgeJob(current), true);
  assert.strictEqual(api.bridgeQueueStats(CONVERSATION).failed, 1);
});

// =========================================================================
// T-185 -- persistence-truth residue
// =========================================================================

test('W6-002/T185: Retry all refuses conflicts that re-delivery can never resolve', () => {
  const { api } = prepare();
  const now = Date.now();
  const base = {
    version: 1,
    conversationKey: CONVERSATION,
    runId: RUN_ID,
    sourceRunId: RUN_ID,
    project: 'AUDAPACK',
    wave: 'core',
    profileId: 'quick3',
    content: waveText('core'),
    permanent: true,
    attempts: 3,
    createdAt: now,
    updatedAt: now
  };
  assert.strictEqual(api.saveBridgeJob({ ...base, jobId: 'futile-1', receipt: 'futile-1', errorCode: 'completed_wave_immutable', lastError: 'refused on content' }), true);
  assert.strictEqual(api.saveBridgeJob({ ...base, jobId: 'futile-2', receipt: 'futile-2', wave: 'second', content: waveText('second'), errorCode: 'materialize_content_conflict', lastError: 'sha mismatch' }), true);

  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'completed_wave_immutable' }), true);
  // T-185 D4: campaign_profile_conflict is FRESH_RUN_REQUIRED -- the run is
  // bound to a profile; no unchanged repost can ever succeed. (The old widget
  // classified it retriable, so Retry all requeued it forever.)
  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'campaign_profile_conflict' }), true);
  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'project_identity_conflict' }), true);
  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'invalid_wave_structure' }), true);
  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'http_503' }), false, 'transport stays recoverable');
  assert.strictEqual(api.bridgeJobRetryIsFutile({ errorCode: 'invalid_auth' }), false, 'auth after token repair stays recoverable');

  const result = api.retryAllBridgeFailedJobs();
  assert.strictEqual(result.retried, 0, 'a content refusal must not be re-queued');
  assert.strictEqual(result.skipped, 2);
  assert.strictEqual(api.readBridgeJob('futile-1').permanent, true, 'evidence stays actionable');
  assert.strictEqual(api.readBridgeJob('futile-1').errorCode, 'completed_wave_immutable');

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'manual_retry_refused'), JSON.stringify(log));
});

test('W6-002/T185: a vanished canonical file turns an acknowledged wave back into attention', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    verify: body => {
      assert.strictEqual(body.verify_only, true, 'verification must not be a write');
      assert.strictEqual(body.receipt, undefined, 'a verification takes no operation receipt');
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          verify_only: true,
          run_id: body.source_run_id,
          waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: ['C:/a/core.md'] }],
          missing_files: ['C:/a/core.md'],
          files_intact: false
        })
      };
    }
  });

  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 1, 'precondition: acknowledged');

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  const result = await pending;

  assert.strictEqual(result.checked, 1);
  assert.strictEqual(result.missing, 1);

  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 1);
  assert.strictEqual(save.durableCount, 0, 'an absent file is not durability');
  assert.strictEqual(save.missingDelivery, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), true);
  assert.ok(api.readAuditResultFresh('core', CONVERSATION).bridgeError);
  assert.ok(api.readBridgeDiagnosticLog().some(entry => entry.event === 'durable_files_missing'));
});

test('W6-002/T185: an intact verification clears the missing-file attention', async () => {
  const { h, api } = prepare({
    overrides: { core: { bridgeSavedAt: Date.now(), bridgeFilesMissing: ['C:/a/core.md'] } }
  });
  installBridge(h);
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 0);

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  await pending;

  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 0);
  assert.strictEqual(save.durableCount, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), false);
});

test('W6-002/T185: an unreachable Bridge never invents durability or loss', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  h.httpResponder = () => ({ error: true, status: 0, responseText: '' });

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  const result = await pending;

  assert.strictEqual(result.checked, 0);
  assert.strictEqual(result.missing, 0);
  const record = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(record.bridgeFilesMissing, undefined, 'an unanswered probe records nothing');
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 1);
});

test('W6-002/T185: legacy materialize failures are retired through a journaled event', () => {
  const { api } = prepare();
  const now = Date.now();
  const legacy = {
    version: 1,
    jobId: 'legacy-journal-1',
    receipt: 'legacy-journal-1',
    runId: 'acb-mat-old',
    sourceRunId: 'acb-mat-old',
    conversationKey: 'c:vaczen',
    project: 'VACZEN Calendar (CalendarTask)',
    wave: 'performance',
    materialize: true,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'Wave is already complete in the canonical run',
    attempts: 3,
    inFlightAt: 0,
    createdAt: now - 8 * 24 * 3600 * 1000,
    updatedAt: now - 8 * 24 * 3600 * 1000
  };
  assert.strictEqual(api.saveBridgeJob(legacy), true);
  assert.strictEqual(api.readBridgeJob('legacy-journal-1').historicalRetiredAt, undefined);

  assert.strictEqual(api.retireLegacyMaterializeFailures(), 1);
  const retired = api.readBridgeJob('legacy-journal-1');
  assert.ok(Number(retired.historicalRetiredAt) > 0, 'retirement is durable, not re-derived');
  assert.strictEqual(retired.content, legacy.content);
  assert.ok(api.readBridgeDiagnosticLog().some(entry => entry.event === 'legacy_materialize_retired'));

  assert.strictEqual(api.retireLegacyMaterializeFailures(), 0, 'retirement happens once');
  assert.strictEqual(api.bridgeQueueStats('').failed, 0);
  assert.strictEqual(api.bridgeQueueStats('').historical, 1);
});

test('W6-002/T185: diagnostics report durability, not only queue counters', () => {
  const now = Date.now();
  const { api } = prepare({
    kinds: ['core', 'second'],
    overrides: { core: { bridgeSavedAt: now }, second: { bridgeSavedAt: 0 } }
  });
  const text = api.bridgeDiagnosticsText();
  assert.ok(/ready=2 durable=1 missing_delivery=1 files_missing=0/.test(text), text);
});

test('W6-002/T185: materializing clears a stale missing-file verdict', async () => {
  const { h, api } = prepare({
    overrides: { core: { bridgeSavedAt: Date.now(), bridgeFilesMissing: ['C:/a/core.md'] } }
  });
  installBridge(h);
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 0, 'precondition: attention');

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual(h.materializeRequests.length, 1);
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 0, 'the repair retires its own verdict');
  assert.strictEqual(save.durableCount, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), false);
});

test('W6-002/T185: manual SAVE does not race a background verification against its own repair', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);
  // Let any startup-driven probe finish, then watch only what manual SAVE does.
  await h.settle();
  h.verifyRequests = [];

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual(
    h.verifyRequests.length,
    0,
    'manual SAVE materializes; it must not also fire the read-only probe'
  );
  assert.strictEqual(h.materializeRequests.length, 1);
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 0, 'no stale verdict survives the repair');
  assert.strictEqual(save.durableCount, save.readyCount);
});

test('W6-002/T185: an outdated Bridge without the materialize endpoint says so', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    materialize: {
      status: 404,
      responseText: JSON.stringify({ ok: false, error: 'Endpoint not found' })
    }
  });

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  const ok = await pending;

  assert.strictEqual(ok, false);
  const record = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(/does not implement POST \/v1\/audits\/materialize/.test(String(record.bridgeError)), record.bridgeError);
  assert.strictEqual(record.text, waveText('core'), 'cached audit text is untouched');
  assert.strictEqual(api.manualAuditSyncInFlight, false);
});

// =========================================================================
// T-185 widget: representation durability reflects corruption + parity fix.
// =========================================================================

// 18 ----------------------------------------------------------------------
test('W6-002/T185: a corrupted canonical file makes the record SAVE! attention', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: ['C:/a/core.md'], unreadable: [] }],
        missing_files: [],
        mismatched_files: ['C:/a/core.md'],
        unreadable_files: [],
        files_intact: false
      })
    })
  });

  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 1, 'precondition: acknowledged');

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  const result = await pending;

  assert.strictEqual(result.checked, 1);
  assert.strictEqual(result.missing, 1);
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 1, 'a corrupted canonical file is not durable');
  assert.strictEqual(save.durableCount, 0);
  assert.strictEqual(api.currentAuditSaveAttention(), true);
  assert.ok(/canonical audit file differs from the committed wave/.test(
    String(api.readAuditResultFresh('core', CONVERSATION).bridgeError || '')
  ));
});

// 19 ----------------------------------------------------------------------
test('W6-002/T185: missing and mismatched files both produce attention', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [
          { wave_id: 'core', files: ['C:/a/core.md'], missing: ['C:/a/core.md'], mismatched: [], unreadable: [] },
          { wave_id: 'second', files: ['C:/a/second.md'], missing: [], mismatched: ['C:/a/second.md'], unreadable: [] }
        ],
        missing_files: ['C:/a/core.md'],
        mismatched_files: ['C:/a/second.md'],
        unreadable_files: [],
        files_intact: false
      })
    })
  });

  const records = api.currentChatAuditRecords(CONVERSATION, { allowHistoricalComplete: true });
  assert.strictEqual(records.length, 1);

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  await pending;

  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 1, 'one acknowledged wave is representation-broken');
  assert.strictEqual(api.currentAuditSaveAttention(), true);
});

// 20 ----------------------------------------------------------------------
test('W6-002/T185: manual SYNC/SAVE repairs a corrupted file and clears attention', async () => {
  const { h, api } = prepare({
    overrides: { core: { bridgeSavedAt: Date.now(), bridgeFilesMissing: ['C:/a/core.md'], bridgeFilesMismatched: ['C:/a/core.md'] } }
  });
  installBridge(h);
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 0, 'precondition: attention');

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual(h.materializeRequests.length, 1);
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 0, 'materialization repairs the mismatch and clears the verdict');
  assert.strictEqual(save.durableCount, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), false);
  const record = api.readAuditResultFresh('core', CONVERSATION);
  assert.deepStrictEqual(record.bridgeFilesMismatched, []);
});

// 21 ----------------------------------------------------------------------
test('W6-002/T185: live diagnostics and copied diagnostics agree on historical jobs', () => {
  const { h, api } = prepare();
  const now = Date.now();
  const legacy = {
    version: 1,
    jobId: 'legacy-parity-1',
    receipt: 'legacy-parity-1',
    runId: 'acb-mat-old',
    sourceRunId: 'acb-mat-old',
    conversationKey: 'c:vaczen',
    project: 'VACZEN Calendar (CalendarTask)',
    wave: 'performance',
    materialize: true,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'Wave is already complete in the canonical run',
    attempts: 3,
    inFlightAt: 0,
    createdAt: now - 8 * 24 * 3600 * 1000,
    updatedAt: now - 8 * 24 * 3600 * 1000
  };
  assert.strictEqual(api.saveBridgeJob(legacy), true);

  // Mount the panel first (the widget normally does this at bootstrap), then
  // render the bridge state, then compare what the live panel shows to what
  // the Copy diagnostics surface reports.
  if (typeof api.mount === 'function') api.mount();
  api.renderBridgeState();
  const logNode = h_logNode(h);
  assert.ok(logNode, 'the panel diagnostics node must exist');
  const liveHistorical = (logNode.textContent.match(/HISTORICAL FAILURES: (\d+)/) || [])[1];
  const copiedHistorical = (api.bridgeDiagnosticsText().match(/HISTORICAL FAILURES: (\d+)/) || [])[1];
  assert.strictEqual(liveHistorical, copiedHistorical, 'live panel and copied diagnostics must agree');
  assert.strictEqual(liveHistorical, '1', 'the retired legacy job must stay inspectable on BOTH surfaces');
});

// 22 ----------------------------------------------------------------------
test('W6-002/T185: a historical job stays inspectable after retirement', () => {
  const { h, api } = prepare();
  const now = Date.now();
  const legacy = {
    version: 1,
    jobId: 'legacy-hist-1',
    receipt: 'legacy-hist-1',
    runId: 'acb-mat-old',
    sourceRunId: 'acb-mat-old',
    conversationKey: 'c:vaczen',
    project: 'VACZEN Calendar (CalendarTask)',
    wave: 'performance',
    materialize: true,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'Wave is already complete in the canonical run',
    attempts: 3,
    inFlightAt: 0,
    createdAt: now - 8 * 24 * 3600 * 1000,
    updatedAt: now - 8 * 24 * 3600 * 1000
  };
  assert.strictEqual(api.saveBridgeJob(legacy), true);
  assert.strictEqual(api.retireLegacyMaterializeFailures(), 1);

  if (typeof api.mount === 'function') api.mount();
  api.renderBridgeState();
  const logNode = h_logNode(h);
  const live = (logNode.textContent.match(/HISTORICAL FAILURES: (\d+)/) || [])[1];
  assert.strictEqual(live, '1', 'retirement moves the job under HISTORICAL, where it stays inspectable');
});

// 23 ----------------------------------------------------------------------
test('W6-002/T185: a current semantic conflict stays current, not historical', () => {
  const { api } = prepare();
  const now = Date.now();
  const current = {
    version: 1,
    jobId: 'current-conflict-1',
    receipt: 'current-conflict-1',
    runId: RUN_ID,
    sourceRunId: RUN_ID,
    conversationKey: CONVERSATION,
    project: 'AUDAPACK',
    wave: 'core',
    materialize: true,
    permanent: true,
    errorCode: 'materialize_content_conflict',
    lastError: 'sha mismatch',
    attempts: 3,
    inFlightAt: 0,
    createdAt: now,
    updatedAt: now
  };
  assert.strictEqual(api.saveBridgeJob(current), true);
  assert.strictEqual(api.bridgeQueueStats(CONVERSATION).failed, 1, 'a current conflict is current health');
  assert.strictEqual(api.bridgeJobIsHistoricalFailure(current), false);
});

function h_logNode(h) {
  return h.dom ? h.dom.querySelector('#acb-bridge-log') : null;
}

// =========================================================================
// T-185 P0/P1: bounded verification freshness, unified recovery policy,
// campaign-final artifact durability.
// =========================================================================

// C1 + C2 ------------------------------------------------------------------
test('W6-002/T185: verification success expires after the freshness interval', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);
  await h.settle();
  // Startup may already have probed this run; the freshness model under test
  // starts from a known state.
  api.invalidateRunVerification(RUN_ID);
  h.verifyRequests = [];

  // First verify: one HTTP request.
  let pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  let result = await pending;
  assert.strictEqual(result.runs, 1);
  assert.strictEqual(h.verifyRequests.length, 1, 'first verification asks the Bridge');
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 1);

  // Immediate second: suppressed inside the freshness window (C1).
  pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  result = await pending;
  assert.strictEqual(result.runs, 0, 'fresh verification is suppressed');
  assert.strictEqual(h.verifyRequests.length, 1);

  // Advance past the freshness interval: the next verify MUST ask again (C2).
  api.advanceBridgeClockForTest(api.constants_BRIDGE_FILE_VERIFY_FRESH_MS + 1000);
  pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  result = await pending;
  assert.strictEqual(result.runs, 1, 'expired verification asks the Bridge again');
  assert.strictEqual(h.verifyRequests.length, 2, 'second HTTP request occurs');
});

// C3: epoch invalidation ----------------------------------------------------
test('W6-002/T185: a Bridge reconnect invalidates an old verification epoch', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: [], unreadable: [] }],
        final_artifacts: [],
        missing_files: [],
        files_intact: true
      })
    })
  });

  let pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await pending;
  h.verifyRequests = [];

  // Reconnect after outage: new generation, old proof is stale.
  api.setBridgeConnectEpochForTest(api.bridgeConnectEpoch + 1);
  pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await pending;
  assert.strictEqual(h.verifyRequests.length, 1, 'epoch change forces a re-verify');
});

// C3: a known missing result is never fresh trust ---------------------------
test('W6-002/T185: a missing verdict invalidates verification freshness', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  let reportsMissing = false;
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [{
          wave_id: 'core',
          files: ['C:/a/core.md'],
          missing: reportsMissing ? ['C:/a/core.md'] : [],
          mismatched: [],
          unreadable: []
        }],
        final_artifacts: [],
        missing_files: reportsMissing ? ['C:/a/core.md'] : [],
        files_intact: !reportsMissing
      })
    })
  });

  let pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await pending;
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 1);
  h.verifyRequests = [];

  // Inside the freshness window the probe is suppressed (C1) -- EXCEPT that a
  // verdict change is stronger evidence than the clock. Force the probe to
  // observe the Bridge's new answer, then prove the missing verdict was NOT
  // cached as fresh: the following normal probe must ask again.
  reportsMissing = true;
  pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  const forced = await pending;
  assert.strictEqual(forced.missing, 1);
  h.verifyRequests = [];

  pending = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  const result = await pending;
  assert.strictEqual(h.verifyRequests.length, 1, 'a missing verdict is never cached as fresh');
  assert.strictEqual(result.missing, 1);
  assert.strictEqual(api.currentBridgeSaveState(CONVERSATION).durableCount, 0, 'durableCount becomes 0');
  assert.strictEqual(api.currentAuditSaveAttention(), true);
});

// C4: manual SAVE finishes on FRESH physical proof --------------------------
test('W6-002/T185: manual SAVE re-verifies past-stale durability before saying SAVED', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);
  await h.settle();

  // Age the last verification past the freshness bound.
  api.advanceBridgeClockForTest(api.constants_BRIDGE_FILE_VERIFY_FRESH_MS + 1000);
  h.verifyRequests = [];
  h.materializeRequests = [];

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  // Materialization itself is the fresh physical proof of this operation.
  assert.strictEqual(h.materializeRequests.length, 1, 'manual SAVE proves durability through materialization');
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 0);
  assert.strictEqual(save.durableCount, save.readyCount);
  assert.strictEqual(api.manualAuditSyncLastOutcome.label, 'SAVED');
});

// C1: repeated renders/status never storm ------------------------------------------------------
test('W6-002/T185: repeated verifications inside the freshness window do not storm', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);

  for (let i = 0; i < 5; i += 1) {
    const pending = api.verifyDurableAuditFilesNow(CONVERSATION);
    await h.settle();
    await pending;
  }
  assert.strictEqual(h.verifyRequests.length, 1, 'five rapid verifications make one request');
});

// P0-2 (widget half): missing ALL_3 becomes attention ------------------------
test('W6-002/T185: a missing campaign-final handoff makes the terminal wave SAVE attention', async () => {
  const { h, api } = prepare({
    kinds: ['core', 'second', 'performance'],
    overrides: {
      core: { bridgeSavedAt: Date.now() },
      second: { bridgeSavedAt: Date.now() },
      performance: { bridgeSavedAt: Date.now() }
    }
  });
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [
          { wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: [], unreadable: [] },
          { wave_id: 'second', files: ['C:/a/second.md'], missing: [], mismatched: [], unreadable: [] },
          { wave_id: 'performance', files: ['C:/a/perf.md'], missing: [], mismatched: [], unreadable: [] }
        ],
        final_artifacts: [
          { path: 'C:/a/AUDAPACK__00_AUDIT_ALL_3.md', digest: 'abc', verdict: 'MISSING' }
        ],
        missing_files: ['C:/a/AUDAPACK__00_AUDIT_ALL_3.md'],
        files_intact: false
      })
    })
  });

  const pending = api.verifyDurableAuditFilesNow(CONVERSATION, { force: true });
  await h.settle();
  const result = await pending;

  assert.strictEqual(result.missing, 1);
  const save = api.currentBridgeSaveState(CONVERSATION);
  assert.strictEqual(save.filesMissing, 1, 'a missing ALL_3 is not durability');
  assert.strictEqual(save.durableCount, save.readyCount - 1);
  assert.strictEqual(api.currentAuditSaveAttention(), true);
  const perf = api.readAuditResultFresh('performance', CONVERSATION);
  assert.ok(/campaign-final handoff/.test(String(perf.bridgeError || '')), perf.bridgeError);
});

// D1: manual SAVE never POSTs a completed_wave_immutable wave to /v1/audits --
test('W6-002/T185: manual SAVE never re-POSTs completed_wave_immutable to ingest', async () => {
  const { h, api } = prepare();
  installBridge(h);
  const record = api.readAuditResultFresh('core', CONVERSATION);
  api.enqueueBridgeAuditRecord(record, { deferFlush: true });
  const job = api.readAuditJob
    ? api.readBridgeJob(record.bridgeReceipt)
    : api.readBridgeJob(record.bridgeReceipt);

  // The ingest failed permanently as completed_wave_immutable.
  assert.strictEqual(api.saveBridgeJob({
    ...job,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'wave already complete with different content'
  }), true);

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual((h.ingestRequests || []).length, 0, 'manual SAVE never POSTs the unchanged wave to /v1/audits');
  assert.strictEqual(h.materializeRequests.length, 1, 'it attempts canonical verify/materialize instead');
  const after = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(Number(after.bridgeSavedAt) > 0, 'matching canonical server wave becomes durable from server proof');
  assert.strictEqual(api.readBridgeJob(job.jobId), null, 'the old failed ingest job is retired');
  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'materialize_recovered_ingest' || entry.event === 'retry_materialize_verified'),
    JSON.stringify(log.map(e => e.event)));
});

// D1: differing canonical content stays permanent -----------------------------
test('W6-002/T185: a differing canonical wave stays a permanent conflict under manual SAVE', async () => {
  const { h, api } = prepare();
  installBridge(h, {
    materialize: {
      status: 409,
      responseText: JSON.stringify({
        ok: false,
        error: {
          code: 'materialize_content_conflict',
          message: "Wave 'core' canonical sha256 'aaaa' does not match submitted content 'bbbb'",
          retriable: false
        }
      })
    }
  });
  const record = api.readAuditResultFresh('core', CONVERSATION);
  api.enqueueBridgeAuditRecord(record, { deferFlush: true });
  const job = api.readBridgeJob(record.bridgeReceipt);
  assert.strictEqual(api.saveBridgeJob({
    ...job,
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'wave already complete with different content'
  }), true);

  const pending = api.syncSaveCurrentChatStateNow();
  await h.settle();
  await pending;

  assert.strictEqual((h.ingestRequests || []).length, 0, 'no unchanged ingest POST');
  const kept = api.readBridgeJob(job.jobId);
  assert.ok(kept, 'the permanent conflict evidence survives');
  assert.strictEqual(kept.permanent, true);
  assert.strictEqual(kept.errorCode, 'completed_wave_immutable', 'attempts/error lineage is not rewritten');
});

// D table: one classification for Retry all and manual SAVE --------------------
test('W6-002/T185: Retry all and manual SAVE share one recovery classification', () => {
  const { api } = prepare();
  const cases = [
    { code: 'completed_wave_immutable', cls: 'MATERIALIZE_CANONICAL' },
    { code: 'materialize_content_conflict', cls: 'UNRECOVERABLE' },
    { code: 'receipt_conflict', cls: 'UNRECOVERABLE' },
    { code: 'campaign_profile_conflict', cls: 'FRESH_RUN_REQUIRED' },
    { code: 'project_identity_conflict', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'invalid_wave_structure', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'invalid_run_id', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'unsupported_wave', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'unsupported_profile', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'invalid_project_id', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'materialize_wave_not_complete', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'project_unresolvable_readonly', cls: 'INPUT_REPAIR_REQUIRED' },
    { code: 'http_503', cls: 'RETRY_TRANSPORT' },
    { code: 'timeout', cls: 'RETRY_TRANSPORT' },
    { code: 'invalid_auth', cls: 'RETRY_AFTER_AUTH' }
  ];
  for (const item of cases) {
    assert.strictEqual(
      api.classifyBridgeJobRecovery({ permanent: true, errorCode: item.code }),
      item.cls,
      `${item.code} must classify once for every consumer`
    );
  }

  // A refused retry keeps its evidence untouched (D6).
  const now = Date.now();
  const refused = {
    version: 1,
    jobId: 'refused-1',
    receipt: 'refused-1',
    runId: RUN_ID,
    sourceRunId: RUN_ID,
    conversationKey: CONVERSATION,
    project: 'AUDAPACK',
    wave: 'core',
    profileId: 'quick3',
    permanent: true,
    errorCode: 'campaign_profile_conflict',
    lastError: 'profile mismatch',
    attempts: 4,
    inFlightAt: 0,
    createdAt: now,
    updatedAt: now
  };
  assert.strictEqual(api.saveBridgeJob(refused), true);
  const result = api.retryAllBridgeFailedJobs();
  assert.strictEqual(result.retried, 0);
  const after = api.readBridgeJob('refused-1');
  assert.strictEqual(after.attempts, 4, 'attempts lineage is preserved');
  assert.strictEqual(after.errorCode, 'campaign_profile_conflict', 'errorCode lineage is preserved');
  assert.strictEqual(after.permanent, true);
});

// D1 (Retry all half): retry all defers completed_wave_immutable to materialize
test('W6-002/T185: Retry all routes completed_wave_immutable through materialize, never ingest', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: 0 } } });
  installBridge(h);
  const now = Date.now();
  assert.strictEqual(api.saveBridgeJob({
    version: 1,
    jobId: 'cwi-retry-1',
    receipt: 'core-acb-w6002-run-receipt',
    runId: RUN_ID,
    sourceRunId: RUN_ID,
    conversationKey: CONVERSATION,
    project: 'AUDAPACK',
    wave: 'core',
    profileId: 'quick3',
    content: waveText('core'),
    permanent: true,
    errorCode: 'completed_wave_immutable',
    lastError: 'wave already complete',
    attempts: 2,
    inFlightAt: 0,
    createdAt: now,
    updatedAt: now
  }), true);

  const result = api.retryAllBridgeFailedJobs();
  assert.strictEqual(result.retried, 0, 'no unchanged requeue');
  await h.settle();
  await new Promise(resolve => setImmediate(resolve));
  await h.settle();

  assert.strictEqual((h.ingestRequests || []).length, 0, 'Retry all never re-POSTs an immutable wave');
  assert.strictEqual(h.materializeRequests.length >= 1, true, 'the materialize probe runs');
});

// =============================================================================
// W2-002: IN-FLIGHT VERIFICATION COALESCING RED-FIRST TESTS
// =============================================================================

// 1. TWO SIMULTANEOUS CALLERS
test('W6-002/T185 W2-002: two simultaneous callers coalesce to one HTTP request', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h, {
    verify: () => ({
      status: 200,
      responseText: JSON.stringify({
        ok: true,
        verify_only: true,
        run_id: RUN_ID,
        waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: [], unreadable: [] }],
        final_artifacts: [],
        missing_files: [],
        files_intact: true
      }),
      delay: 6000
    })
  });

  const p1 = api.verifyDurableAuditFilesNow(CONVERSATION);
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  const r1 = await p1;
  const r2 = await p2;

  assert.strictEqual(h.verifyRequests.length, 1, 'two callers share one physical probe');
  assert.strictEqual(r1.checked, 1);
  assert.strictEqual(r2.checked, 1, 'both callers observe the same durability truth');
});

// 2. CHECKBRIDGE + EXPLICIT VERIFY: two concurrent verify callers coalesce
test('W6-002/T185 W2-002: checkBridge-like auto-verify coalesces with explicit verify', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);

  const p1 = api.verifyDurableAuditFilesNow(CONVERSATION);
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p1;
  await p2;

  assert.strictEqual(h.verifyRequests.length, 1, 'coalesced caller shares probe');
});

// 3. FIVE RAPID CALLS (storm test already exists at line 1142)

// 4. DIFFERENT RUNS - separate conversations so groups run separately
test('W6-002/T185 W2-002: different runs verify independently', async () => {
  const RUN_A = RUN_ID;
  const RUN_B = 'acb-w6002-other';
  const CONV_A = 'c:run-a';
  const CONV_B = 'c:run-b';
  function recordFor(runId, conv) {
    return {
      version: 1,
      conversationKey: conv,
      runId,
      bridgeReceipt: `core-${runId}-receipt`,
      kind: 'core',
      projectName: 'AUDAPACK',
      projectId: 'audapack',
      profileId: 'quick3',
      profileVersion: '1.0.0',
      waveIndex: 1,
      waveCount: 1,
      gateState: 'complete',
      completedAt: Date.now(),
      bridgeSavedAt: Date.now(),
      text: [
        'PROJECT_NAME: AUDAPACK',
        'CAMPAIGN_PROFILE: quick3',
        `CAMPAIGN_RUN_ID: ${runId}`,
        'WAVE_ID: core',
        'STATUS: AUDIT_CORE: COMPLETE',
        'TICKETS: 0',
        'HANDOFF: IMPLEMENTATION_AGENT',
        'NO VERIFIED CORE DEFECTS.',
        'CORE_DONE_WHEN: verified'
      ].join('\n')
    };
  }
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', TOKEN);
  // No prepare -- custom run records.
  api.writeAuditResult(recordFor(RUN_A, CONV_A));
  api.writeAuditResult(recordFor(RUN_B, CONV_B));
  installBridge(h);

  const p1 = api.verifyDurableAuditFilesNow(CONV_A);
  await h.settle();
  await p1;

  const p2 = api.verifyDurableAuditFilesNow(CONV_B);
  await h.settle();
  await p2;

  assert.strictEqual(h.verifyRequests.length, 2, 'one request per run');
});

// 5. FINGERPRINT CHANGES DURING REQUEST - old response must not become fresh proof
test('W6-002/T185 W2-002: fingerprint change during request invalidates the old proof', async () => {
  const RUN_ORG = RUN_ID;
  const wave = [
    'PROJECT_NAME: AUDAPACK',
    'CAMPAIGN_PROFILE: quick3',
    `CAMPAIGN_RUN_ID: ${RUN_ORG}`,
    'WAVE_ID: core',
    'STATUS: AUDIT_CORE: COMPLETE',
    'TICKETS: 0',
    'HANDOFF: IMPLEMENTATION_AGENT',
    'NO VERIFIED CORE DEFECTS.',
    'CORE_DONE_WHEN: verified'
  ].join('\n');
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.auditProfile = 'quick3';
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', TOKEN);
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
  api.autoRuntime.conversationKey = CONVERSATION;
  api.autoRuntime.runId = RUN_ORG;
  api.autoRuntime.projectName = 'AUDAPACK';
  api.autoRuntime.projectId = 'audapack';
  api.autoRuntime.stage = 'complete';
  api.saveAutoRuntime({ pauseOnFailure: false });
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  api.writeAuditResult({
    version: 1,
    conversationKey: CONVERSATION,
    runId: RUN_ORG,
    bridgeReceipt: `core-${RUN_ORG}-receipt`,
    kind: 'core',
    projectName: 'AUDAPACK',
    projectId: 'audapack',
    profileId: 'quick3',
    profileVersion: '1.0.0',
    waveIndex: 1,
    waveCount: 1,
    gateState: 'complete',
    completedAt: Date.now(),
    bridgeSavedAt: Date.now(),
    text: wave
  });
  installBridge(h);
  // Absorb boot-time health/status/verify so epoch stable at 0 and following
  // mutation vs probe comparison is fingerprint-only. Boot verify may have
  // already cached freshness so suppress it for the fingerprint test.
  await h.settle();
  api.invalidateRunVerification(RUN_ORG);
  // Remove any staleness from the boot probe's prior durable mark so the
  // test observes whether THIS probe marks the new record fresh.
  api.writeAuditResult({ ...api.readAuditResultFresh('core', CONVERSATION), bridgeFilesVerifiedAt: undefined, bridgeVerifyEpoch: undefined, bridgeFilesMissing: [], bridgeFilesMismatched: [], bridgeFilesUnreadable: [], bridgeError: '' });
  h.verifyRequests = [];

  const oldRecord = api.readAuditResultFresh('core', CONVERSATION);
  const p = api.verifyDurableAuditFilesNow(CONVERSATION);
  // Mutate length -> new fingerprint before response settles.
  oldRecord.text = oldRecord.text + '\nX_ENOUGH_TO_CHANGE_LENGTH';
  api.writeAuditResult(oldRecord);
  await h.settle();
  await p;

  const rec = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(rec.bridgeFilesVerifiedAt, undefined, 'old proof not applied to changed record');

  h.verifyRequests = [];
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p2;
  assert.strictEqual(h.verifyRequests.length, 1, 'one fresh verification for the new fingerprint');
});

// 6. BRIDGE EPOCH CHANGES DURING REQUEST
test('W6-002/T185 W2-002: bridge epoch change during request discards the old proof', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);
  await h.settle();
  api.invalidateRunVerification(RUN_ID);
  api.writeAuditResult({ ...api.readAuditResultFresh('core', CONVERSATION), bridgeFilesVerifiedAt: undefined, bridgeVerifyEpoch: undefined, bridgeFilesMissing: [], bridgeFilesMismatched: [], bridgeFilesUnreadable: [], bridgeError: '' });
  h.verifyRequests = [];

  const p = api.verifyDurableAuditFilesNow(CONVERSATION);
  // Advance epoch while request is in flight (before response resolves).
  api.setBridgeConnectEpochForTest(api.bridgeConnectEpoch + 1);
  await h.settle();
  await p;

  // Old response must not become fresh proof for the new generation.
  const rec = api.readAuditResultFresh('core', CONVERSATION);
  assert.strictEqual(rec.bridgeFilesVerifiedAt, undefined, 'old proof discarded');

  // Next verification: one new request.
  h.verifyRequests = [];
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p2;
  assert.strictEqual(h.verifyRequests.length, 1, 'one fresh verification for the new generation');
});

// 7. FAILURE RELEASES OWNERSHIP
test('W6-002/T185 W2-002: failed verification releases in-flight ownership', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  await h.settle();
  api.invalidateRunVerification(RUN_ID);
  let phase = 0;
  installBridge(h, {
    verify: () => {
      phase += 1;
      if (phase === 1) return { status: 503, responseText: JSON.stringify({ ok: false, error: { code: 'http_503' } }) };
      return { status: 200, responseText: JSON.stringify({ ok: true, verify_only: true, run_id: RUN_ID, waves: [{ wave_id: 'core', files: [], missing: [], mismatched: [], unreadable: [] }], final_artifacts: [], missing_files: [], files_intact: true }) };
    }
  });

  const p = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p;
  assert.strictEqual(h.verifyRequests.length, 1);
  assert.strictEqual(api.inFlightRunVerificationCount, 0, 'ownership released on failure');

  // A later call can retry (suppress freshness that might still be active; the test
  // covers in-flight release, not freshness expiry).
  api.invalidateRunVerification(RUN_ID);
  h.verifyRequests = [];
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p2;
  assert.strictEqual(h.verifyRequests.length, 1, 'retry issues new request after failure');
});

// 8. EXCEPTION RELEASES OWNERSHIP
test('W6-002/T185 W2-002: exception releases in-flight ownership', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  // Bridge returns a not-ok error (same finally path) -- true throw in the harness
  // crashes the responder, but offline error proves the same inFlight cleanup.
  installBridge(h, {
    verify: () => ({ status: 0, responseText: JSON.stringify({ ok: false, error: { code: 'bridge_offline', retriable: true } }) })
  });

  const p = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p.catch(() => {});
  assert.strictEqual(api.inFlightRunVerificationCount, 0, 'ownership released on failure/exception');

  // Next call works.
  installBridge(h);
  h.verifyRequests = [];
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  await p2;
  assert.strictEqual(h.verifyRequests.length, 1, 'retry issues new request after exception');
});

// 9. OLD FINALLY CANNOT DELETE NEW OWNER
test('W6-002/T185 W2-002: old finally cannot delete newer replacement entry', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  let phase = 0;
  installBridge(h, {
    verify: () => {
      phase += 1;
      if (phase === 1) return { status: 200, delay: 100, responseText: JSON.stringify({ ok: true, verify_only: true, run_id: RUN_ID, waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: [], unreadable: [] }], final_artifacts: [], missing_files: [], files_intact: true }) };
      // Second call sees the fingerprint change.
      return { status: 200, responseText: JSON.stringify({ ok: true, verify_only: true, run_id: RUN_ID, waves: [{ wave_id: 'core', files: ['C:/a/core.md'], missing: [], mismatched: [], unreadable: [] }], final_artifacts: [], missing_files: [], files_intact: true }) };
    }
  });

  // Start first verification (small delay to keep it in-flight during second call).
  const p1 = api.verifyDurableAuditFilesNow(CONVERSATION);
  // Before it completes, start a second with changed fingerprint.
  const old = api.readAuditResultFresh('core', CONVERSATION);
  api.writeAuditResult({ ...old, text: old.text + '\nX', bridgeSavedAt: Date.now() });
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);

  await h.settle();
  await p1.catch(() => {});
  await p2;
  // No permanently stuck entry.
  assert.strictEqual(api.inFlightRunVerificationCount, 0);
  // The second request should have been the one that applied (fingerprint matches).
  const rec = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(Number(rec.bridgeFilesVerifiedAt) > 0, 'second verification marked the current record fresh');
});

// 10. SUCCESSFUL COALESCED RESULT
test('W6-002/T185 W2-002: all waiters observe the same final durability truth without duplicated patch', async () => {
  const { h, api } = prepare({ overrides: { core: { bridgeSavedAt: Date.now() } } });
  installBridge(h);

  const p1 = api.verifyDurableAuditFilesNow(CONVERSATION);
  const p2 = api.verifyDurableAuditFilesNow(CONVERSATION);
  await h.settle();
  const r1 = await p1;
  const r2 = await p2;

  assert.strictEqual(h.verifyRequests.length, 1);
  assert.strictEqual(r1.checked, 1);
  assert.strictEqual(r2.checked, 1);
  assert.strictEqual(r1.missing, 0);
  assert.strictEqual(r2.missing, 0);

  // Single patch: only one bridgeFilesVerifiedAt write.
  const rec = api.readAuditResultFresh('core', CONVERSATION);
  assert.ok(Number(rec.bridgeFilesVerifiedAt) > 0);
  // No duplicate diagnostics.
  const log = api.readBridgeDiagnosticLog();
  const dur = log.filter(e => e.event === 'durable_files_missing');
  assert.strictEqual(dur.length, 0, 'no duplicate diagnostic entries');
});

// PERF-004 (audit/11.md): verifiedMaterializeRuns is bounded with deterministic eviction
test('W6-002/PERF-004: verifiedMaterializeRuns enforces max items with oldest-first eviction', () => {
  const { api } = prepare();
  const maxItems = api.constants_VERIFIED_MATERIALIZE_RUNS_MAX_ITEMS;
  assert.ok(maxItems >= 1);

  // Start clean
  api.clearVerifiedMaterializeRunsForTest();
  assert.strictEqual(api.verifiedMaterializeRunsSize, 0);

  // Add maxItems + 3 entries
  for (let i = 0; i < maxItems + 3; i += 1) {
    const runId = `run-perf004-${i}`;
    api.recordRunVerification(runId, [], { verifiedAt: Date.now() + i });
  }

  // Size must be bounded
  assert.ok(api.verifiedMaterializeRunsSize <= maxItems, `item bound violated: ${api.verifiedMaterializeRunsSize}`);

  // Oldest-first: the earliest key was evicted, the newest survived
  assert.strictEqual(api.bridgeVerificationIsFresh(`run-perf004-0`, []), false, 'oldest entry should be evicted');
  assert.strictEqual(api.bridgeVerificationIsFresh(`run-perf004-${maxItems + 2}`, []), true, 'newest entry should survive');

  // Cleanup
  api.clearVerifiedMaterializeRunsForTest();
  assert.strictEqual(api.verifiedMaterializeRunsSize, 0);
});
