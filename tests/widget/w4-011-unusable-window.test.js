'use strict';

// A managed worker window signed out of ChatGPT lands on the marketing page:
// no composer, no eligibility, so startBrowserWorker refused and the window
// never registered at all -- invisible to the pool and to the operator, who
// saw W 1/6 with nothing anywhere saying why.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function managedWindow(options = {}) {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: options.pathname || '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  return { h, api };
}

test('W11: a signed-out window names the reason', () => {
  const { h, api } = managedWindow();
  // No composer at all, and a way in offered: the logged-out landing page.
  const login = h.el('button', { 'aria-label': 'Log in' });
  h.dom.querySelector('body').appendChild(login);

  assert.strictEqual(api.chatGPTSignedOut(), true);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'signed-out');
});

test('W11: a signed-in window with a composer is not called signed out', () => {
  const { h, api } = managedWindow();
  composerFixture(h);
  assert.strictEqual(api.chatGPTSignedOut(), false);
  assert.notStrictEqual(api.browserWorkerUnusableReason(), 'signed-out');
});

test('W11: a window parked on a conversation says so, not "signed out"', () => {
  const { h, api } = managedWindow({ pathname: '/c/abc123' });
  composerFixture(h);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'not-on-root-chat');
});

test('W11: a window with no composer and no login offer is not guessed at', () => {
  const { api } = managedWindow();
  assert.strictEqual(api.chatGPTSignedOut(), false);
  assert.strictEqual(api.browserWorkerUnusableReason(), 'no-composer');
});

test('W11: a dispatched run arms the audit engine', () => {
  // The Bridge sends the Core and moves the lane to AUDITING, but harvesting
  // the response and committing the wave is the widget's own auto engine, and
  // a worker window's engine is off by default: a CM audit ran for 11m34s,
  // produced a valid terminal handoff in the chat, and nothing was saved.
  const { h, api } = managedWindow();
  composerFixture(h);
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  assert.strictEqual(Boolean(api.autoRuntime.enabled), false);

  api.browserWorkerArmAuditEngine();
  assert.strictEqual(Boolean(api.autoRuntime.enabled), true);
});

test('W11: arming an engine that is already on is a no-op', () => {
  const { h, api } = managedWindow();
  composerFixture(h);
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), runId: 'acb-live' };
  assert.strictEqual(api.browserWorkerArmAuditEngine(), true);
  assert.strictEqual(api.autoRuntime.runId, 'acb-live', 'an armed run must not be disturbed');
});
