'use strict';

// T-189 rev 2: NEW and START are deterministic on ChatGPT.
//
// Surface identity is separate from Work quota/capability state: only strong
// structural evidence (a selected Work mode control or a Work composer
// placeholder) makes a surface Work, while a Work quota banner is diagnostic
// evidence that must never lock an ordinary Chat composer out of START/NEW.
// NEW is one bounded verification transaction with a single dispatch and
// positive Chat proof before success; START paints named phases and its
// attachment wait is progress-based instead of waiting a fixed 40s.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture, addComposerAttachmentTile } = require('./helpers');

function managedWorkSetup(pathname = '/') {
  const { h, api } = setup({
    location: {
      href: `https://chatgpt.com${pathname}?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1`,
      pathname,
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  return { h, api };
}

function workComposerPlaceholder(h) {
  const input = h.dom.querySelector('#prompt-textarea');
  input.setAttribute('placeholder', 'Work on anything');
  return input;
}

function chatPlaceholder(h) {
  const input = h.dom.querySelector('#prompt-textarea');
  input.setAttribute('placeholder', 'Ask anything');
  return input;
}

function addWorkSwitch(h, input, active = false) {
  const btn = h.el('button', { 'aria-label': 'Chat' });
  if (active) btn.setAttribute('aria-selected', 'true');
  btn.addEventListener('click', () => input.setAttribute('placeholder', 'Ask anything'));
  h.dom.querySelector('form[data-type="unified-composer"]').appendChild(btn);
  return btn;
}

function addWorkActiveControl(h, activeName = 'Work') {
  const work = h.el('button', { 'aria-label': activeName });
  work.setAttribute('aria-selected', 'true');
  h.dom.querySelector('form[data-type="unified-composer"]').appendChild(work);
  return work;
}

function addQuotaBanner(h, text = "You're out of Work usage for now") {
  const banner = h.el('div');
  banner._text = text;
  banner.textContent = text;
  h.dom.querySelector('body').appendChild(banner);
  return banner;
}

function addNewChatControl(h, onNewChat) {
  const fresh = h.el('button', { 'aria-label': 'New chat', 'data-testid': 'create-new-chat-button' });
  fresh.addEventListener('click', () => { if (onNewChat) onNewChat(); });
  h.dom.querySelector('body').appendChild(fresh);
  return fresh;
}

function statusText(h) {
  const status = h.dom.querySelector('#acb-status-text');
  return String(status ? status.textContent : '');
}

// ---------------------------------------------------------------------------
// Surface identity vs Work quota state (T-189 rev 2 contract)
// ---------------------------------------------------------------------------

test('T189-01: root "/" with Work placeholder => Work mode', () => {
  const { h, api } = managedWorkSetup('/');
  workComposerPlaceholder(h);
  assert.strictEqual(api.chatGPTSurfaceMode(), 'work');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), true);
});

test('T189-02: /c/<id> with Work control active => Work', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  addWorkActiveControl(h, 'Work');
  assert.strictEqual(api.chatGPTSurfaceMode(), 'work');
});

test('T189-03: /c/<id> ordinary Chat composer + Work quota banner remains Chat', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  addQuotaBanner(h, "You're out of Work usage for now");
  const surface = api.chatGPTSurfaceSnapshot();
  assert.strictEqual(surface.mode, 'chat', 'quota banner must not turn proven Chat into Work');
  assert.strictEqual(surface.workQuotaExhausted, true, 'quota banner is reported as capability state');
  assert.strictEqual(surface.composerAvailable, true);
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), false);
});

test('T189-03b: Work composer + quota banner remains Work', () => {
  const { h, api } = managedWorkSetup('/');
  workComposerPlaceholder(h);
  addQuotaBanner(h);
  const surface = api.chatGPTSurfaceSnapshot();
  assert.strictEqual(surface.mode, 'work');
  assert.strictEqual(surface.workQuotaExhausted, true);
});

test('T189-03c: quota banner survives a Work->Chat switch without failing the switch', () => {
  const { h, api } = managedWorkSetup('/');
  const input = workComposerPlaceholder(h);
  addWorkSwitch(h, input);
  addQuotaBanner(h);
  assert.strictEqual(api.chatGPTSurfaceMode(), 'work');
  const outcome = api.chatGPTEnsureChatMode();
  assert.strictEqual(outcome.ok, true);
  assert.strictEqual(outcome.action, 'switched');
  const surface = api.chatGPTSurfaceSnapshot();
  assert.strictEqual(surface.mode, 'chat', 'switch proves ordinary Chat');
  assert.strictEqual(surface.workQuotaExhausted, true, 'stale banner survives the switch');
});

