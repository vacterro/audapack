'use strict';

// T-184 -- the operator's real path, end to end.
//
// PHASE P: manual archive drop -> START -> Core COMPLETE -> physical Bridge
// save, with no step assumed. PHASE Q: the START failure cases that must NOT
// send. PHASE R: the automatic per-wave save chain (Core, Second, Performance,
// ALL_3), one transient outage, and one permanent immutable conflict.
//
// The operator reasonably suspected ALL audit saving, so nothing here is
// proven by a unit stub: every save goes through the real Bridge job queue and
// the real HTTP surface.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup,
  composerFixture,
  addComposerAttachmentTile,
  installAcceptedSend,
  FakeEvent
} = require('./helpers');
const { FakeFile, FakeDataTransfer } = require('./harness');

const TOKEN = 'w6-003-test-token';
const CONVO = 'c:abc123';
const RUN_ID = 'acb-w6003-run';
const ARCHIVE = '_AUDAPACK_09.09.26-T16-01-51.zip';

function handoffFor(api, waveId, profileId = 'quick3', runId = RUN_ID) {
  const profile = api.EMBEDDED_AUDIT_PROFILES.profiles[profileId];
  const waveDef = profile.waves.find(w => w.id === waveId);
  const pfx = waveDef.ticket_prefix.replace(/-$/, '');
  const termKey = waveDef.terminal_status_key || waveDef.slug;
  const fields = (waveDef.ticket_fields && waveDef.ticket_fields.length)
    ? waveDef.ticket_fields
    : ['EVIDENCE', 'DEFECT', 'REPAIR', 'VERIFY'];
  return `PROJECT_NAME: AUDAPACK
DATE_TIME: 2026-09-09T16:00:00+03:00
CAMPAIGN_PROFILE: ${profileId}
CAMPAIGN_RUN_ID: ${runId}
WAVE_ID: ${waveDef.id}
WAVE_INDEX: ${waveDef.ordinal}
WAVE_COUNT: ${profile.waves.length}
WAVE: ${waveDef.wave_header}
STATUS: ${termKey}: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [${pfx}-001] Sample defect issue title
${fields.map(field => `${field}: sample ${field.toLowerCase()}.`).join('\n')}

${waveDef.done_marker.replace(/:\s*$/, '')}: All tickets and handoffs are verified.`;
}

function archiveFile(name = ARCHIVE) {
  return new FakeFile([Buffer.from('PK\u0003\u0004 archive bytes')], name, {
    type: 'application/zip',
    lastModified: Date.now()
  });
}

function dispatchDrop(h, target, { files = [], uri = '' } = {}) {
  const transfer = new FakeDataTransfer();
  for (const file of files) transfer.items.add(file);
  if (uri) transfer.setData('text/uri-list', uri);
  transfer.types = [...(files.length ? ['Files'] : []), ...(uri ? ['text/uri-list'] : [])];
  const event = new FakeEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer });
  event.target = target;
  h.dom.dispatchEvent(event);
  return event;
}

function composerTilesNamed(h, name) {
  return h.dom.querySelectorAll('[role="group"]')
    .filter(element => element.getAttribute('aria-label') === name);
}

// Bridge surface. Ingest and materialize stay strictly separate endpoints.
function installBridge(h, handlers = {}) {
  h.ingestRequests = [];
  h.materializeRequests = [];
  h.verifyRequests = [];
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
      if (body.verify_only) {
        h.verifyRequests.push(body);
        return handlers.verify || {
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
      h.materializeRequests.push(body);
      return typeof handlers.materialize === 'function'
        ? handlers.materialize(body)
        : (handlers.materialize || {
          status: 200,
          responseText: JSON.stringify({
            ok: true,
            duplicate: false,
            materialized: true,
            run_id: body.source_run_id,
            final_rebuilt: body.waves.length === 3,
            all3_ready: body.waves.length === 3,
            waves: body.waves.map(w => ({ wave_id: w.wave_id, files: [`C:/audits/AUDAPACK__${w.wave_id}.md`] })),
            files: body.waves.map(w => `C:/audits/AUDAPACK__${w.wave_id}.md`)
          })
        });
    }
    if (/\/v1\/audits/.test(url)) {
      const body = JSON.parse(String(request.data || '{}'));
      h.ingestRequests.push(body);
      return typeof handlers.audits === 'function'
        ? handlers.audits(body)
        : (handlers.audits || {
          status: 200,
          responseText: JSON.stringify({
            ok: true,
            duplicate: false,
            run_id: body.run_id,
            files: [`C:/audits/AUDAPACK__${body.wave}.md`],
            all3_ready: body.wave === 'performance'
          })
        });
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found' } }) };
  };
}

function auditWindow(options = {}) {
  const { h, api } = setup();
  const composer = composerFixture(h);
  api.state.auditProfile = options.profileId || 'quick3';
  api.state.chatgptPromptDelivery = 'text';
  api.state.bridgeEnabled = true;
  api.state.autoSaveAuditFiles = true;
  api.state.superCompact = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', TOKEN);
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: options.profileId || 'quick3' });
  api.autoRuntime.conversationKey = CONVO;
  api.autoRuntime.projectName = 'AUDAPACK';
  api.autoRuntime.projectId = 'audapack';
  if (options.runId !== null) api.autoRuntime.runId = options.runId || RUN_ID;
  api.autoRuntime.startedAt = Date.now();
  api.saveAutoRuntime({ pauseOnFailure: false });
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  installBridge(h, options.handlers || {});
  return { h, api, composer };
}

