'use strict';

// A reload only re-runs the script the userscript manager already holds. When
// it has nothing newer, the window comes back on the same build -- and asking
// again every cooldown churned three idle worker windows forever without ever
// changing anything. Try each required build once, then say so.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function workerWindow() {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  api.browserWorkerLease = null;
  return { h, api };
}

test('W13: the first sight of a required build reloads the window', () => {
  const { api } = workerWindow();
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), true);
});

test('W13: the same required build is never chased twice', () => {
  const { api } = workerWindow();
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), true);
  // The window came back on the same stale build: reloading again cannot help.
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), false);
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), false);
});

test('W13: coming back stale is reported once, as a warning', () => {
  const { api } = workerWindow();
  api.browserWorkerReloadForStaleBuild('0.0.36');
  api.browserWorkerReloadForStaleBuild('0.0.36');
  api.browserWorkerReloadForStaleBuild('0.0.36');

  const stuck = api.readBridgeDiagnosticLog().filter(e => e.event === 'worker_stale_build_stuck');
  assert.strictEqual(stuck.length, 1, 'the operator is told once, not on every poll');
  assert.strictEqual(stuck[0].severity, 'warn');
});

test('W13: a genuinely newer required build is chased again', () => {
  const { api } = workerWindow();
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), true);
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.36'), false);
  // The Bridge shipped another build: that one is worth one reload of its own.
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.37'), true);
});

test('W13: an owned run is never interrupted to chase a build', () => {
  const { api } = workerWindow();
  assert.strictEqual(
    api.browserWorkerReloadForStaleBuild('0.0.36', { dispatch_id: 'dsp-0123456789abcdef' }),
    false
  );
});