test('T189-04: ordinary chat message containing work => not Work', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  const main = h.dom.querySelector('main');
  const turn = h.el('article', { 'data-message-author-role': 'assistant' });
  turn._text = 'this is ordinary work discussion about work';
  turn.textContent = 'this is ordinary work discussion about work';
  main.appendChild(turn);
  assert.notStrictEqual(api.chatGPTSurfaceMode(), 'work');
});

test('T189-05: ordinary /c/<id> Chat => chat', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  assert.strictEqual(api.chatGPTSurfaceMode(), 'chat');
  assert.strictEqual(api.chatGPTWorkSurfaceActive(), false);
});

// ---------------------------------------------------------------------------
// NEW: direct end-to-end coverage of openNewChatFromWidget
// ---------------------------------------------------------------------------

test('T189-10: NEW from root Work ends in positively proven Chat', () => {
  const { h, api } = managedWorkSetup('/');
  workComposerPlaceholder(h);
  const fresh = addNewChatControl(h, () => chatPlaceholder(h));
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.strictEqual(fresh._clickCount, 1, 'exactly one New Chat dispatch');
  assert.strictEqual(api.readNewChatRequiresChatIntent(), null, 'intent consumed only after Chat proof');
  assert.strictEqual(api.chatGPTSurfaceMode(), 'chat');
  assert.match(statusText(h), /normal Chat/);
  assert.strictEqual(h.location.assigned.length, 0, 'no navigation fallback needed');
  assert.strictEqual(h.location.reloaded, 0);
  assert.strictEqual(api.newChatDiagnostics().dispatchCount, 1);
});

test('T189-11: NEW from conversation Work ends in positively proven Chat', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  workComposerPlaceholder(h);
  const fresh = addNewChatControl(h, () => chatPlaceholder(h));
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.strictEqual(fresh._clickCount, 1);
  assert.strictEqual(api.readNewChatRequiresChatIntent(), null);
  assert.strictEqual(api.chatGPTSurfaceMode(), 'chat');
});

test('T189-12: delayed Chat switch hydration succeeds within the bounded verifier', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  workComposerPlaceholder(h);
  // No exact control yet: NEW falls back to one bounded navigation.
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.strictEqual(h.location.assigned.length, 1, 'one navigation fallback');
  assert.notStrictEqual(api.readNewChatRequiresChatIntent(), null, 'no success before Chat proof');

  // The Chat/Work switch only appears after a short hydration delay.
  const input = workComposerPlaceholder(h);
  addWorkSwitch(h, input);
  for (let i = 0; i < 40 && api.readNewChatRequiresChatIntent(); i += 1) {
    h.advance(400);
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.strictEqual(api.readNewChatRequiresChatIntent(), null, 'bounded retry proved Chat');
  assert.strictEqual(api.chatGPTSurfaceMode(), 'chat');
});

test('T189-13: unresolvable Work ends in a named NEW HOLD, never success', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  workComposerPlaceholder(h);
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.notStrictEqual(api.readNewChatRequiresChatIntent(), null);
  assert.match(statusText(h), /NEW requested/);

  for (let i = 0; i < 60 && api.readNewChatRequiresChatIntent(); i += 1) {
    h.advance(400);
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.notStrictEqual(api.readNewChatRequiresChatIntent(), null, 'unproven intent is never cleared');
  assert.match(statusText(h), /NEW HOLD \u00b7 ChatGPT is in Work mode/);
  assert.strictEqual(h.location.assigned.length, 1, 'no navigation loop');
  assert.strictEqual(h.location.reloaded, 0);
});

test('T189-14: NEW never reports success before Chat proof (unknown surface)', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  // Work placeholder with no Chat switch reachable: mode stays work/unknown.
  workComposerPlaceholder(h);
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.notStrictEqual(api.readNewChatRequiresChatIntent(), null, 'dispatch alone is not success');
  assert.doesNotMatch(statusText(h), /normal Chat/);
});

test('T189-15: generic a[href="/"] is never the preferred New Chat control', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  const home = h.el('a', { href: '/' });
  home.addEventListener('click', () => {});
  h.dom.querySelector('body').appendChild(home);
  const fresh = addNewChatControl(h, () => chatPlaceholder(h));
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.strictEqual(fresh._clickCount, 1, 'exact accessible-name/testid control wins');
  assert.strictEqual(home._clicked, undefined, 'home link is not clicked when an exact control exists');
  assert.strictEqual(h.location.assigned.length, 0);
});

