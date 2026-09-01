'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

test('SRC-005 worker snapshot identifies stable tab and safe FREE state', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  h.location.href = 'https://chatgpt.com/';
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  const snapshot = api.browserWorkerSnapshot();
  assert.ok(snapshot.worker_id);
  assert.strictEqual(snapshot.site, 'chatgpt');
  assert.strictEqual(snapshot.browser_name, 'Brave');
  assert.strictEqual(snapshot.is_brave, true);
  assert.strictEqual(snapshot.is_chromium, true);
  assert.strictEqual(snapshot.page_eligible, true);
  assert.strictEqual(snapshot.state, 'FREE');
  assert.strictEqual(snapshot.generating, false);
  assert.strictEqual(snapshot.has_manual_draft, false);
  assert.strictEqual(snapshot.has_attachments, false);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
});

test('T07 managed worker launch identity is reported without changing eligibility', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  h.location.search = '?audapack_worker_slot=4&audapack_worker_generation=9';
  const snapshot = api.browserWorkerSnapshot();
  assert.strictEqual(snapshot.managed_slot, 4);
  assert.strictEqual(snapshot.managed_generation, 9);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
  h.location.search = '';
  h.location.pathname = '/c/audit-run';
  const navigated = api.browserWorkerSnapshot();
  assert.strictEqual(navigated.managed_slot, 4);
  assert.strictEqual(navigated.managed_generation, 9);
});

test('T54 managed slots keep distinct worker identities when Chromium clones session storage', () => {
  const first = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=7',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=7'
    }
  }).api.browserWorkerSnapshot();
  const second = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=2&audapack_worker_generation=7',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=2&audapack_worker_generation=7'
    }
  }).api.browserWorkerSnapshot();

  assert.match(first.worker_id, /^audapack-managed-1-7-/);
  assert.match(second.worker_id, /^audapack-managed-2-7-/);
  assert.strictEqual(first.managed_slot, 1);
  assert.strictEqual(second.managed_slot, 2);
  assert.notStrictEqual(first.worker_id, second.worker_id);
});

test('SRC-005 worker refuses FREE claim while runtime is active', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'running', runId: 'run-active-1' };
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerSnapshot().state, 'AUDITING');
});

test('SRC-005 worker start and stop are idempotent controls', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  assert.strictEqual(api.startBrowserWorker(), true);
  assert.strictEqual(api.stopBrowserWorker(), true);
  assert.strictEqual(api.stopBrowserWorker(), true);
});

test('SRC-005 worker accepts Chrome but refuses non-root ChatGPT pages', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  delete h.navigator.brave;
  assert.strictEqual(api.browserWorkerSnapshot().browser_name, 'Chrome');
  assert.strictEqual(api.browserWorkerSnapshot().is_chromium, true);
  assert.strictEqual(api.browserWorkerCanClaim(), true);
  assert.strictEqual(api.startBrowserWorker(), true);

  h.navigator.brave = { isBrave: () => Promise.resolve(true) };
  h.location.pathname = '/c/existing-chat';
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.startBrowserWorker(), false);
});

test('SRC-005 worker refuses non-Chromium browsers', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  delete h.navigator.brave;
  h.navigator.userAgent = 'Mozilla/5.0 Firefox/128.0';
  assert.strictEqual(api.browserWorkerHasChromiumCapability(), false);
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.startBrowserWorker(), false);
});

test('W8: stop preserves active non-terminal lease for recovery', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  h.navigator.brave = { isBrave: () => Promise.resolve(true) };
  api.state.bridgeEnabled = true;
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: String(api.browserWorkerSnapshot().worker_id),
    lease_id: 'lease-1',
    project_id: 'p1',
    project_name: 'P1',
    campaign_run_id: '',
    start_receipt: ''
  };
  api.persistBrowserWorkerLease();
  assert.strictEqual(api.stopBrowserWorker(), true);
  // Active lease checkpoint must survive a plain stop.
  assert.ok(api.browserWorkerLease, 'active lease must survive stop');
  assert.strictEqual(api.browserWorkerLease.dispatch_id, 'dsp-0123456789abcdef');
});

