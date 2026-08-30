'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

test('W3-004: buildAuditWavePrompt generates CAMPAIGN CONTEXT for Super10', () => {
  const { api } = setup();
  const profile = api.getActiveProfile('super10');
  assert.ok(profile);
  assert.strictEqual(profile.profile_id, 'super10');
  assert.strictEqual(profile.waves.length, 10);

  const wave1 = profile.waves[0];
  const prompt = api.buildAuditWavePrompt(profile, wave1);
  assert.match(prompt, /CAMPAIGN CONTEXT/);
  assert.match(prompt, /This audit belongs to a multi-wave Super10 campaign/);
  assert.match(prompt, /CAMPAIGN_PROFILE: super10/);
  assert.match(prompt, /WAVE_ID: architecture/);
});

test('W3-004: writeAuditResult and readAuditResult support dynamic Super10 waves', () => {
  const { api } = setup();
  const convoKey = api.currentConversationKey();
  api.clearAuditResultsForConversation(convoKey);

  const testWaveIds = ['architecture', 'state', 'recovery', 'security', 'integration', 'verification', 'performance', 'operator', 'redteam'];
  for (const waveId of testWaveIds) {
    const record = {
      version: 1,
      conversationKey: convoKey,
      runId: 'run-super10-test',
      kind: waveId,
      text: `PROJECT_NAME: SAIPEN\nSTATUS: ${waveId.toUpperCase()}: COMPLETE\nTICKETS: 2\n[P1] [T-001] test defect`
    };
    const saved = api.writeAuditResult(record);
    assert.strictEqual(saved, true, `Failed to save wave ${waveId}`);

    const read = api.readAuditResult(waveId, convoKey);
    assert.ok(read, `Failed to read wave ${waveId}`);
    assert.strictEqual(read.kind, waveId);
    assert.match(read.text, /PROJECT_NAME: SAIPEN/);
  }

  // Verify clearing works across all waves
  api.clearAuditResultsForConversation(convoKey);
  for (const waveId of testWaveIds) {
    const read = api.readAuditResult(waveId, convoKey);
    assert.strictEqual(read, null, `Wave ${waveId} was not cleared`);
  }
});

test('W3-004: setWaveUserId tracks waveAnchors correctly', () => {
  const { api } = setup();
  api.bindAutoRuntimeToCurrentConversation();
  const convoKey = api.currentConversationKey();
  
  api.setWaveUserId('architecture', 'turn-arch-001');
  api.saveAutoRuntime();
  let stored = api.loadAutoRuntime(convoKey);
  assert.strictEqual(stored.waveUserIds['architecture'], 'turn-arch-001');
  assert.ok(stored.waveAnchors['architecture']);
  assert.strictEqual(stored.waveAnchors['architecture'].rootUserId, 'turn-arch-001');
  assert.strictEqual(stored.waveAnchors['architecture'].activeUserId, 'turn-arch-001');
  assert.strictEqual(stored.waveAnchors['architecture'].status, 'active');

  // Updating user turn for second wave
  api.setWaveUserId('correctness', 'turn-corr-002');
  api.saveAutoRuntime();
  stored = api.loadAutoRuntime(convoKey);
  assert.strictEqual(stored.waveUserIds['correctness'], 'turn-corr-002');
  assert.strictEqual(stored.waveAnchors['correctness'].activeUserId, 'turn-corr-002');
});

test('W3-004: first A10 COMPLETE advances to 2/10 instead of terminal DONE', () => {
  const { api } = setup();
  api.state.auditProfile = 'super10';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'super10' });
  api.autoRuntime.conversationKey = 'c:abc123';
  api.autoRuntime.stage = 'wait-architecture';
  api.autoRuntime.currentWaveId = 'architecture';
  api.autoRuntime.currentWaveIndex = 1;
  api.autoRuntime.runId = 'run-super10-advance';
  api.autoRuntime.startedAt = Date.now();
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  const handoff = `
PROJECT_NAME: AUDAPACK
WAVE: AUDIT ARCHITECTURE / SYSTEM INVARIANTS
STATUS: AUDIT_ARCHITECTURE: COMPLETE
TICKETS: 0
HANDOFF: IMPLEMENTATION_AGENT

NO VERIFIED ARCHITECTURAL DEFECTS.
ARCH_DONE_WHEN: Architecture wave fully inspected and verified.
`;

  const result = api.commitTerminalWaveResult('architecture', handoff, 'complete', 'turn-architecture');
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.nextWave, 'correctness');
  assert.strictEqual(api.autoRuntime.stage, 'sending-correctness');
  assert.strictEqual(api.autoRuntime.currentWaveIndex, 2);
  assert.notStrictEqual(api.superCompactAutoLabel(), 'DONE');
});
