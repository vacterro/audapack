'use strict';

// browserWorkerRecoverIdleEngine armed the engine and then asked
// reassertA3FromMachineReceipt to adopt the visible turn -- but that function
// refuses an already-enabled runtime on its first line, by design: it exists to
// repair a BLANK one. So the recovery could never repair anything. Live: six
// A10 windows logged "armed, no machine turn to adopt yet" every six seconds
// for sixteen minutes with the finished ARCHITECTURE answer on screen, and not
// one wave was harvested.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture } = require('./helpers');

function armedWorker() {
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
  return { h, api };
}

test('W16: the dispatch recovery is not wired to a function that refuses armed runtimes', () => {
  const { api } = armedWorker();
  const source = api.browserWorkerRecoverIdleEngine.toString();
  assert.match(source, /reconcileEnabledIdleAuditRuntime/,
    'the enabled+idle state needs the reconciler built for it');
});

test('W16: reassertA3FromMachineReceipt refuses once the engine is armed', () => {
  // Pinning the reason the old wiring was dead, so it cannot be restored.
  const { h, api } = armedWorker();
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true });
  assert.strictEqual(api.reassertA3FromMachineReceipt('c:abc'), false);
});

test('W16: the reconciler walks the ACTIVE profile, so A10 opens on architecture', () => {
  const { api } = armedWorker();
  const prof = api.getActiveProfile();
  assert.strictEqual(prof.waves[0].id, 'architecture');
  // quick3's CORE is not in super10 at all: a core-only adopter finds nothing.
  assert.ok(!prof.waves.some(w => w.id === 'core'));
});