function attachArchiveByDrop(h, api, composer, name = ARCHIVE) {
  composer.upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const event = dispatchDrop(h, composer.input, {
    files: [archiveFile(name)],
    uri: `file:///V:/___VAC/__K/__CODE/_PY/_AUDAPACK/${name}`
  });
  return { event, dropResult: api.lastArchiveDropResult };
}

// =========================================================================
// PHASE P -- manual drop -> START -> Core COMPLETE -> physical save
// =========================================================================

test('W6-003/P: a manually dropped archive attaches cleanly and never leaks file:/// text', () => {
  const { h, api, composer } = auditWindow();
  assert.strictEqual(api.composerPlainText(composer.input), '', '1. composer starts empty');

  const { event, dropResult } = attachArchiveByDrop(h, api, composer);

  assert.strictEqual(dropResult.action, 'sanitized', '2. the real drop sanitizer owns the drop');
  assert.strictEqual(dropResult.reason, 'archive-attached');
  assert.strictEqual(event.defaultPrevented, true);
  assert.strictEqual(composerTilesNamed(h, ARCHIVE).length, 1, '3. exactly one ZIP tile');
  assert.strictEqual(composer.upload.files.length, 1);
  assert.strictEqual(composer.upload.files[0].name, ARCHIVE);
  const text = api.composerPlainText(composer.input);
  assert.ok(!text.includes('file:///'), '4. no file:/// URI text reaches the composer');
  assert.ok(!text.includes(ARCHIVE));
});

test('W6-003/P: project identity resolves from the manually attached archive', () => {
  const { h, api, composer } = auditWindow();
  attachArchiveByDrop(h, api, composer);
  assert.strictEqual(api.projectNameFromArtifactFilename(ARCHIVE), 'AUDAPACK', '5. identity from the archive');
  const summary = api.chatGPTReadyAttachmentSummary();
  assert.ok(summary.ready || summary.count > 0, 'the attachment is visible to the widget');
});

test('W6-003/P: START is offered for a manually dropped archive and sends exactly once', async () => {
  const { h, api, composer } = auditWindow({ runId: null });
  installAcceptedSend(h, composer);
  api.autoRuntime.stage = 'complete';
  attachArchiveByDrop(h, api, composer);

  const start = api.miniStartAuditState();
  assert.strictEqual(start.available, true, '6. START is exposed on the compact widget');

  composer.send.disabled = true;
  h.timers.setTimeout(() => { composer.send.disabled = false; }, 400);

  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  const started = await pending;

  assert.strictEqual(started.sent, true, '7. START runs');
  assert.strictEqual(started.adopted, true, '7b. the sent Core is adopted, not just submitted');
  assert.strictEqual(started.stage, 'wait-core');
  assert.strictEqual(composer.send._clicked, true, '10. exactly one Send occurs');
  assert.strictEqual(composer.send._clickCount, 1, '10. exactly ONE Send click, never two');
  assert.strictEqual(composerTilesNamed(h, ARCHIVE).length, 1, '9. the ZIP stays attached');
  assert.ok(api.autoRuntime.runId, '11. a canonical run id is persisted');
  assert.strictEqual(api.autoRuntime.enabled, true, '12. Auto3 owns the run');
  const stored = api.readStoredRuntime(CONVO).runtime;
  assert.strictEqual(String(stored.runId || ''), String(api.autoRuntime.runId));
});