test('W8: stop clears lease when no active dispatch', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  api.browserWorkerLease = null;
  api.persistBrowserWorkerLease();
  assert.strictEqual(api.stopBrowserWorker(), true);
  assert.strictEqual(api.browserWorkerLease, null);
});

test('T56: a spent managed worker recycles back to a clean chat and keeps its slot identity', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/c/finished-run?audapack_worker=1&audapack_worker_slot=3&audapack_worker_generation=7',
      pathname: '/c/finished-run',
      search: '?audapack_worker=1&audapack_worker_slot=3&audapack_worker_generation=7'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  // The window is parked on a finished conversation: not eligible, not claimable.
  assert.strictEqual(api.browserWorkerSnapshot().page_eligible, false);
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerNeedsRecycle(), true);
  assert.strictEqual(api.browserWorkerRecycleBlockReason(), '');

  assert.strictEqual(api.browserWorkerRecycleToCleanChat('test'), true);
  assert.strictEqual(h.location.assigned.length, 1);
  const target = h.location.assigned[0];
  assert.match(target, /^https:\/\/chatgpt\.com\/\?/);
  assert.match(target, /audapack_worker_slot=3/);
  assert.match(target, /audapack_worker_generation=7/);
});

test('T56: recycle never navigates a human tab or abandons live work', () => {
  const plain = setup({
    location: { href: 'https://chatgpt.com/c/human', pathname: '/c/human', search: '' }
  });
  plain.api.state.bridgeEnabled = true;
  assert.strictEqual(plain.api.browserWorkerRecycleBlockReason(), 'not-a-managed-worker');
  assert.strictEqual(plain.api.browserWorkerRecycleToCleanChat('test'), false);
  assert.strictEqual(plain.h.location.assigned.length, 0);

  const leased = setup({
    location: {
      href: 'https://chatgpt.com/c/run?audapack_worker_slot=2&audapack_worker_generation=1',
      pathname: '/c/run',
      search: '?audapack_worker_slot=2&audapack_worker_generation=1'
    }
  });
  leased.api.state.bridgeEnabled = true;
  leased.api.browserWorkerLease = { dispatch_id: 'dsp-live', worker_id: 'w', lease_id: 'l' };
  assert.strictEqual(leased.api.browserWorkerRecycleBlockReason(), 'lease-still-owned');
  assert.strictEqual(leased.api.browserWorkerRecycleToCleanChat('test'), false);
  assert.strictEqual(leased.h.location.assigned.length, 0);
});

test('T56: a clean managed worker is left alone and recycle is rate limited', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  assert.strictEqual(api.browserWorkerNeedsRecycle(), false);
  assert.strictEqual(api.browserWorkerRecycleToCleanChat('test'), false);
  assert.strictEqual(h.location.assigned.length, 0);

  // Now dirty it and prove the second attempt inside the cooldown is refused.
  h.location.pathname = '/c/dirty';
  assert.strictEqual(api.browserWorkerRecycleToCleanChat('test'), true);
  assert.strictEqual(api.browserWorkerRecycleToCleanChat('test'), false);
  assert.strictEqual(h.location.assigned.length, 1);
});

test('T56: a managed window still recycles after ChatGPT drops its slot query params', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=5&audapack_worker_generation=2',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=5&audapack_worker_generation=2'
    }
  });
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  // First load stamps the dedicated profile.
  assert.strictEqual(api.browserWorkerManagedIdentity().slot, 5);
  assert.strictEqual(api.browserWorkerIsManagedProfile(), true);

  // ChatGPT navigates the window to a conversation and drops the params, and a
  // fresh page load loses the per-tab session identity too.
  h.location.search = '';
  h.location.pathname = '/c/hydrated';
  h.sessionStore.clear();

  assert.strictEqual(api.browserWorkerManagedIdentity().slot, 0);
  assert.strictEqual(api.browserWorkerIsManagedProfile(), true);
  assert.strictEqual(api.browserWorkerNeedsRecycle(), true);
  assert.strictEqual(api.browserWorkerRecycleBlockReason(), '');
  assert.strictEqual(api.browserWorkerRecycleToCleanChat('params-lost'), true);
  assert.strictEqual(h.location.assigned[0], 'https://chatgpt.com/?audapack_worker=1');
});

