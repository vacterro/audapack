'use strict';

// W9: ChatGPT rolled out the Work surface as the default landing on `/` for
// some accounts. A managed worker parked there looks CLEAN (root path, empty
// composer, zero turns) but every send would burn limited Work usage instead
// of running the audit. The widget must detect the Work composer, refuse to
// claim while it is active, and switch the window back to the normal chat on
// its own.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function managedSetup() {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  return { h, api };
}

function workComposer(h) {
  const input = h.dom.querySelector('#prompt-textarea');
  input.setAttribute('placeholder', 'Work on anything');
  return input;
}

function chatSwitch(h, input, target = 'form[data-type="unified-composer"]') {
  const chatOption = h.el('button', { 'aria-label': 'Chat' });
  chatOption.addEventListener('click', () => {
    input.setAttribute('placeholder', 'Ask anything');
  });
  h.dom.querySelector(target).appendChild(chatOption);
  return chatOption;
}

test('W9: the Work landing is detected and never looks CLEAN', () => {
  const { h, api } = managedSetup();
  workComposer(h);

  const snapshot = api.browserWorkerSnapshot();
  assert.strictEqual(snapshot.work_surface, true);
  assert.strictEqual(snapshot.clean_for_audit, false);
  assert.strictEqual(snapshot.worker_class, 'OCCUPIED');
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerClaimBlockReason(), 'worker-in-work-mode');
});

test('W9: the plain chat composer is not the Work surface', () => {
  const { api } = managedSetup();

  const snapshot = api.browserWorkerSnapshot();
  assert.strictEqual(snapshot.work_surface, false);
  assert.strictEqual(snapshot.clean_for_audit, true);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
});

test('W9: a ChatGPT hero alone does not flag the surface without the composer', () => {
  const { h, api } = managedSetup();
  // No Work marker on the composer; the page text check must not fire on an
  // arbitrary body (the harness body has no innerText).
  const input = h.dom.querySelector('#prompt-textarea');
  input.setAttribute('placeholder', 'Ask anything');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), false);
});

test('W9: ensureChatMode clicks the exact Chat control and re-checks the composer', () => {
  const { h, api } = managedSetup();
  const input = workComposer(h);
  chatSwitch(h, input);

  assert.strictEqual(api.chatGPTWorkSurfaceActive(), true);
  const outcome = api.chatGPTEnsureChatMode();
  assert.strictEqual(outcome.ok, true);
  assert.strictEqual(outcome.action, 'switched');
  assert.strictEqual(api.browserWorkerCanClaim(), true);
});

test('W9: a control that merely contains "chat" is never clicked', () => {
  const { h, api } = managedSetup();
  const input = workComposer(h);
  const decoy = h.el('button', { 'aria-label': 'New chat' });
  decoy.addEventListener('click', () => {
    input.setAttribute('placeholder', 'Ask anything');
  });
  h.dom.querySelector('form[data-type="unified-composer"]').appendChild(decoy);

  const outcome = api.chatGPTEnsureChatMode();
  assert.strictEqual(outcome.ok, false);
  assert.strictEqual(outcome.action, 'no-switch-control');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), true);
});

test('W9: managed worker housekeeping returns the Work composer to chat on its own', () => {
  const { h, api } = managedSetup();
  const input = workComposer(h);
  chatSwitch(h, input);

  assert.strictEqual(api.browserWorkerEnsureChatModeHousekeeping(), true);
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), false);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_work_mode_switched'), JSON.stringify(log));
});

test('W9: a window owning a dispatch is never switched', () => {
  const { h, api } = managedSetup();
  const input = workComposer(h);
  chatSwitch(h, input);
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-1-1-x',
    lease_id: 'lease-1'
  };

  assert.strictEqual(api.browserWorkerEnsureChatModeHousekeeping(), false);
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), true);
});

test('W9: an unswitchable Work surface stays blocked and reports once', () => {
  const { h, api } = managedSetup();
  workComposer(h);

  assert.strictEqual(api.browserWorkerEnsureChatModeHousekeeping(), false);
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerClaimBlockReason(), 'worker-in-work-mode');
  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_work_mode_blocked'), JSON.stringify(log));
});

test('W9: the widget never clicks its own CHAT button', () => {
  // The widget renders a toolbar with a CHAT button. Searching the whole
  // document for a control named "chat" found OUR button first and clicked it
  // forever instead of ChatGPT's toggle.
  const { h, api } = managedSetup();
  workComposer(h);
  const ours = h.el('button', { 'aria-label': 'Chat', id: 'acb-bar-chat' });
  h.dom.querySelector('body').appendChild(ours);

  assert.strictEqual(api.chatGPTWorkModeSwitchControl?.() ?? null, null);
  const outcome = api.chatGPTEnsureChatMode();
  assert.strictEqual(outcome.action, 'no-switch-control');
  assert.ok(!(outcome.names || []).includes('chat'), JSON.stringify(outcome.names));
});

test('W9: an unfindable switch reports the controls the page actually offers', () => {
  const { h, api } = managedSetup();
  workComposer(h);
  const other = h.el('button', { 'aria-label': 'Upgrade' });
  h.dom.querySelector('form[data-type="unified-composer"]').appendChild(other);

  const outcome = api.chatGPTEnsureChatMode();
  assert.strictEqual(outcome.ok, false);
  assert.ok((outcome.names || []).includes('upgrade'), JSON.stringify(outcome.names));
});

test('W9: a managed window may fall back to New chat, an ordinary call may not', () => {
  const { h, api } = managedSetup();
  const input = workComposer(h);
  const fresh = h.el('button', { 'aria-label': 'New chat' });
  fresh.addEventListener('click', () => { input.setAttribute('placeholder', 'Ask anything'); });
  h.dom.querySelector('body').appendChild(fresh);

  assert.strictEqual(api.chatGPTEnsureChatMode().action, 'no-switch-control');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), true);

  const allowed = api.chatGPTEnsureChatMode({ allowNewChat: true });
  assert.strictEqual(allowed.ok, true);
  assert.strictEqual(allowed.action, 'new-chat');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), false);
});