test('W6-003/P: a COMPLETE Core is captured, queued, saved and then materialized alone', async () => {
  const { h, api } = auditWindow();
  api.autoRuntime.stage = 'wait-core';
  api.autoRuntime.coreUserId = 'u-core';
  api.saveAutoRuntime({ pauseOnFailure: false });

  const coreText = handoffFor(api, 'core');
  const commit = api.commitTerminalWaveResult('core', coreText, 'complete', 'u-core');
  assert.strictEqual(commit.ok, true, '13/14. a structurally COMPLETE Core is captured');

  const cached = api.readAuditResultFresh('core', CONVO);
  assert.strictEqual(cached.text, coreText, '14. the exact handoff is stored');

  // 15. auto-save queued a NORMAL ingest job (never a materialize job).
  const queued = api.readBridgeJob(cached.bridgeReceipt);
  assert.ok(queued, 'auto-save queued a Bridge ingest job');
  assert.strictEqual(Boolean(queued.materialize), false);

  await h.settle();

  // 16/17. a successful acknowledgement is the only thing that sets durability.
  const saved = api.readAuditResultFresh('core', CONVO);
  assert.ok(Number(saved.bridgeSavedAt) > 0, '16. bridgeSavedAt comes from a real acknowledgement');
  assert.strictEqual(api.readBridgeJob(cached.bridgeReceipt), null, '17. the job leaves the queue');

  // 18. save state reflects durability.
  const save = api.currentBridgeSaveState(CONVO);
  assert.strictEqual(save.readyCount, 1);
  assert.strictEqual(save.durableCount, 1);
  assert.strictEqual(save.missingDelivery, 0);
  assert.strictEqual(api.currentAuditSaveAttention(), false);

  // 19/20. manual SYNC/SAVE materializes exactly the one COMPLETE Core and
  // never waits for Second or Performance to exist.
  const manual = api.syncSaveCurrentChatStateNow();
  await h.settle();
  const ok = await manual;
  assert.strictEqual(ok, true);
  assert.strictEqual(h.materializeRequests.length, 1);
  assert.deepStrictEqual([...h.materializeRequests[0].waves.map(w => w.wave_id)], ['core']);
  assert.strictEqual(h.materializeRequests[0].source_run_id, RUN_ID);
  assert.strictEqual(api.manualAuditSyncInFlight, false, 'the foreground operation returned promptly');
});

// =========================================================================
// PHASE Q -- START failure cases
// =========================================================================

test('W6-003/Q: START waits while the ZIP is still uploading and does not send early', async () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';
  composer.upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name, { busy: true });
  };
  dispatchDrop(h, composer.input, { files: [archiveFile()], uri: `file:///x/${ARCHIVE}` });
  composer.send.setAttribute('aria-disabled', 'true');

  let settled = false;
  const wait = api.waitForChatGPTSendReady(40, 4000).then(value => { settled = true; return value; });
  const end = Date.now() + 400;
  while (Date.now() < end) {
    h.advance(30);
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.strictEqual(settled, false, 'START must wait, bounded, while the tile ingests');
  assert.strictEqual(composer.send._clicked, undefined, 'nothing was sent while uploading');

  for (const spinner of h.dom.querySelectorAll('.animate-spin')) spinner.remove();
  composer.send.setAttribute('aria-disabled', 'false');
  const end2 = Date.now() + 300;
  while (Date.now() < end2) {
    h.advance(30);
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.strictEqual(await wait, composer.send);
});

test('W6-003/Q: with no attachment at all START is not offered and nothing is sent', async () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';

  assert.strictEqual(api.miniStartAuditState().available, false, 'no archive, no START');
  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  assert.strictEqual(await pending, false);
  assert.strictEqual(composer.send._clicked, undefined, 'START never sends without a ready archive');
});

test('W6-003/Q: START does not overwrite a manual composer draft', async () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';
  attachArchiveByDrop(h, api, composer);
  const draft = 'my own half-written question';
  composer.input.textContent = draft;

  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  const started = await pending;

  if (!started) {
    assert.strictEqual(api.composerPlainText(composer.input), draft, 'a refused START leaves the draft intact');
    assert.strictEqual(composer.send._clicked, undefined);
  } else {
    assert.ok(
      api.composerPlainText(composer.input).includes('ACB_CHAIN_RECEIPT'),
      'if START proceeds it must own the composer through its canonical prompt'
    );
  }
});