test('T56: a profile-marked window never discards a typed draft', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=6&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=6&audapack_worker_generation=1'
    }
  });
  const { input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  api.browserWorkerIsManagedProfile();

  h.location.search = '';
  h.sessionStore.clear();
  input._text = 'a human was typing here';

  assert.strictEqual(api.browserWorkerSnapshot().has_manual_draft, true);
  assert.strictEqual(api.browserWorkerManagedIdentity().slot, 0);
  assert.strictEqual(api.browserWorkerNeedsRecycle(), false);
  assert.strictEqual(api.browserWorkerRecycleToCleanChat('draft'), false);
  assert.strictEqual(h.location.assigned.length, 0);
});

test('T57: a stuck start flag stops pinning the worker as busy forever', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  assert.strictEqual(api.browserWorkerSnapshot().audit_start_in_flight, false);
  assert.strictEqual(api.browserWorkerCanClaim(), true);

  api.setAuditStartInFlightForTest(true, Date.now());
  assert.strictEqual(api.auditStartIsLive(), true);
  assert.strictEqual(api.browserWorkerSnapshot().audit_start_in_flight, true);
  assert.strictEqual(api.browserWorkerCanClaim(), false);

  // The same flag, raised well beyond any legitimate start, must not keep the
  // lane occupied: that is how six workers read BUSY with nothing running.
  api.setAuditStartInFlightForTest(true, Date.now() - 200000);
  assert.strictEqual(api.auditStartIsLive(), false);
  assert.strictEqual(api.browserWorkerSnapshot().audit_start_in_flight, false);
  assert.strictEqual(api.browserWorkerSnapshot().worker_class, 'CLEAN');
  assert.strictEqual(api.browserWorkerCanClaim(), true);
});

test('T57: composer text keeps the ProseMirror line structure', () => {
  const { h, api } = setup();
  const input = h.el('div', { id: 'prompt-textarea', contenteditable: 'true', role: 'textbox' });
  input.isContentEditable = true;
  for (const line of ['AUDIT CORE — wave 1/3 of Quick 3 Waves.', '', 'ROLE', 'ACB_CHAIN_RECEIPT: startcore-abc']) {
    const p = h.el('p');
    p.textContent = line;
    input.appendChild(p);
  }

  // textContent glues every block together with no separator at all.
  assert.strictEqual(String(input.textContent).includes('\n'), false);

  const text = api.composerBlockText(input);
  assert.match(text, /^AUDIT CORE — wave 1\/3 of Quick 3 Waves\.\n/);
  assert.match(text, /\nACB_CHAIN_RECEIPT: startcore-abc/);
});

test('T64: two windows launched on the same managed slot never share a worker id', () => {
  const location = {
    href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=2&audapack_worker_generation=1',
    pathname: '/',
    search: '?audapack_worker=1&audapack_worker_slot=2&audapack_worker_generation=1'
  };
  const first = setup({ location: { ...location } }).api.browserWorkerSnapshot();
  const second = setup({ location: { ...location } }).api.browserWorkerSnapshot();

  // One id shared by two windows made the dispatcher flip between a window
  // running an audit and a window reporting itself clean, so every queued job
  // saw free_workers 0 while clean workers were sitting idle.
  assert.notStrictEqual(first.worker_id, second.worker_id);
  assert.match(first.worker_id, /^audapack-managed-2-1-/);
  assert.match(second.worker_id, /^audapack-managed-2-1-/);
  assert.strictEqual(first.managed_slot, 2);
  assert.strictEqual(second.managed_slot, 2);
});

test('T64: a reload of the same managed window keeps its worker id', () => {
  const location = {
    href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=4&audapack_worker_generation=2',
    pathname: '/',
    search: '?audapack_worker=1&audapack_worker_slot=4&audapack_worker_generation=2'
  };
  const { h, api } = setup({ location: { ...location } });
  const original = api.browserWorkerSnapshot().worker_id;
  assert.strictEqual(h.sessionStore.get('ai_chatbuttons_auto_tab_id_v1'), original);

  // A reload re-runs the script against the SAME sessionStorage, which is what
  // browser worker lease recovery depends on.
  const reloaded = h.load();
  assert.strictEqual(reloaded.browserWorkerSnapshot().worker_id, original);
});

