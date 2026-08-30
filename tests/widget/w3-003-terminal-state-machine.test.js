'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture, runtimeFixture, userTurn, assistantTurn, addTurns } = require('./helpers');

function createSampleHandoff(waveDef, options = {}) {
  const pfx = waveDef.ticket_prefix.replace(/-$/, '');
  const termKey = waveDef.terminal_status_key || waveDef.slug;
  const statusState = options.statusState || 'COMPLETE';
  const statusLine = options.customStatusLine || `STATUS: ${termKey}: ${statusState}`;
  const ticketsCount = options.ticketCount !== undefined ? options.ticketCount : 2;

  let body = `
PROJECT_NAME: ${options.projectName || 'AUDAPACK'}
DATE_TIME: 2026-08-27T16:03:00+03:00
CAMPAIGN_PROFILE: ${options.profileId || 'quick3'}
CAMPAIGN_RUN_ID: ${options.runId || `run-${(options.profileId || 'quick3')}-test`}
WAVE_ID: ${waveDef.id}
WAVE_INDEX: ${waveDef.ordinal}
WAVE_COUNT: ${options.waveCount || 3}
WAVE: ${waveDef.wave_header}
${statusLine}
TICKETS: ${ticketsCount}
HANDOFF: IMPLEMENTATION_AGENT
`;

  if (ticketsCount === 0) {
    const noFindings = waveDef.no_findings_marker || `NO VERIFIED ${pfx} DEFECTS.`;
    body += `\n${noFindings}\n`;
  } else {
    for (let i = 1; i <= ticketsCount; i += 1) {
      const numStr = String(i).padStart(3, '0');
      body += `
[P1] [${pfx}-${numStr}] Sample defect issue title
EVIDENCE: Verified in codebase.
DEFECT: Sample defect explanation.
REPAIR: Proposed fix.
OPTIMIZE: Proposed optimization.
ISSUE: Sample performance issue.
GUARDRAIL: Safety guard.
VERIFY: Run tests.
`;
    }
  }

  const doneMarkerLabel = waveDef.done_marker.replace(/:\/s*$/, '');
  body += `\n${doneMarkerLabel}: All tickets and handoffs are verified.\n`;
  return body.trim();
}

test('W3-003: Manifest-driven terminal status matrix across all profiles and waves', () => {
  const { api } = setup();
  const manifest = api.EMBEDDED_AUDIT_PROFILES || {};
  const profiles = manifest.profiles || {};

  assert.ok(Object.keys(profiles).length >= 2, 'Must have at least quick3 and super10 profiles');

  for (const [profId, prof] of Object.entries(profiles)) {
    for (const waveDef of prof.waves) {
      const stage = `wait-${waveDef.id}`;

      // 1. COMPLETE
      const completeBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        statusState: 'COMPLETE'
      });
      const gateComplete = api.responseGate(stage, completeBody);
      const integrityComplete = api.auditHandoffIntegrity(stage, completeBody);
      assert.strictEqual(
        gateComplete,
        'complete',
        `Wave ${waveDef.id} in ${profId} with STATUS: ${waveDef.terminal_status_key}: COMPLETE must evaluate to 'complete'`
      );
      assert.strictEqual(
        integrityComplete.valid,
        true,
        `Wave ${waveDef.id} in ${profId} complete handoff must have valid integrity`
      );

      // 2. Canonical status_line must also evaluate to COMPLETE
      const canonicalStatusBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        customStatusLine: waveDef.status_line
      });
      const gateCanonical = api.responseGate(stage, canonicalStatusBody);
      assert.strictEqual(
        gateCanonical,
        'complete',
        `Wave ${waveDef.id} in ${profId} with canonical status_line (${waveDef.status_line}) must evaluate to 'complete'`
      );

      // 3. PARTIAL
      const partialBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        statusState: 'PARTIAL'
      });
      const gatePartial = api.responseGate(stage, partialBody);
      assert.strictEqual(
        gatePartial,
        'partial',
        `Wave ${waveDef.id} in ${profId} with STATUS: ${waveDef.terminal_status_key}: PARTIAL must evaluate to 'partial'`
      );

      // 4. BLOCKED
      const blockedBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        statusState: 'BLOCKED'
      });
      const gateBlocked = api.responseGate(stage, blockedBody);
      assert.strictEqual(
        gateBlocked,
        'blocked',
        `Wave ${waveDef.id} in ${profId} with STATUS: ${waveDef.terminal_status_key}: BLOCKED must evaluate to 'blocked'`
      );

      // 5. Malformed status key -> unknown
      const malformedBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        customStatusLine: 'STATUS: UNKNOWN_CORRUPTED_KEY: COMPLETE'
      });
      const gateMalformed = api.responseGate(stage, malformedBody);
      assert.strictEqual(
        gateMalformed,
        'unknown',
        `Wave ${waveDef.id} in ${profId} with malformed status key must evaluate to 'unknown'`
      );

      // 6. Zero-ticket handoff
      const zeroTicketBody = createSampleHandoff(waveDef, {
        profileId: profId,
        waveCount: prof.waves.length,
        ticketCount: 0,
        statusState: 'COMPLETE'
      });
      const gateZero = api.responseGate(stage, zeroTicketBody);
      const integrityZero = api.auditHandoffIntegrity(stage, zeroTicketBody);
      assert.strictEqual(
        gateZero,
        'complete',
        `Wave ${waveDef.id} in ${profId} zero-ticket handoff must evaluate to 'complete'`
      );
      assert.strictEqual(
        integrityZero.valid,
        true,
        `Wave ${waveDef.id} in ${profId} zero-ticket handoff must have valid integrity`
      );
    }
  }
});

