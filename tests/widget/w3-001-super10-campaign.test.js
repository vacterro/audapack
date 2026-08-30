'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, assistantTurn, addTurns } = require('./helpers');

test('W3-001: embedded audit profiles manifest is loaded and valid', () => {
  const { api } = setup();
  assert.ok(api.EMBEDDED_AUDIT_PROFILES, 'EMBEDDED_AUDIT_PROFILES should be defined');
  assert.ok(api.AUDIT_PROFILES_MANIFEST_SHA256, 'AUDIT_PROFILES_MANIFEST_SHA256 should be defined');
  const prof = api.getActiveProfile();
  assert.ok(prof, 'getActiveProfile should return a profile');
  assert.strictEqual(prof.profile_id, 'super10');
  assert.strictEqual(prof.waves.length, 10);
});

test('W3-001: all 10 Super10 wave commands classify correctly', () => {
  const { api } = setup();
  const markers = [
    ['AUDIT ARCHITECTURE — wave 1/10', 'architecture'],
    ['AUDIT CORRECTNESS — wave 2/10', 'correctness'],
    ['AUDIT STATE — wave 3/10', 'state'],
    ['AUDIT FAILURE / RECOVERY — wave 4/10', 'recovery'],
    ['AUDIT RECOVERY — wave 4/10', 'recovery'],
    ['AUDIT SECURITY — wave 5/10', 'security'],
    ['AUDIT INTEGRATION — wave 6/10', 'integration'],
    ['AUDIT TESTS / VERIFICATION / CONTRACTS — wave 7/10', 'verification'],
    ['AUDIT VERIFICATION — wave 7/10', 'verification'],
    ['AUDIT PERFORMANCE / SCALABILITY / STABILITY / RESOURCE BOUNDS — wave 8/10', 'performance'],
    ['AUDIT UX / OPERATOR EFFECTIVENESS — wave 9/10', 'operator'],
    ['AUDIT OPERATOR — wave 9/10', 'operator'],
    ['AUDIT RED TEAM / ADVERSARIAL SYNTHESIS — wave 10/10', 'redteam'],
    ['AUDIT REDTEAM — wave 10/10', 'redteam']
  ];

  for (const [text, expected] of markers) {
    assert.strictEqual(api.classifyAuditMessage(text), expected, `Failed to classify: ${text}`);
  }
});

test('W3-001: all 10 Super10 CONTINUE commands classify correctly', () => {
  const { api } = setup();
  const continueMarkers = [
    ['AUDIT ARCHITECTURE CONTINUE — keep going', 'architecture'],
    ['AUDIT CORRECTNESS CONTINUE — keep going', 'correctness'],
    ['AUDIT STATE CONTINUE — keep going', 'state'],
    ['AUDIT RECOVERY CONTINUE — keep going', 'recovery'],
    ['AUDIT SECURITY CONTINUE — keep going', 'security'],
    ['AUDIT INTEGRATION CONTINUE — keep going', 'integration'],
    ['AUDIT VERIFICATION CONTINUE — keep going', 'verification'],
    ['AUDIT PERFORMANCE CONTINUE — keep going', 'performance'],
    ['AUDIT OPERATOR CONTINUE — keep going', 'operator'],
    ['AUDIT RED TEAM CONTINUE — keep going', 'redteam']
  ];

  for (const [text, expected] of continueMarkers) {
    assert.strictEqual(api.classifyAuditMessage(text), expected, `Failed to classify CONTINUE: ${text}`);
  }
});

test('W3-001: findWaveDefinitionForStageOrKind finds waves across all profiles', () => {
  const { api } = setup();
  const w1 = api.findWaveDefinitionForStageOrKind('architecture');
  assert.ok(w1);
  assert.strictEqual(w1.id, 'architecture');
  assert.strictEqual(w1.ordinal, 1);

  const w10 = api.findWaveDefinitionForStageOrKind('wait-redteam');
  assert.ok(w10);
  assert.strictEqual(w10.id, 'redteam');
  assert.strictEqual(w10.ordinal, 10);

  const q2 = api.findWaveDefinitionForStageOrKind('sending-second');
  assert.ok(q2);
  assert.strictEqual(q2.id, 'second');
});

test('W3-001: buildAuditWavePrompt generates valid wave prompts with required headers', () => {
  const { api } = setup();
  const prof = api.getActiveProfile();
  const wave1 = prof.waves[0];
  const prompt1 = api.buildAuditWavePrompt(prof, wave1, { runId: 'run-w3-test' });
  assert.ok(prompt1.includes('WAVE_ID: architecture'));
  assert.ok(prompt1.includes('CAMPAIGN_PROFILE: super10'));
  assert.ok(prompt1.includes('STATUS: AUDIT_ARCHITECTURE: COMPLETE'));
  assert.ok(prompt1.includes('ARCH_DONE_WHEN:'));

  const wave10 = prof.waves[9];
  const prompt10 = api.buildAuditWavePrompt(prof, wave10, { runId: 'run-w3-test' });
  assert.ok(prompt10.includes('WAVE_ID: redteam'));
  assert.ok(prompt10.includes('FINAL DEDUPLICATED IMPLEMENTATION HANDOFF SECTION'));
  assert.ok(prompt10.includes('SUPER_AUDIT_STATUS: COMPLETE'));
  assert.ok(prompt10.includes('SUPER_AUDIT_DONE_WHEN:'));
});