test('T189-15b: home/root link alone is only a bounded navigation fallback', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  chatPlaceholder(h);
  const home = h.el('a', { href: '/' });
  h.dom.querySelector('body').appendChild(home);
  const ok = api.openNewChatFromWidget();
  assert.strictEqual(ok, true);
  assert.strictEqual(home._clicked, undefined, 'home link is never clicked');
  assert.strictEqual(h.location.assigned.length, 1, 'navigation fallback ran once');
});

test('T189-16: repeated NEW callbacks do not create duplicate navigation', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  // The surface stays Work and no Chat switch exists yet, so the first NEW
  // transaction stays pending across its bounded verifier window.
  workComposerPlaceholder(h);
  const fresh = addNewChatControl(h, () => {});
  const first = api.openNewChatFromWidget();
  assert.strictEqual(first, true);
  const second = api.openNewChatFromWidget();
  assert.strictEqual(second, true, 'repeat press is absorbed by the running transaction');
  assert.strictEqual(fresh._clickCount, 1, 'one click total');
  assert.strictEqual(api.newChatDiagnostics().dispatchCount, 1, 'one dispatch total');
  assert.strictEqual(h.location.assigned.length, 0);
  assert.strictEqual(h.location.reloaded, 0);
  assert.notStrictEqual(api.readNewChatRequiresChatIntent(), null);
  await h.settle();
});

// ---------------------------------------------------------------------------
// START liveness
// ---------------------------------------------------------------------------

test('T189-06: START on Work root => zero composer mutation and zero Send', async () => {
  const { h, api } = managedWorkSetup('/');
  const { input, send } = composerFixture(h);
  workComposerPlaceholder(h);
  api.state.auditProfile = 'quick3';
  api.state.superCompact = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, stage: 'complete', conversationKey: 'c:abc123' });
  addComposerAttachmentTile(h, '_AUDAPACK_01.zip');
  const before = String(input.textContent || '');
  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  const result = await pending;
  assert.strictEqual(result, false);
  assert.strictEqual(send._clicked, undefined);
  assert.strictEqual(String(input.textContent || ''), before);
  assert.ok(api.startPhaseHistory().includes('START HOLD \u00b7 Work'), JSON.stringify(api.startPhaseHistory()));
});

test('T189-07: START on Work /c/<id> => zero composer mutation', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  const { input, send } = composerFixture(h);
  addWorkActiveControl(h);
  api.state.auditProfile = 'quick3';
  api.state.superCompact = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, stage: 'complete', conversationKey: 'c:abc123' });
  addComposerAttachmentTile(h, '_AUDAPACK_01.zip');
  const before = String(input.textContent || '');
  const pending = api.startAuditCoreFromReadyAttachment();
  await h.settle();
  const result = await pending;
  assert.strictEqual(result, false);
  assert.strictEqual(send._clicked, undefined);
  assert.strictEqual(String(input.textContent || ''), before);
});

test('T189-17: START in normal Chat with stale Work quota banner is allowed', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  const { send } = composerFixture(h);
  chatPlaceholder(h);
  addQuotaBanner(h);
  send.setAttribute('aria-disabled', 'true');
  api.state.auditProfile = 'quick3';
  api.state.superCompact = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, stage: 'complete', conversationKey: 'c:abc123' });
  addComposerAttachmentTile(h, '_AUDAPACK_01.zip');
  const pending = api.startAuditCoreFromReadyAttachment();
  // T-260: the send chain is click -> acceptance -> form submit -> acceptance
  // -> Enter, and each acceptance wait is a real bounded 2.5 s of clock. The
  // harness now emulates form.requestSubmit() the way the DOM does, so this
  // path is reachable in tests and a 6 s window no longer covers one attempt.
  // The wait is still bounded: this is a ceiling, not a wait-for-success.
  for (let i = 0; i < 200; i += 1) {
    h.advance(200);
    await new Promise(resolve => setTimeout(resolve, 2));
    if (api.currentStartPhase.includes('HOLD')) break;
  }
  await pending.catch(() => {});
  const history = api.startPhaseHistory();
  assert.ok(history.includes('START WAIT ZIP'), JSON.stringify(history));
  assert.strictEqual(
    history.includes('START HOLD \u00b7 Work'), false,
    'stale quota banner must not block START in normal Chat'
  );
  assert.strictEqual(send._clicked, undefined);
});

