'use strict';

// PERF-002 (audit/10.md): stable-root topology bursts must not cause O(mutations)
// global auth/root DOM scans, while auth/route correctness stays intact.
//
// The defect: every external topology mutation reached
// bindAutoRuntimeToCurrentConversation({ claim: false }) BEFORE the callback
// checked whether the mutation could matter, so on a stable '/' route a burst of
// N unrelated childList mutations re-ran the broad auth/interstitial scan N
// times (100 callbacks measured 2,100 querySelectorAll + 30,700 innerText reads).
//
// The fix: a topology mutation rebinds only when the pathname changed, when an
// auth-modal/login control was inserted/removed, or (already) on a turn
// mutation; every other topology mutation stays scoped.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, assistantTurn, addTurns } = require('./helpers');

function authDialog(h, text) {
  const dialog = h.el('div', { 'role': 'dialog', 'aria-modal': 'true', 'data-testid': 'modal' });
  dialog._text = text || 'Log in to get answers';
  return dialog;
}

function loginButtons(h) {
  const wrap = h.el('div');
  const login = h.el('button', {});
  login._text = 'Log in';
  const signup = h.el('button', {});
  signup._text = 'Sign up';
  wrap.appendChild(login);
  wrap.appendChild(signup);
  return wrap;
}

function observeAutoAudit(h, api, root) {
  return Array.from(h._observers).filter(o => o === api.autoAuditObserver && o.connected && o.root === root);
}

test('PERF-002: ordinary same-path topology burst does bounded global scans', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);

  // Prime the bound pathname with one explicit bind (the monitor startup does
  // this), then count the DOM work of 100 unrelated topology mutations.
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  h.counters.qsa = 0;
  h.counters.innerTextReads = 0;

  const observer = observeAutoAudit(h, api, main)[0];
  assert.ok(observer, 'one auto-audit observer on main');
  const records = [];
  for (let index = 0; index < 100; index += 1) {
    const node = h.el('div', { class: 'unrelated-reflow' });
    node._text = `noise ${index}`;
    main.appendChild(node);
    records.push({
      type: 'childList', target: main, addedNodes: [node], removedNodes: [], addedElements: null,
    });
  }
  // ONE callback carrying the whole burst, exactly like a real React batch.
  observer.callback(records, observer);

  const qsa = h.counters.qsa;
  const innerText = h.counters.innerTextReads;
  assert.ok(qsa <= 12, `a same-path burst must not scan O(mutations) times (qsa=${qsa})`);
  assert.ok(innerText <= 8, `a same-path burst must not read O(mutations) innerText (innerText=${innerText})`);
});

test('PERF-002: a login/auth control insertion invalidates immediately', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  h.location.pathname = '/';
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  assert.ok(!api.chatGPTRootIsQuarantined(), 'no auth surface yet');

  const observer = observeAutoAudit(h, api, main)[0];
  const buttons = loginButtons(h);
  main.appendChild(buttons);
  observer.callback([
    { type: 'childList', target: main, addedNodes: [buttons], removedNodes: [] },
  ], observer);

  // The widget must have re-bound against the new auth-visible state.
  assert.ok(api.chatGPTRootIsQuarantined(), 'auth surface must quarantine the root');
});

test('PERF-002: a same-path non-auth mutation never rebinds', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);
  api.bindAutoRuntimeToCurrentConversation({ claim: false });

  const observer = observeAutoAudit(h, api, main)[0];
  const before = api.autoBoundConversationKey;
  const node = h.el('div');
  node._text = 'irrelevant';
  main.appendChild(node);
  const records = [{ type: 'childList', target: main, addedNodes: [node], removedNodes: [] }];
  assert.strictEqual(api.mutationMayChangeAuthState(records[0]), false);
  observer.callback(records, observer);
  assert.strictEqual(api.autoBoundConversationKey, before, 'same-path non-auth mutation must not rebind');
});

test('PERF-002: an explicit pathname change invalidates immediately', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  const before = api.autoBoundConversationKey;

  h.location.pathname = '/c/route-two';
  const observer = observeAutoAudit(h, api, main)[0];
  const node = h.el('div');
  main.appendChild(node);
  observer.callback([
    { type: 'childList', target: main, addedNodes: [node], removedNodes: [] },
  ], observer);

  assert.notStrictEqual(api.autoBoundConversationKey, before, 'route change must rebind');
  assert.match(api.autoBoundConversationKey, /route-two/);
});

test('PERF-002: removing the auth modal re-derives the root immediately', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  h.location.pathname = '/';
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);
  const dialog = authDialog(h);
  main.appendChild(dialog);
  api.bindAutoRuntimeToCurrentConversation({ claim: false });
  assert.ok(api.chatGPTRootIsQuarantined(), 'modal present -> quarantined');

  const observer = observeAutoAudit(h, api, main)[0];
  dialog.remove();
  observer.callback([
    { type: 'childList', target: main, addedNodes: [], removedNodes: [dialog] },
  ], observer);

  assert.ok(!api.chatGPTRootIsQuarantined(), 'a removed auth modal must un-quarantine promptly');
});

test('PERF-002: a conversation-turn insertion still schedules the audit check', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'idle';
  api.startAutoAuditMonitor();
  h.advance(0);
  const observer = observeAutoAudit(h, api, main)[0];
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns');
  assert.ok(!h.timers.pending().includes(650), 'no 650 debounce armed before the mutation');

  const turn = assistantTurn(h, 'a1', el => { el._text = 'answer'; });
  main.appendChild(turn);
  observer.callback([
    { type: 'childList', target: main, addedNodes: [turn], removedNodes: [] },
  ], observer);

  assert.ok(
    h.timers.pending().includes(650),
    `a turn insertion must arm the 650 ms audit debounce (pending=${h.timers.pending().join(',')})`,
  );
});

test('PERF-002: root quarantine reuses a supplied auth verdict', () => {
  const { h, api } = setup();
  h.location.pathname = '/';
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);

  // Proving auth visibility once and threading it in must not re-scan the
  // interstitial surfaces: this is the duplication the bind removed.
  h.counters.qsa = 0;
  api.chatGPTRootIsQuarantined(false);
  const suppliedFalse = h.counters.qsa;
  h.counters.qsa = 0;
  api.chatGPTAuthInterstitialVisible();
  const oneProbe = h.counters.qsa;
  assert.ok(
    suppliedFalse < oneProbe,
    `a supplied verdict must skip the interstitial scan (supplied=${suppliedFalse}, scan=${oneProbe})`,
  );
  assert.ok(api.chatGPTRootIsQuarantined(true) === true, 'a supplied true verdict quarantines');
});

test('PERF-002: existing PERF widget tests keep their observer configuration', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.startAutoAuditMonitor();
  const observers = observeAutoAudit(h, api, main);
  assert.strictEqual(observers.length, 1);
  assert.strictEqual(observers[0].options.characterData, false);
  assert.strictEqual(api.autoAuditObserverConfig(), 'nav');
});