test('T74: a dirty managed window that stays blocked is force-recycled by the watchdog', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { form } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true });
  api.autoRuntime.stage = 'wait-core';
  // An attachment cannot be cleared in place, so this exercises the navigation path.
  const tile = h.el('div', { role: 'group', 'aria-label': 'LEFTOVER_PROJECT.zip' });
  tile.appendChild(h.el('button', { 'aria-label': 'Remove file' }));
  form.appendChild(tile);

  // Dirty, and a soft reason keeps blocking the normal recycle.
  assert.strictEqual(api.browserWorkerNeedsRecycle(), true);
  assert.strictEqual(api.browserWorkerRecycleBlockReason(), 'audit-still-running');
  assert.strictEqual(api.browserWorkerRecycleWatchdog(), false);
  assert.strictEqual(h.location.assigned.length, 0);

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_recycle_blocked'), JSON.stringify(log));

  // Past the watchdog window the lane is reclaimed rather than pinned forever.
  api.setBrowserWorkerDirtySinceForTest(Date.now() - 200000);
  assert.strictEqual(api.browserWorkerRecycleWatchdog(), true);
  assert.strictEqual(h.location.assigned.length, 1);
  assert.match(h.location.assigned[0], /audapack_worker_slot=1/);
});

test('T74: the watchdog never overrides a live lease or a prepared START', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  input._text = 'AUDIT CORE — prepared and owned';
  api.browserWorkerLease = { dispatch_id: 'dsp-live', worker_id: 'w', lease_id: 'l' };

  assert.strictEqual(api.browserWorkerRecycleBlockReason(), 'lease-still-owned');
  api.setBrowserWorkerDirtySinceForTest(Date.now() - 900000);
  assert.strictEqual(api.browserWorkerRecycleWatchdog(), false);
  assert.strictEqual(h.location.assigned.length, 0);
});

test('T75: recycling can never become an endless navigation loop', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/c/dirty',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  // The window keeps looking dirty after every recycle: exactly the live loop
  // that fired worker_recycled every 20 s and never produced a CLEAN worker.
  let recycles = 0;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    h.sessionStore.delete('audapack_worker_recycle_v1');
    if (api.browserWorkerRecycleToCleanChat('poll-idle')) recycles += 1;
  }

  assert.strictEqual(recycles, 3, 'recycle attempts must be capped');
  assert.strictEqual(h.location.assigned.length, 3);

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_recycle_exhausted'), JSON.stringify(log));
});

test('T75: reaching a usable state restores the recycle budget', () => {
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

  h.sessionStore.set('audapack_worker_recycle_count_v1', '3');
  // A clean, claimable window: the watchdog clears the spent budget.
  assert.strictEqual(api.browserWorkerNeedsRecycle(), false);
  assert.strictEqual(api.browserWorkerRecycleWatchdog(), false);
  assert.strictEqual(h.sessionStore.get('audapack_worker_recycle_count_v1'), undefined);
});

test('T77: an abandoned machine-authored prompt is cleared in place, not navigated away', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  input._text = 'AUDIT CORE — wave 1/3 of Quick 3 Waves.\nACB_CHAIN_RECEIPT: startcore-abandoned';
  assert.strictEqual(api.browserWorkerSnapshot().worker_class, 'DIRTY');
  assert.strictEqual(api.browserWorkerCanClaim(), false);

  assert.strictEqual(api.browserWorkerRecycleWatchdog(), false);
  // Cleared in place: no navigation, so no loop is possible.
  assert.strictEqual(h.location.assigned.length, 0);
  assert.strictEqual(api.browserWorkerSnapshot().worker_class, 'CLEAN');
  assert.strictEqual(api.browserWorkerCanClaim(), true);

  const log = api.readBridgeDiagnosticLog();
  assert.ok(log.some(entry => entry.event === 'worker_draft_cleared'), JSON.stringify(log));
});

test('T77: a human draft in a managed window is never cleared', () => {
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  input._text = 'what is the capital of Estonia';
  assert.strictEqual(api.browserWorkerClearAbandonedDraft(), false);
  assert.match(String(input.textContent), /capital of Estonia/);
});