test('W3-003: Exact user reproduction fixture for Quick3 Second Wave (W2 COMPLETE with 16 tickets)', () => {
  const { api } = setup();

  const waveDef = api.findWaveDefinitionForStageOrKind('second');
  assert.ok(waveDef, 'Second wave definition must exist in Quick3');

  let fixture = `
PROJECT_NAME: SAIPEN
DATE_TIME: 2026-08-27T16:49:12+03:00
CAMPAIGN_PROFILE: quick3
CAMPAIGN_PROFILE_VERSION: 1.0.0
CAMPAIGN_RUN_ID: second-mtbgrqj6-63aa207a5d39
CAMPAIGN_MANIFEST_SHA256: 0150e79661d7b03b2fee434b93ea0cec2ec584e2c68ebb375b7d41bfbd13ff87
WAVE_ID: second
WAVE_INDEX: 2
WAVE_COUNT: 3
WAVE: AUDIT SECOND WAVE
TARGET::SAIPEN_27.08.26-T06-27-48.zip -> fully unpacked SAIPEN project
STATUS: SECOND_WAVE: COMPLETE
TICKETS: 16
HANDOFF: IMPLEMENTATION_AGENT
`;

  for (let i = 1; i <= 16; i += 1) {
    const num = String(i).padStart(3, '0');
    fixture += `
[P1] [W2-${num}] Second wave finding #${i}
EVIDENCE: Verified in test recovery archive.
DEFECT: Flaw description for defect #${i}.
REPAIR: Concrete repair instruction.
VERIFY: Automated tests pass.
`;
  }

  fixture += `\nSECOND_WAVE_DONE_WHEN: All 16 tickets verified and resolved cleanly.\n`;

  const integrity = api.auditHandoffIntegrity('wait-second', fixture);
  assert.strictEqual(integrity.valid, true, 'Integrity must be valid for 16 tickets');
  assert.strictEqual(integrity.found, 16, 'Found tickets count must be 16');

  const gate = api.responseGate('wait-second', fixture);
  assert.strictEqual(gate, 'complete', 'Response gate must be COMPLETE for STATUS: SECOND_WAVE: COMPLETE');
});

test('W3-003: commitTerminalWaveResult atomically commits COMPLETE and advances Quick3 W2 to Performance', () => {
  const { api } = setup();

  api.autoRuntime = runtimeFixture({
    stage: 'wait-second',
    enabled: true,
    runId: 'run-w3-test',
    coreUserId: 'turn-user-1',
    secondUserId: 'turn-user-2',
    continuationKind: 'second',
    continuationReason: 'partial'
  });

  const waveDef = api.findWaveDefinitionForStageOrKind('second');
  const completeBody = createSampleHandoff(waveDef, {
    profileId: 'quick3',
    ticketCount: 2,
    statusState: 'COMPLETE'
  });

  const res = api.commitTerminalWaveResult('second', completeBody, 'complete', 'turn-user-2');
  assert.strictEqual(res.ok, true, 'Terminal commit must succeed');
  assert.strictEqual(res.terminal, 'complete');
  assert.strictEqual(res.nextWave, 'performance');

  // Verify atomic runtime transition
  assert.strictEqual(api.autoRuntime.stage, 'sending-performance', 'Stage must advance to sending-performance');
  assert.strictEqual(api.autoRuntime.currentWaveId, 'performance', 'currentWaveId must be performance');
  assert.strictEqual(api.autoRuntime.continuationKind, '', 'continuationKind must be cleared');
  assert.strictEqual(api.autoRuntime.continuationReason, '', 'continuationReason must be cleared');

  // Verify cached result
  const cached = api.readAuditResultFresh('second', api.autoRuntime.conversationKey);
  assert.ok(cached, 'Result must be cached');
  assert.strictEqual(cached.gateState, 'complete');
});