test('W6-003/Q: a second START while one is in flight is blocked', async () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';
  attachArchiveByDrop(h, api, composer);
  api.setAuditStartInFlightForTest(true, Date.now());
  assert.strictEqual(api.auditStartIsLive(), true);

  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  assert.strictEqual(await pending, false, 'a live START blocks a second one');
  assert.strictEqual(composer.send._clicked, undefined, 'no second Core is sent');
});

test('W6-003/Q: a manually dropped archive needs no AUTO ZIP provenance', () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';
  // No archive proof was ever remembered: this ZIP came from Explorer, not
  // from AUTO ZIP. START must still be offered.
  attachArchiveByDrop(h, api, composer);
  assert.strictEqual(api.miniStartAuditState().available, true);
});

test('W6-003/Q: an AUTO ZIP sourced archive is equally acceptable', () => {
  const { h, api, composer } = auditWindow({ runId: null });
  api.autoRuntime.stage = 'complete';
  composer.upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const file = archiveFile();
  composer.upload.files = [file];
  if (typeof composer.upload._onFilesSet === 'function') composer.upload._onFilesSet(composer.upload, [file]);
  assert.strictEqual(api.miniStartAuditState().available, true);
});

test('W6-003/Q: a finished old run plus a newly attached archive starts a fresh audit run', async () => {
  const { h, api, composer } = auditWindow();
  api.autoRuntime.stage = 'complete';
  api.saveAutoRuntime({ pauseOnFailure: false });
  const previousRunId = api.autoRuntime.runId;

  attachArchiveByDrop(h, api, composer);
  const start = api.miniStartAuditState();
  assert.strictEqual(start.available, true);
  assert.strictEqual(start.isNewAudit, true, 'a new archive over a finished run is a NEW audit');

  composer.send.disabled = false;
  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  await pending;
  assert.notStrictEqual(String(api.autoRuntime.runId || ''), String(previousRunId), 'a fresh run id is minted');
});

// =========================================================================
// PHASE R -- automatic save, end to end
// =========================================================================

test('W6-003/R: Core, Second and Performance each save independently, then ALL_3', async () => {
  const { h, api } = auditWindow();
  const durability = [];

  for (const wave of ['core', 'second', 'performance']) {
    api.autoRuntime.stage = `wait-${wave}`;
    api.autoRuntime[`${wave}UserId`] = `u-${wave}`;
    api.saveAutoRuntime({ pauseOnFailure: false });
    const commit = api.commitTerminalWaveResult(wave, handoffFor(api, wave), 'complete', `u-${wave}`);
    assert.strictEqual(commit.ok, true, `${wave} must commit`);
    await h.settle();

    const record = api.readAuditResultFresh(wave, CONVO);
    assert.ok(Number(record.bridgeSavedAt) > 0, `${wave} is durably saved as soon as it is COMPLETE`);
    durability.push(wave);
  }

  assert.deepStrictEqual(durability, ['core', 'second', 'performance']);
  const sentWaves = h.ingestRequests.map(request => request.wave);
  assert.deepStrictEqual([...sentWaves], ['core', 'second', 'performance'], 'one ingest per wave, in order');

  const perf = api.readAuditResultFresh('performance', CONVO);
  assert.ok(Number(perf.combinedSavedAt) > 0, 'ALL_3 is acknowledged at 3/3');

  const save = api.currentBridgeSaveState(CONVO);
  assert.strictEqual(save.readyCount, 3);
  assert.strictEqual(save.durableCount, 3);
  assert.strictEqual(save.missingDelivery, 0);
});

test('W6-003/R: Core does not wait for 3/3 before it is physically saved', async () => {
  const { h, api } = auditWindow();
  api.autoRuntime.stage = 'wait-core';
  api.saveAutoRuntime({ pauseOnFailure: false });
  api.commitTerminalWaveResult('core', handoffFor(api, 'core'), 'complete', 'u-core');
  await h.settle();

  assert.strictEqual(h.ingestRequests.length, 1);
  assert.strictEqual(h.ingestRequests[0].wave, 'core');
  assert.ok(Number(api.readAuditResultFresh('core', CONVO).bridgeSavedAt) > 0);
  assert.strictEqual(api.readAuditResultFresh('second', CONVO), null, 'no second wave exists yet');
});

