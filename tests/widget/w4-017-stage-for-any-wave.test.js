'use strict';

// stageForAuditKind hardcoded the quick3 three and returned '' for anything
// else. So an A10 run, whose wave 1 is ARCHITECTURE, got no stage;
// resumeRuntimeFromAuditTurn refused on its `!stage` guard; and the whole
// recovery chain above it had nothing to stand on. Live: six windows with the
// finished ARCHITECTURE answer on screen logging "no machine turn to adopt
// yet" every six seconds, two batches killed, not one wave ever harvested.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, assistantTurn, addTurns, composerFixture } = require('./helpers');

const ARCH_TURN = 'AUDIT ARCHITECTURE / SYSTEM INVARIANTS\n'
  + 'The complete command is attached as "AUDIT_ARCHITECTURE_176jhkn.md".\n'
  + 'Treat that attached file as my full instruction for this turn and execute it exactly;'
  + ' do not merely summarize the file.\n'
  + 'ACB_CHAIN_RECEIPT: startcore-mtlcgjyk-6192120616dd';

function a10Window() {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/c/abc?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/c/abc',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  composerFixture(h);
  api.state.bridgeEnabled = true;
  api.state.auditProfile = 'super10';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'super10' });
  api.autoRuntime.stage = 'idle';
  return { h, api };
}

test('W17: every wave of the active profile maps to a stage, not just quick3 three', () => {
  const { api } = a10Window();
  for (const wave of api.getActiveProfile().waves) {
    assert.strictEqual(api.stageForAuditKind(wave.id), `wait-${wave.id}`, wave.id);
  }
});

test('W17: the stage mapping round-trips both ways', () => {
  const { api } = a10Window();
  assert.strictEqual(api.auditKindForStage('wait-architecture'), 'architecture');
  assert.strictEqual(api.auditKindForStage('wait-core'), 'core');
  assert.strictEqual(api.auditKindForStage('idle'), '');
  assert.strictEqual(api.auditKindForStage(''), '');
  assert.strictEqual(api.auditKindForStage('wait-nonsense'), '');
});

test('W17: an A10 wave-1 turn is resumable, so the idle engine can be repaired', () => {
  const { h, api } = a10Window();
  const user = userTurn(h, 'u1', ARCH_TURN);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'ARCH-001 ...\nSTATUS: AUDIT_ARCHITECTURE: COMPLETE\nTICKETS: 3';
  addTurns(h, [user, assistant]);
  const turns = api.getChatGPTTurns();

  assert.strictEqual(api.classifyAuditTurn(user), 'architecture');
  assert.strictEqual(api.resumeRuntimeFromAuditTurn(user, { turns }), true);
  assert.strictEqual(api.autoRuntime.stage, 'wait-architecture');
});

test('W17: the enabled+idle reconciler now repairs an A10 window', () => {
  const { h, api } = a10Window();
  const user = userTurn(h, 'u1', ARCH_TURN);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'ARCH-001 ...\nSTATUS: AUDIT_ARCHITECTURE: COMPLETE\nTICKETS: 3';
  addTurns(h, [user, assistant]);

  assert.strictEqual(api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns()), true);
  assert.notStrictEqual(api.autoRuntime.stage, 'idle');
});