test('W3-003: PARTIAL -> CONTINUE -> COMPLETE state machine integration with active anchor resolution', () => {
  const { h, api } = setup();
  composerFixture(h);

  const uCore = userTurn(h, 'u-core', 'AUDIT CORE\nPROJECT_NAME: AUDAPACK');
  const aCore = assistantTurn(h, 'a-core', (el) => {
    el._text = createSampleHandoff(api.findWaveDefinitionForStageOrKind('core'), { statusState: 'COMPLETE' });
  });

  const uSecondRoot = userTurn(h, 'u-second-root', 'AUDIT SECOND WAVE');
  const aSecondPartial = assistantTurn(h, 'a-second-partial', (el) => {
    el._text = createSampleHandoff(api.findWaveDefinitionForStageOrKind('second'), { statusState: 'PARTIAL' });
  });

  addTurns(h, [uCore, aCore, uSecondRoot, aSecondPartial]);

  api.autoRuntime = runtimeFixture({
    stage: 'wait-second',
    enabled: true,
    runId: 'run-pcc',
    coreUserId: 'u-core',
    secondUserId: 'u-second-root'
  });

  // Verify initial partial detection
  const gatePartial = api.responseGate('wait-second', aSecondPartial._text);
  assert.strictEqual(gatePartial, 'partial');

  // Now append continuation turn
  const uSecondCont = userTurn(h, 'u-second-cont', 'AUDIT SECOND WAVE CONTINUE — partial');
  const aSecondComplete = assistantTurn(h, 'a-second-complete', (el) => {
    el._text = createSampleHandoff(api.findWaveDefinitionForStageOrKind('second'), { statusState: 'COMPLETE' });
  });
  addTurns(h, [uSecondCont, aSecondComplete]);

  // Rebuild runtime from continuation turn
  const resumed = api.resumeRuntimeFromAuditTurn(uSecondCont);
  assert.strictEqual(resumed, true, 'resumeRuntimeFromAuditTurn on continuation turn must succeed');

  // Verify that active wave anchor points to continuation turn and stage advanced to performance
  assert.strictEqual(api.autoRuntime.secondUserId, 'u-second-cont');
  assert.strictEqual(api.autoRuntime.stage, 'sending-performance');
  assert.strictEqual(api.autoRuntime.currentWaveId, 'performance');

  // Verify visible lineage still identifies the root turn
  const lineage = api.visibleAuditLineage(api.getChatGPTTurns());
  assert.strictEqual(api.getTurnId(lineage.second), 'u-second-root', 'Lineage root must stay u-second-root');
});

test('W3-003: COMPLETE dominates queued continuation timer / preemption race condition', async () => {
  const { api } = setup();

  api.autoRuntime = runtimeFixture({
    stage: 'sending-continuation',
    enabled: true,
    continuationKind: 'second',
    continuationReason: 'partial',
    secondUserId: 'turn-user-2'
  });

  // Commit terminal complete result
  const waveDef = api.findWaveDefinitionForStageOrKind('second');
  const completeBody = createSampleHandoff(waveDef, { statusState: 'COMPLETE' });
  api.commitTerminalWaveResult('second', completeBody, 'complete', 'turn-user-2');

  // Now attempt to execute sendAutoAuditContinuation
  const sent = await api.sendAutoAuditContinuation('second', 'partial');
  assert.strictEqual(sent, false, 'Continuation send must be rejected when wave is already COMPLETE');
  assert.strictEqual(api.autoRuntime.stage, 'sending-performance', 'Stage must remain sending-performance');
});

test('W3-003: Last wave COMPLETE transitions campaign to complete stage', () => {
  const { api } = setup();

  api.autoRuntime = runtimeFixture({
    stage: 'wait-performance',
    enabled: true,
    runId: 'run-last-wave',
    coreUserId: 'u-1',
    secondUserId: 'u-2',
    performanceUserId: 'u-3'
  });

  const waveDef = api.findWaveDefinitionForStageOrKind('performance');
  const completeBody = createSampleHandoff(waveDef, {
    profileId: 'quick3',
    ticketCount: 1,
    statusState: 'COMPLETE'
  });

  const res = api.commitTerminalWaveResult('performance', completeBody, 'complete', 'u-3');
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.terminal, 'complete');
  assert.strictEqual(res.campaignComplete, true);
  assert.strictEqual(api.autoRuntime.stage, 'complete', 'Stage must transition to complete');
});