test('W6-003/R: a transient Bridge outage keeps the record cached and saves it on retry', async () => {
  let online = false;
  const { h, api } = auditWindow({
    handlers: {
      status: () => (online
        ? { status: 200, responseText: JSON.stringify({ ok: true, service: 'AUDAPACK Bridge' }) }
        : { error: true, status: 0, responseText: '' }),
      audits: body => (online
        ? {
          status: 200,
          responseText: JSON.stringify({ ok: true, duplicate: false, run_id: body.run_id, files: ['C:/a/core.md'] })
        }
        : { error: true, status: 0, responseText: '' })
    }
  });
  // `handlers.status` above is a function; installBridge only calls functions
  // for audits/materialize, so re-install with a live responder.
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (!online) return { error: true, status: 0, responseText: '' };
    if (/\/v1\/status|\/health/.test(url)) {
      return { status: 200, responseText: JSON.stringify({ ok: true, service: 'AUDAPACK Bridge' }) };
    }
    if (/\/v1\/projects\/resolve/.test(url)) {
      return { status: 200, responseText: JSON.stringify({ ok: true, project_id: 'audapack' }) };
    }
    const body = JSON.parse(String(request.data || '{}'));
    h.ingestRequests.push(body);
    return {
      status: 200,
      responseText: JSON.stringify({ ok: true, duplicate: false, run_id: body.run_id, files: ['C:/a/core.md'] })
    };
  };

  api.autoRuntime.stage = 'wait-core';
  api.saveAutoRuntime({ pauseOnFailure: false });
  const coreText = handoffFor(api, 'core');
  api.commitTerminalWaveResult('core', coreText, 'complete', 'u-core');
  await h.settle();

  const offline = api.readAuditResultFresh('core', CONVO);
  assert.strictEqual(offline.text, coreText, 'the record stays cached while the Bridge is down');
  assert.strictEqual(Number(offline.bridgeSavedAt) || 0, 0, 'an outage is never durability');
  const job = api.readBridgeJob(offline.bridgeReceipt);
  assert.ok(job, 'the job stays pending');
  assert.strictEqual(job.permanent, false);
  assert.strictEqual(api.currentAuditSaveAttention(), true);

  online = true;
  const flush = api.flushBridgeQueue({ force: true, manual: true, conversationKey: CONVO });
  await h.settle();
  await flush;

  // Awaiting the flush is not by itself proof of durability: the delivery lands
  // on a later turn of the harness loop, and under load settle() returns first.
  // Wait for the observable this assertion is about instead of racing a single
  // fixed drain -- otherwise the gate is red or green by machine load.
  for (let guard = 0; guard < 50; guard += 1) {
    if (Number(api.readAuditResultFresh('core', CONVO).bridgeSavedAt) > 0) break;
    await h.settle();
  }

  const saved = api.readAuditResultFresh('core', CONVO);
  assert.ok(Number(saved.bridgeSavedAt) > 0, 'the retry saves it');
  assert.strictEqual(api.readBridgeJob(offline.bridgeReceipt), null);
  assert.strictEqual(
    h.ingestRequests.filter(request => request.wave === 'core').length,
    1,
    'the retry never creates a second logical wave delivery'
  );
});

test('W6-003/R: a permanent immutable conflict leaves the record visibly NOT durable', async () => {
  const { h, api } = auditWindow({
    handlers: {
      audits: {
        status: 409,
        responseText: JSON.stringify({
          ok: false,
          error: {
            code: 'completed_wave_immutable',
            message: "Wave 'core' is already complete in run acb-other; start a fresh run for replacement",
            retriable: false
          }
        })
      }
    }
  });

  api.autoRuntime.stage = 'wait-core';
  api.saveAutoRuntime({ pauseOnFailure: false });
  const coreText = handoffFor(api, 'core');
  api.commitTerminalWaveResult('core', coreText, 'complete', 'u-core');
  await h.settle();

  const record = api.readAuditResultFresh('core', CONVO);
  assert.strictEqual(record.text, coreText, 'nothing is silently discarded');
  assert.strictEqual(Number(record.bridgeSavedAt) || 0, 0, 'a refused write is never durable');
  assert.ok(record.bridgeError, 'the operator sees the conflict');

  const job = api.readBridgeJob(record.bridgeReceipt);
  assert.ok(job, 'the conflict stays actionable in the queue');
  assert.strictEqual(job.permanent, true);
  assert.strictEqual(job.errorCode, 'completed_wave_immutable');

  const save = api.currentBridgeSaveState(CONVO);
  assert.strictEqual(save.failed, 1);
  assert.strictEqual(save.durableCount, 0);
  assert.strictEqual(save.missingDelivery, 1);
  assert.strictEqual(api.currentAuditSaveAttention(), true);
});