test('W3-001: buildAuditWavePrompt embeds concrete runtime run-id in CAMPAIGN_RUN_ID', () => {
  const { api } = setup();
  const prof = api.getActiveProfile();
  const wave1 = prof.waves[0];
  const prompt = api.buildAuditWavePrompt(prof, wave1, { runId: 'run-real-abc' });
  assert.ok(prompt.includes('CAMPAIGN_RUN_ID: run-real-abc'), 'prompt must contain the exact runtime run id');
  assert.ok(!prompt.includes('<run-id>'), 'prompt must not contain the placeholder <run-id>');
});

test('W3-001: COMPLETE integrity rejects placeholder/missing CAMPAIGN_RUN_ID for super10', () => {
  const { api } = setup();
  const prof = api.getActiveProfile();
  const wave1 = prof.waves[0];
  // Build a COMPLETE handoff WITHOUT a real run-id.
  const bodyNoRun = `PROJECT_NAME: Test
CAMPAIGN_PROFILE: super10
CAMPAIGN_RUN_ID: <run-id>
WAVE_ID: architecture
WAVE_INDEX: 1
WAVE_COUNT: 10
WAVE: AUDIT ARCHITECTURE
STATUS: AUDIT_ARCHITECTURE: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [ARC-001] Something
EVIDENCE: src/foo.js:20
DEFECT: It breaks.
REPAIR: Fix it.
VERIFY: Tests pass.
ARCH_DONE_WHEN: ARC-001 fixed.
`;
  const integrity = api.auditHandoffIntegrity('wait-architecture', bodyNoRun);
  assert.strictEqual(integrity.valid, false, 'placeholder run-id must be rejected');
  assert.strictEqual(integrity.reason, 'missing-campaign-run-id');
});

test('W3-001: Super10 10-wave clean progression lineage tracks completely', () => {
  const { h, api } = setup();
  const turns = [
    userTurn(h, 'u1', 'AUDIT ARCHITECTURE — wave 1/10'),
    userTurn(h, 'u2', 'AUDIT CORRECTNESS — wave 2/10'),
    userTurn(h, 'u3', 'AUDIT STATE — wave 3/10'),
    userTurn(h, 'u4', 'AUDIT FAILURE / RECOVERY — wave 4/10'),
    userTurn(h, 'u5', 'AUDIT SECURITY — wave 5/10'),
    userTurn(h, 'u6', 'AUDIT INTEGRATION — wave 6/10'),
    userTurn(h, 'u7', 'AUDIT TESTS / VERIFICATION / CONTRACTS — wave 7/10'),
    userTurn(h, 'u8', 'AUDIT PERFORMANCE — wave 8/10'),
    userTurn(h, 'u9', 'AUDIT UX / OPERATOR EFFECTIVENESS — wave 9/10'),
    userTurn(h, 'u10', 'AUDIT RED TEAM — wave 10/10')
  ];
  addTurns(h, turns);

  const lineage = api.visibleAuditLineage(turns);
  assert.strictEqual(lineage.blockedByReset, false);
  assert.ok(lineage.architecture, 'architecture should be in lineage');
  assert.ok(lineage.correctness, 'correctness should be in lineage');
  assert.ok(lineage.state, 'state should be in lineage');
  assert.ok(lineage.recovery, 'recovery should be in lineage');
  assert.ok(lineage.security, 'security should be in lineage');
  assert.ok(lineage.integration, 'integration should be in lineage');
  assert.ok(lineage.verification, 'verification should be in lineage');
  assert.ok(lineage.performance, 'performance should be in lineage');
  assert.ok(lineage.operator, 'operator should be in lineage');
  assert.ok(lineage.redteam, 'redteam should be in lineage');
});

test('W3-001: non-audit user message breaks Super10 lineage', () => {
  const { h, api } = setup();
  const u1 = userTurn(h, 'u1', 'AUDIT ARCHITECTURE — wave 1/10');
  const u2 = userTurn(h, 'u2', 'AUDIT CORRECTNESS — wave 2/10');
  const interrupt = userTurn(h, 'u-break', 'can you do something else?');
  const u3 = userTurn(h, 'u3', 'AUDIT STATE — wave 3/10');
  const turns = [u1, u2, interrupt, u3];
  addTurns(h, turns);

  const lineage = api.visibleAuditLineage(turns);
  assert.strictEqual(lineage.architecture, null, 'Previous lineage should be severed');
  assert.strictEqual(lineage.correctness, null, 'Previous lineage should be severed');
  assert.strictEqual(lineage.state, null, 'State wave without satisfied dependencies should not resolve');
});

test('W3-001: AUDIT CORE seeds wave 1 (architecture) under Super10', () => {
  const { h, api } = setup();
  const u1 = userTurn(h, 'u1', 'AUDIT CORE\nThe complete command is attached as "AUDIT_CORE.md"');
  const turns = [u1];
  addTurns(h, turns);

  const lineage = api.visibleAuditLineage(turns);
  assert.strictEqual(lineage.blockedByReset, false);
  assert.ok(lineage.architecture, 'AUDIT CORE should populate wave 1 (architecture) in Super10 lineage');
  assert.strictEqual(lineage.architecture, u1);
});