test('T189-17b: START stable impossible attachment state fails fast', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  const { send } = composerFixture(h);
  chatPlaceholder(h);
  api.state.auditProfile = 'quick3';
  api.state.superCompact = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, stage: 'complete', conversationKey: 'c:abc123' });
  // A tile that exists but can never become ready: hidden, stable, not busy.
  const tile = addComposerAttachmentTile(h, '_AUDAPACK_01.zip');
  tile.setAttribute('hidden', '');
  const pending = api.startAuditCoreFromReadyAttachment();
  let settled = false;
  pending.then(() => { settled = true; });
  let advanced = 0;
  while (!settled && advanced < 40000) {
    h.advance(250);
    advanced += 250;
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  const result = await pending;
  assert.strictEqual(result, false);
  assert.ok(settled, 'the wait resolved before the 40s ceiling');
  assert.ok(advanced < 4000, `stable impossible state fails fast (advanced ${advanced}ms)`);
  assert.ok(api.startPhaseHistory().includes('START HOLD \u00b7 attachment not ready'), JSON.stringify(api.startPhaseHistory()));
  assert.strictEqual(send._clicked, undefined);
});

test('T189-17c: START genuine busy attachment keeps waiting and phases are observable', async () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  const { send } = composerFixture(h);
  chatPlaceholder(h);
  api.state.auditProfile = 'quick3';
  api.state.superCompact = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, stage: 'complete', conversationKey: 'c:abc123' });
  const tile = addComposerAttachmentTile(h, '_AUDAPACK_01.zip', { busy: true });
  const pending = api.startAuditCoreFromReadyAttachment();
  let settled = false;
  pending.then(() => { settled = true; });
  // A busy tile is positive progress evidence: the wait must stay alive well
  // past the stable-state grace window.
  for (let i = 0; i < 12 && !settled; i += 1) {
    h.advance(250);
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.strictEqual(settled, false, 'busy attachment keeps the START wait alive');
  assert.ok(api.startPhaseHistory().includes('START WAIT ZIP'), JSON.stringify(api.startPhaseHistory()));

  // The upload finishes: the spinner disappears and the tile becomes ready.
  const spinner = tile.querySelector('[class*="animate-spin"]');
  spinner.setAttribute('hidden', '');
  for (let i = 0; i < 40 && !settled; i += 1) {
    h.advance(250);
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  await pending;
  const history = api.startPhaseHistory();
  assert.ok(history.includes('START LEASE'), `START progressed past the attachment wait: ${JSON.stringify(history)}`);
  assert.ok(history.includes('START PRECHECK'), 'PRECHECK phase was painted');
  assert.strictEqual(send._clicked, undefined, 'no send without a lease');
});

test('T189-08: managed worker on Work conversation is not claimable', () => {
  const { h, api } = managedWorkSetup('/c/abc123');
  addWorkActiveControl(h);
  assert.strictEqual(api.browserWorkerCanClaim(), false);
  assert.strictEqual(api.browserWorkerClaimBlockReason(), 'worker-in-work-mode');
});

test('T189-09: widget own CHAT button never mistaken for mode control', () => {
  const { h, api } = managedWorkSetup('/');
  workComposerPlaceholder(h);
  const ours = h.el('button', { 'aria-label': 'Chat', id: 'acb-bar-chat' });
  h.dom.querySelector('body').appendChild(ours);
  assert.strictEqual(api.chatGPTWorkModeSwitchControl?.() ?? null, null);
});

test('T189-18: stable ready ZIP + Send disabled with no upload evidence => bounded HOLD not 5min', async () => {
  const { h, api } = setup();
  const { form, send } = composerFixture(h);
  const tile = addComposerAttachmentTile(h, '_AUDAPACK_01.zip');
  send.setAttribute('aria-disabled', 'true');
  const startedAt = Date.now();
  const pending = api.waitForChatGPTSendReady(40, 4000);
  let settled = false;
  pending.then(() => { settled = true; });
  const end = Date.now() + 600;
  while (Date.now() < end) {
    h.advance(30);
    await new Promise(r => setTimeout(r, 5));
  }
  assert.strictEqual(settled, true, 'must fail fast, not wait to hard deadline');
  const elapsed = Date.now() - startedAt;
  assert.ok(elapsed < 1500, `elapsed ${elapsed} should be bounded <1500ms`);
  assert.strictEqual(await pending, null);
});
