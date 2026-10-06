'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup,
  mainEl,
  composerFixture: rawComposerFixture,
  addComposerAttachmentTile,
  installAcceptedSend,
  addTurns,
  userTurn,
  runtimeFixture
} = require('./helpers');
const { FakeFile, FakeDataTransfer, FakeEvent } = require('./harness');

// W6-004 drives the verified-Send path. The fixture must model the real
// accepted ChatGPT submission -- the payload leaves the composer, the
// attachment tiles are consumed and one user turn appears -- so the widget's
// positive verification, never the click itself, decides SENT.
const composerFixture = h => installAcceptedSend(h, rawComposerFixture(h), { consumeAttachment: true });

const NEW_ARCHIVE = '_AUDAPACK_09.09.26-T01-18-12.zip';
const TOKEN = 'w6-004-test-token';

function project(overrides = {}) {
  return { project_id: 'audapack', display_name: 'AUDAPACK', audit_name: 'AUDAPACK', enabled: true, ...overrides };
}

function installBridge(h, options = {}) {
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (request.method === 'GET' && /\/v1\/projects$/.test(url)) {
      return { status: 200, responseText: JSON.stringify({ ok: true, projects: options.projects || [project()] }) };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
}

function armRuntime(h, api, overrides = {}) {
  api.autoRuntime = runtimeFixture({ projectName: 'AUDAPACK', stage: 'idle', enabled: false, ...overrides });
  api.storage.gmSet('ai_chatbuttons_bridge_token_v1', TOKEN);
  installBridge(h);
}

function archiveFile(name, size = 1024) {
  return new FakeFile([Buffer.alloc(size)], name, { type: 'application/zip', lastModified: Date.now() });
}

function normalFile(name, size = 100) {
  return new FakeFile([Buffer.alloc(size)], name, { type: 'text/plain', lastModified: Date.now() });
}

function persistedGeneration(overrides = {}) {
  const now = Date.now();
  return {
    version: 1,
    generation: 1,
    conversationKey: 'c:abc123',
    projectId: 'audapack',
    projectName: 'AUDAPACK',
    filename: NEW_ARCHIVE,
    fileSize: 1024,
    fileLastModified: now,
    state: 'SENT',
    armedAt: now - 4000,
    readyAt: now - 3000,
    sendReadyDeadlineAt: null,
    sendPreparedAt: now - 2000,
    sendClickedAt: now - 1000,
    sentAt: now,
    previousUserTurnId: 'u-before-click',
    lastErrorCode: null,
    ...overrides
  };
}

function dispatchDrop(h, input, { files = [], uri = '', plain = '' } = {}) {
  const transfer = new FakeDataTransfer();
  for (const file of files) transfer.items.add(file);
  if (uri) transfer.setData('text/uri-list', uri);
  if (plain) transfer.setData('text/plain', plain);
  transfer.types = [
    ...(files.length ? ['Files'] : []),
    ...(uri ? ['text/uri-list'] : []),
    ...(plain ? ['text/plain'] : [])
  ];
  const event = new FakeEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer });
  event.target = input;
  h.dom.dispatchEvent(event);
  return event;
}

test('W6-004 01: canonical registered project ZIP drop -> ARMED', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  armRuntime(h, api, );
  const file = archiveFile(NEW_ARCHIVE);
  dispatchDrop(h, input, { files: [file], uri: `file:///v:/x/${NEW_ARCHIVE}` });
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.ok(gen, 'generation should exist');
  assert.strictEqual(gen.state, 'ARMED');
  assert.strictEqual(gen.projectName, 'AUDAPACK');
  assert.strictEqual(gen.filename, NEW_ARCHIVE);
  assert.strictEqual(gen.generation, 1);
});

test('W6-004 02: arbitrary ZIP -> zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const file = new FakeFile([Buffer.alloc(2048)], 'arbitrary_data.zip', { type: 'application/zip', lastModified: Date.now() });
  await api.handleSaihandoffM1DropOrSelection([file]);
  await h.settle();
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'IGNORED');
  assert.strictEqual(gen.lastErrorCode, 'arbitrary_zip');
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'arbitrary ZIP must not trigger send');
});

test('W6-004 03: ordinary file -> zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const file = normalFile('notes.txt');
  await api.handleSaihandoffM1DropOrSelection([file]);
  await h.settle();
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'IGNORED');
  assert.strictEqual(gen.lastErrorCode, 'ordinary_file');
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'ordinary file must not trigger send');
});

test('W6-004 04: project ZIP + unrelated second normal file -> fail/hold according to explicit policy, zero accidental Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const files = [archiveFile(NEW_ARCHIVE), normalFile('notes.txt')];
  const res = await api.handleSaihandoffM1DropOrSelection(files);
  await h.settle();
  assert.strictEqual(res.ok, false);
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'unexpected_additional_files');
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false);
});

test('W6-004 05: two different project archives -> ambiguous, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const files = [archiveFile(NEW_ARCHIVE), archiveFile('_SAICONT_09.09.26-T01-18-12.zip')];
  const res = await api.handleSaihandoffM1DropOrSelection(files);
  await h.settle();
  assert.strictEqual(res.ok, false);
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'ambiguous_multiple_archives');
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false);
});

test('W6-004 06: busy attachment -> WAITING_UPLOAD', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  await api.stepSaihandoffM1();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'WAITING_UPLOAD');
  assert.strictEqual(Boolean(send._clicked), false, 'busy attachment must not send');
});

test('W6-004 07: disabled Send during upload -> zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  send.disabled = true;
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'disabled send button must not be clicked');
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'WAITING_SEND_READY');
  assert.ok(Number.isFinite(gen.sendReadyDeadlineAt));
});

test('W6-004 08: exact expected tile becomes ready -> one Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  send.disabled = false;
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(clicks, 1);
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'SENT');
});

test('W6-004 09: repeated readiness observation -> one Send total', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  await api.stepSaihandoffM1();
  await api.stepSaihandoffM1();
  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 1, 'repeated observation must result in exactly one Send total');
});

test('W6-004 10: MutationObserver burst -> one Send total', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  await Promise.all([
    api.stepSaihandoffM1(),
    api.stepSaihandoffM1(),
    api.stepSaihandoffM1(),
    api.stepSaihandoffM1()
  ]);
  assert.strictEqual(clicks, 1, 'concurrent step burst must result in exactly one Send total');
});

test('W6-004 11: manual composer draft -> zero Send and byte/text-equivalent draft', async () => {
  const { h, api } = setup();
  const { input, send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const manualText = 'Unrelated operator draft text that must survive';
  input._text = manualText;
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'manual draft must prevent send');
  assert.strictEqual(api.composerPlainText(input), manualText, 'draft text must be untouched');
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'manual_draft_present');
});

test('W6-004 12: ChatGPT generating -> WAITING_CHAT_IDLE', async () => {
  const { h, api } = setup();
  const { stop, send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  stop.hidden = false;
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false);
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'WAITING_CHAT_IDLE');
});

test('W6-004 13: generation finishes -> progression resumes', async () => {
  const { h, api } = setup();
  const { stop, send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  stop.hidden = false;
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'WAITING_CHAT_IDLE');

  stop.hidden = true;
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 1);
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'SENT');
});

test('W6-004 14: conversation changes before readiness -> stale generation invalidated', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  gen.conversationKey = 'c:conv-1';
  api.setSaihandoffM1Generation(gen);

  h.location.pathname = '/c/conv-2';
  h.location.href = 'https://chatgpt.com/c/conv-2';
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false);
  const updatedGen = api.getSaihandoffM1Generation();
  assert.strictEqual(updatedGen.state, 'INVALIDATED');
  assert.strictEqual(updatedGen.lastErrorCode, 'conversation_changed');
});

test('W6-004 15: attachment replaced before readiness -> stale generation cannot Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const gen1 = api.getSaihandoffM1Generation();
  assert.strictEqual(gen1.generation, 1);

  await api.handleSaihandoffM1DropOrSelection([archiveFile('_SAICONT_09.09.26-T01-18-12.zip')]);
  await h.settle();
  const gen2 = api.getSaihandoffM1Generation();
  assert.strictEqual(gen2.generation, 2);

  const res = await api.stepSaihandoffM1(1);
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'stale_generation');
  assert.strictEqual(Boolean(send._clicked), false);
});

test('W6-004 16: same filename+same size replacement event still invalidates previous generation', async () => {
  const { h, api } = setup();
  armRuntime(h, api, );
  const file1 = archiveFile(NEW_ARCHIVE, 1024);
  await api.handleSaihandoffM1DropOrSelection([file1]);
  await h.settle();
  await h.settle();
  const gen1 = api.getSaihandoffM1Generation();
  assert.strictEqual(gen1.generation, 1);

  const file2 = archiveFile(NEW_ARCHIVE, 1024);
  await api.handleSaihandoffM1DropOrSelection([file2]);
  await h.settle();
  await h.settle();
  const gen2 = api.getSaihandoffM1Generation();
  assert.strictEqual(gen2.generation, 2);
  assert.notStrictEqual(gen1.generation, gen2.generation);
});

test('W6-004 17: SPA remount without replacement -> same pending generation resumes', async () => {
  const { h, api } = setup();
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  composerFixture(h);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const genBefore = api.getSaihandoffM1Generation();
  assert.strictEqual(genBefore.generation, 1);

  const main = mainEl(h);
  main.innerHTML = '';
  const remounted = composerFixture(h);
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  remounted.send.addEventListener('click', () => { clicks++; });

  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 1);
  const genAfter = api.getSaihandoffM1Generation();
  assert.strictEqual(genAfter.generation, 1, 'generation ID must be preserved across remount');
  assert.strictEqual(genAfter.state, 'SENT');
});

test('W6-004 18: Send temporarily absent -> bounded waiting', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  send.remove();
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'WAITING_SEND_READY');

  const form = h.dom.documentElement.querySelector('form[data-type="unified-composer"]');
  form.appendChild(send);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res2 = await api.stepSaihandoffM1();
  assert.strictEqual(res2.ok, true);
  assert.strictEqual(res2.state, 'SENT');
  assert.strictEqual(clicks, 1);
});

test('W6-004 19: successful verified click -> SENT exactly once', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(clicks, 1);
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'SENT');
  assert.ok(gen.sentAt > 0);
  assert.ok(gen.sendClickedAt > 0);
});

test('W6-004 20: rerender after SENT -> zero second Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 1);

  api.renderAutoAuditState();
  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 1, 'rerender after SENT must not send again');
});

test('W6-004 21: persisted SENT reload -> zero second Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);

  const sentGen = {
    version: 1,
    generation: 1,
    conversationKey: 'c:abc123',
    projectId: 'audapack',
    projectName: 'AUDAPACK',
    filename: NEW_ARCHIVE,
    fileSize: 1024,
    fileLastModified: Date.now(),
    state: 'SENT',
    armedAt: Date.now() - 1000,
    readyAt: Date.now() - 500,
    sendReadyDeadlineAt: null,
    sendPreparedAt: Date.now() - 100,
    sendClickedAt: Date.now() - 50,
    sentAt: Date.now(),
    previousUserTurnId: '',
    lastErrorCode: null
  };
  h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(sentGen));
  api.saihandoffM1CurrentGen = null;

  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(clicks, 0, 'persisted SENT reload must not click send again');
});

test('W6-004 22: delayed generation N callback after generation N+1 -> zero stale Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);

  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  assert.strictEqual(api.getSaihandoffM1Generation().generation, 1);

  await api.handleSaihandoffM1DropOrSelection([archiveFile('_SAICONT_09.09.26-T01-18-12.zip')]);
  await h.settle();
  assert.strictEqual(api.getSaihandoffM1Generation().generation, 2);

  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1(1);
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'stale_generation');
  assert.strictEqual(clicks, 0, 'delayed generation N callback must never send');
});

test('W6-004 23: ambiguous send acknowledgement -> no blind second click', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  let clicks = 0;
  send.addEventListener('click', () => {
    clicks++;
  });
  send._sendFails = true;

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT');
  const firstClicks = clicks;

  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, firstClicks, 'ambiguous acknowledgement must not trigger a blind second click');
});

test('W6-004 24: T-185/P0 activation gate closed=false -> production auto-send disabled', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  assert.strictEqual(api.isSaihandoffM1GateOpen(), false, 'P0 gate must be closed by default');
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'BLOCKED_BY_P0_GATE');
  assert.strictEqual(clicks, 0, 'auto-send must be blocked when P0 gate is closed');
  assert.strictEqual(api.getSaihandoffM1Generation().lastErrorCode, 'blocked_by_p0_gate');
});

test('W6-004 25: gate open=true -> normal M1 path eligible', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  assert.strictEqual(api.isSaihandoffM1GateOpen(), true, 'P0 gate must be open');
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(clicks, 1, 'normal M1 path must be eligible when gate is open');
});

test('W6-004 26: unregistered but canonically named archive -> HOLD, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h, { projects: [] });
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'unregistered_project');
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'plausibly named but unregistered ZIP must never send');
});

test('W6-004 27: ambiguous registered identity -> HOLD, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h, { projects: [project(), project({ project_id: 'audapack-2', display_name: 'audapack' })] });
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'ambiguous_registered_project');
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false, 'ambiguous registered identity must never send');
});

test('W6-004 28: registry unavailable -> HOLD, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h, { projects: null });
  h.httpResponder = () => ({ status: 503, responseText: JSON.stringify({ ok: false, error: { code: 'down' } }) });
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'registry_unavailable');
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.stepSaihandoffM1();
  assert.strictEqual(Boolean(send._clicked), false);
});

test('W6-004 29: registry result for older generation -> stale, ignored, newer gen owns state', async () => {
  const { h, api } = setup();
  armRuntime(h, api, );
  installBridge(h);
  const requests = [];
  const realResponder = h.httpResponder;
  h.httpResponder = request => {
    if (/\/v1\/projects$/.test(String(request.url || ''))) requests.push(request);
    return realResponder(request);
  };
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await api.handleSaihandoffM1DropOrSelection([archiveFile('_SAICONT_09.09.26-T01-18-12.zip')]);
  await h.settle();
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.generation, 2, 'newer generation owns the state');
  assert.strictEqual(gen.state, 'HOLD', 'newer gen is unregistered SAICONT');
  assert.strictEqual(gen.lastErrorCode, 'unregistered_project');
  assert.strictEqual(requests.length, 2, 'both generations resolved against the real registry');
});

test('W6-004 30: one physical sanitized drop -> exactly one generation', async () => {
  const { h, api } = setup();
  const { input, upload } = composerFixture(h);
  armRuntime(h, api, );
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  dispatchDrop(h, input, { files: [archiveFile(NEW_ARCHIVE)], uri: `file:///v:/x/${NEW_ARCHIVE}` });
  await h.settle();
  assert.strictEqual(api.saihandoffM1GenerationCounter, 1, 'one physical drop -> one generation');
  const synthetic = new FakeEvent('change', { bubbles: true, cancelable: false });
  synthetic.target = upload;
  h.dom.dispatchEvent(synthetic);
  await h.settle();
  assert.strictEqual(api.saihandoffM1GenerationCounter, 1, 'synthetic change from setNativeFileList must not create a second generation');

  upload.files = [archiveFile('_SAICONT_09.09.26-T01-18-12.zip')];
  const replacement = new FakeEvent('change', { bubbles: true, cancelable: false });
  replacement.target = upload;
  h.dom.dispatchEvent(replacement);
  await h.settle();
  assert.strictEqual(api.saihandoffM1GenerationCounter, 2, 'later real replacement must not reuse the one-shot internal marker');
  assert.strictEqual(api.getSaihandoffM1Generation().generation, 2);
  const tiles = h.dom.querySelectorAll('[role="group"]').filter(t => t.getAttribute('aria-label') === NEW_ARCHIVE);
  assert.strictEqual(tiles.length, 1);
});

test('W6-004 31: pre-existing same-name ready tile cannot satisfy a new generation', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h);
  const oldTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 0, 'old same-name tile must never satisfy the new generation');
  const newTile = addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.state, 'WAITING_UPLOAD');
  assert.strictEqual(Boolean(send._clicked), false, 'RED control: old ready tile not sent, new tile still uploading');
  oldTile.remove();
  newTile.remove();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  const res2 = await api.stepSaihandoffM1();
  assert.strictEqual(res2.ok, true, 'the generation binds only its own new tile');
  assert.strictEqual(clicks, 1);
});

test('W6-004 32: ready generation reacquires one exact remounted tile before click', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  installBridge(h);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res1 = await api.stepSaihandoffM1();
  assert.strictEqual(res1.ok, false);
  assert.strictEqual(res1.state, 'BLOCKED_BY_P0_GATE');
  const tiles = h.dom.querySelectorAll('[role="group"]').filter(t => t.getAttribute('aria-label') === NEW_ARCHIVE);
  for (const tile of tiles) tile.remove();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  api.setSaihandoffM1GateOpen(true);
  const res2 = await api.stepSaihandoffM1();
  assert.strictEqual(res2.ok, true);
  assert.strictEqual(res2.state, 'SENT');
  assert.strictEqual(clicks, 1);
});

test('W6-004 33: persisted SENDING reload -> zero second Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const sendingGen = {
    version: 1, generation: 1, conversationKey: 'c:abc123', projectId: 'audapack', projectName: 'AUDAPACK',
    filename: NEW_ARCHIVE, fileSize: 1024, fileLastModified: Date.now(), state: 'SENDING',
    armedAt: Date.now() - 2000, readyAt: Date.now() - 1500, sendPreparedAt: Date.now() - 1000,
    sendClickedAt: Date.now() - 900, sendReadyDeadlineAt: null, sentAt: null, previousUserTurnId: '', lastErrorCode: null
  };
  h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(sendingGen));
  api.saihandoffM1CurrentGen = null;
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT');
  assert.strictEqual(clicks, 0, 'persisted SENDING must never blind-click after reload');
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT');
});

test('W6-004 34: persisted SENDING + proven user turn -> reconciles to SENT', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  const sendingGen = {
    version: 1, generation: 1, conversationKey: 'c:abc123', projectId: 'audapack', projectName: 'AUDAPACK',
    filename: NEW_ARCHIVE, fileSize: 1024, fileLastModified: Date.now(), state: 'SENDING',
    armedAt: Date.now() - 2000, readyAt: Date.now() - 1500, sendPreparedAt: Date.now() - 1000,
    sendClickedAt: Date.now() - 900, sendReadyDeadlineAt: null, sentAt: null, previousUserTurnId: 'u-before-click', lastErrorCode: null
  };
  h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(sendingGen));
  api.saihandoffM1CurrentGen = null;
  addTurns(h, [userTurn(h, 'u-after-click', `sent ${NEW_ARCHIVE}`)]);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(clicks, 0, 'reconciliation proves acceptance without a second click');
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'SENT');
});

test('W6-004 35: corrupt persisted JSON fails closed', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  h.sessionStore.set('saihandoff_m1_send_generation_v1', '{corrupt json');
  api.saihandoffM1CurrentGen = null;
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'no_generation');
  assert.strictEqual(clicks, 0, 'corrupt persisted state must fail closed');
});

test('W6-004 36: unsupported persisted schema fails closed', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify({ version: 2, generation: 7, state: 'READY_TO_SEND' }));
  api.saihandoffM1CurrentGen = null;
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'no_generation');
  assert.strictEqual(clicks, 0, 'unsupported schema must fail closed');
});

test('W6-004 37: M1 UI uses closed visible vocabulary and data-state', () => {
  const { h, api } = setup();
  composerFixture(h);
  const el = h.dom.querySelector('#acb-saihandoff-state');
  assert.ok(el, 'M1 status element exists in the panel');
  const generation = (state, lastErrorCode = null) => ({
    version: 1,
    generation: 1,
    conversationKey: 'c:x',
    projectId: 'p',
    projectName: 'P',
    filename: 'f',
    fileSize: 1,
    fileLastModified: 0,
    state,
    armedAt: 1,
    readyAt: null,
    sendReadyDeadlineAt: null,
    sendPreparedAt: null,
    sendClickedAt: null,
    sentAt: null,
    previousUserTurnId: '',
    lastErrorCode
  });
  const states = [
    // T-261: the IDLE label names the SUBSYSTEM. A bare "SAIHANDOFF IDLE" on a
    // tab running a live Core reads as "this audit is idle", which is the one
    // thing the panel must never claim.
    [null, 'Implementation handoff: IDLE', 'idle'],
    [generation('ARMED'), 'ARCHIVE ARMED', 'armed'],
    [generation('CLASSIFYING'), 'HOLD classifying', 'hold'],
    [generation('WAITING_UPLOAD'), 'UPLOAD', 'upload'],
    [generation('WAITING_CHAT_IDLE'), 'WAIT CHAT', 'wait-chat'],
    [generation('WAITING_SEND_READY'), 'READY', 'ready'],
    [generation('READY_TO_SEND'), 'READY', 'ready'],
    [generation('SENDING'), 'SENDING', 'sending'],
    [generation('SENT'), 'SENT', 'sent'],
    [generation('BLOCKED_BY_P0_GATE', 'blocked_by_p0_gate'), 'HOLD blocked_by_p0_gate', 'hold'],
    [generation('HOLD', 'manual_draft_present'), 'HOLD manual_draft_present', 'hold'],
    [generation('FAILED', 'registry_unavailable'), 'FAILED registry_unavailable', 'failed'],
    [generation('UNRESOLVED_SEND_ACKNOWLEDGEMENT', 'send_not_accepted'), 'HOLD send_not_accepted', 'hold'],
    [generation('IGNORED', 'ordinary_file'), 'HOLD ordinary_file', 'hold']
  ];
  for (const [gen, expected, dataState] of states) {
    api.saihandoffM1CurrentGen = gen;
    api.renderAutoAuditState();
    assert.strictEqual(el.textContent, expected, `mapping for ${gen ? gen.state : 'null'}`);
    assert.strictEqual(el.dataset.state, dataState, `data-state for ${gen ? gen.state : 'null'}`);
  }
});

// ---------------------------------------------------------------------------
// T-186: runtime ownership vs persisted intent (fail-closed reload, SPA remount)
// ---------------------------------------------------------------------------

test('W6-004 38: persisted pre-send generation restored in a new runtime epoch -> fail closed, zero Send', async () => {
  // D: runtime-only ownership must never be serialized. Restored in a new
  // script/page reload, the exact attachment ownership proof no longer exists.
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  // A stored READY_TO_SEND without ownedTileRef proves nothing — fail closed.
  const stored = {
    version: 1,
    generation: 1,
    conversationKey: 'c:abc123',
    projectId: 'audapack',
    projectName: 'AUDAPACK',
    filename: NEW_ARCHIVE,
    fileSize: 1024,
    fileLastModified: Date.now(),
    state: 'READY_TO_SEND',
    armedAt: Date.now() - 1000,
    readyAt: null,
    sendReadyDeadlineAt: null,
    sendPreparedAt: null,
    sendClickedAt: null,
    sentAt: null,
    previousUserTurnId: '',
    lastErrorCode: null
  };
  h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(stored));
  api.saihandoffM1CurrentGen = null;
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  // Runtime state: no ownedTileRef -> generation is in pre-send phase.
  // After getSaihandoffM1Generation(), it becomes HOLD ownership_proof_lost.
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'ownership_proof_lost', 'pre-send without DOM ownership fails closed');
  assert.strictEqual(clicks, 0, 'persisted intent without DOM ownership must never send');
});

test('W6-004 39: runtime-owned busy tile survives SPA remount -> ownership resumes, still zero Send while busy', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const ownedTile = addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  let res = await api.stepSaihandoffM1();
  assert.strictEqual(res.state, 'WAITING_UPLOAD');
  assert.strictEqual(api.getSaihandoffM1Generation().ownedTileRef != null, true, 'ownership established on the busy tile');

  // SPA remount: composer DOM torn down and rebuilt in the SAME JS runtime.
  const main = mainEl(h);
  main.innerHTML = '';
  ownedTile.remove();
  composerFixture(h);
  addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  res = await api.stepSaihandoffM1();
  assert.strictEqual(res.state, 'WAITING_UPLOAD', 'rebound busy tile keeps WAITING_UPLOAD');
  assert.strictEqual(api.getSaihandoffM1Generation().ownedTileRef != null, true, 'ownership reestablished');
  assert.strictEqual(clicks, 0, 'busy rebound tile must not send');
});

test('W6-004 40: runtime-owned generation + SPA remount + two same-name candidates -> HOLD ambiguous ownership, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const ownedTile = addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  const res0 = await api.stepSaihandoffM1();
  assert.strictEqual(res0.state, 'WAITING_UPLOAD');
  assert.strictEqual(api.getSaihandoffM1Generation().ownedTileRef != null, true);

  const main = mainEl(h);
  main.innerHTML = '';
  ownedTile.remove();
  composerFixture(h);
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'ambiguous_owned_tile');
  assert.strictEqual(Boolean(send._clicked), false, 'ambiguous ownership never sends');
});

test('W6-004 41: old same-project generation + new same-project generation -> identity is unambiguous, only the new owned tile may Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h);
  const oldTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 0, 'old ready tile cannot satisfy the new generation');

  const newTile = addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  const res2 = await api.stepSaihandoffM1();
  assert.strictEqual(res2.state, 'WAITING_UPLOAD');
  oldTile.remove();
  newTile.remove();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  const res3 = await api.stepSaihandoffM1();
  assert.strictEqual(res3.ok, true);
  assert.strictEqual(clicks, 1, 'only the generation-owned tile authorizes Send');
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'SENT');
});

test('W6-004 42: Project A + Project B attachments -> ambiguous, zero Send', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  addComposerAttachmentTile(h, '_SAICONT_09.09.26-T01-18-12.zip');
  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'HOLD');
  assert.strictEqual(res.reason, 'ambiguous_multiple_archives');
  assert.strictEqual(Boolean(send._clicked), false);
});

test('W6-004 43: mixed project ZIP + PDF drop -> every File injected once, M1 HOLD unexpected_additional_files, zero Send', async () => {
  const { h, api } = setup();
  const { input, upload, send } = composerFixture(h);
  armRuntime(h, api, );
  api.setSaihandoffM1GateOpen(true);
  installBridge(h);
  let injected = [];
  upload._onFilesSet = (element, files) => {
    injected = files.map(file => file.name);
  };
  const zip = archiveFile(NEW_ARCHIVE);
  const pdf = new FakeFile([Buffer.from('%PDF-1.7')], 'report.pdf', { type: 'application/pdf' });
  dispatchDrop(h, input, {
    files: [zip, pdf],
    uri: `file:///V:/x/${NEW_ARCHIVE}`
  });
  await h.settle();
  assert.deepStrictEqual(injected, [NEW_ARCHIVE, 'report.pdf'], 'every original File injected once in order');
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'HOLD');
  assert.strictEqual(gen.lastErrorCode, 'unexpected_additional_files');
  assert.strictEqual(Boolean(send._clicked), false, 'mixed drop never auto-sends');
});

test('W6-004 44: canonical project non-ZIP archive names are never PROJECT_ARCHIVE', () => {
  const { api } = setup();
  for (const suffix of ['tar.gz', '7z', 'rar', 'tgz', 'tar']) {
    const file = new FakeFile([Buffer.alloc(32)], `AUDAPACK_09.09.26-T01-18-12.${suffix}`, {
      type: 'application/octet-stream',
      lastModified: Date.now()
    });
    assert.notStrictEqual(api.classifyComposerDropFile(file), 'PROJECT_ARCHIVE', suffix);
  }
});

test('W6-004 45: malformed or unknown persisted generations fail closed', async () => {
  const mutations = [
    ['unknown state', gen => { gen.state = 'UNKNOWN'; }],
    ['empty conversationKey', gen => { gen.conversationKey = ''; }],
    ['empty projectId', gen => { gen.projectId = ''; }],
    ['empty projectName', gen => { gen.projectName = ''; }],
    ['empty filename', gen => { gen.filename = ''; }],
    ['negative fileSize', gen => { gen.fileSize = -1; }],
    ['negative fileLastModified', gen => { gen.fileLastModified = -1; }],
    ['non-numeric fileLastModified', gen => { gen.fileLastModified = '1'; }],
    ['missing sentAt', gen => { delete gen.sentAt; }],
    ['missing sendReadyDeadlineAt', gen => { delete gen.sendReadyDeadlineAt; }],
    ['non-finite timestamp', gen => { gen.sentAt = '1'; }]
  ];
  for (const [label, mutate] of mutations) {
    const { h, api } = setup();
    const { send } = composerFixture(h);
    armRuntime(h, api);
    api.setSaihandoffM1GateOpen(true);
    const stored = persistedGeneration();
    mutate(stored);
    h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(stored));
    api.saihandoffM1CurrentGen = null;
    addComposerAttachmentTile(h, NEW_ARCHIVE);
    let clicks = 0;
    send.addEventListener('click', () => { clicks++; });
    const res = await api.stepSaihandoffM1();
    assert.strictEqual(res.ok, false, label);
    assert.strictEqual(res.reason, 'no_generation', label);
    assert.strictEqual(clicks, 0, label);
  }
});

test('W6-004 46: sessionStorage write failure fails closed before click', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  h.sandbox.sessionStorage.setItem = () => { throw new Error('quota exceeded'); };

  const handled = await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  assert.strictEqual(handled.ok, false);
  assert.strictEqual(handled.reason, 'saihandoff_persistence_failed');
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(clicks, 0, 'unpersisted intent must never click Send');
  const gen = api.getSaihandoffM1Generation();
  assert.strictEqual(gen.state, 'FAILED');
  assert.strictEqual(gen.lastErrorCode, 'saihandoff_persistence_failed');
});

test('W6-004 47: delayed acceptance after generation replacement cannot mutate the stale generation', async () => {
  const { h, api } = setup();
  const { send } = rawComposerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);

  const oldGen = api.getSaihandoffM1Generation();
  let replacement = null;
  let clicks = 0;
  send.addEventListener('click', () => {
    clicks++;
    h.timers.setTimeout(() => {
      replacement = {
        ...oldGen,
        generation: 2,
        filename: '_SAICONT_09.09.26-T01-18-12.zip',
        state: 'ARMED',
        armedAt: Date.now(),
        readyAt: null,
        sendPreparedAt: null,
        sendClickedAt: null,
        sentAt: null,
        previousUserTurnId: '',
        lastErrorCode: null
      };
      api.setSaihandoffM1Generation(replacement);
    }, 0);
    h.timers.setTimeout(() => {
      addTurns(h, [userTurn(h, 'accepted-late', NEW_ARCHIVE)]);
    }, 100);
  });

  const pending = api.stepSaihandoffM1();
  await h.settle();
  const res = await pending;
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, 'stale_generation');
  assert.strictEqual(clicks, 1);
  assert.strictEqual(api.getSaihandoffM1Generation(), replacement);
  assert.strictEqual(replacement.state, 'ARMED');
});

test('W6-004 48: M1 performs one click attempt without _sendFails or fallback submission', async () => {
  const { h, api } = setup();
  const { form, input, send } = rawComposerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const ownedTile = addComposerAttachmentTile(h, NEW_ARCHIVE);

  const inner = h.el('span');
  send.appendChild(inner);
  let sendClicks = 0;
  let innerClicks = 0;
  let requestSubmits = 0;
  let enterFallbacks = 0;
  send.addEventListener('click', () => {
    sendClicks++;
    ownedTile.remove();
    addTurns(h, [userTurn(h, 'accepted-once', NEW_ARCHIVE)]);
  });
  inner.addEventListener('click', () => { innerClicks++; });
  form.requestSubmit = () => { requestSubmits++; };
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') enterFallbacks++;
  });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.state, 'SENT');
  assert.strictEqual(sendClicks, 1);
  assert.strictEqual(innerClicks, 0);
  assert.strictEqual(requestSubmits, 0);
  assert.strictEqual(enterFallbacks, 0);
});

test('W6-004 49: unmarked persisted gate state stays closed', () => {
  const { h, api } = setup();
  const persisted = JSON.parse(JSON.stringify(api.state));
  delete persisted.saihandoffM1GateExplicitV1;
  persisted.saihandoffM1GateMigratedV1 = true;
  persisted.saihandoffM1P0GateOpen = true;
  persisted.saihandoffM1GateClosed = false;
  h.gmStore.set('ai_chatbuttons_v6', JSON.stringify(persisted));

  const loaded = api.loadState();
  assert.strictEqual(loaded.saihandoffM1P0GateOpen, false);
  assert.strictEqual(loaded.saihandoffM1GateClosed, true);
});

test('W6-004 50: explicit gate opening persists through saveState', () => {
  const { h, api } = setup();
  assert.strictEqual(api.setSaihandoffM1GateOpen(true), true);
  const persisted = JSON.parse(h.gmStore.get('ai_chatbuttons_v6'));
  assert.strictEqual(persisted.saihandoffM1P0GateOpen, true);
  assert.strictEqual(persisted.saihandoffM1GateExplicitV1, true);
  assert.strictEqual(api.loadState().saihandoffM1P0GateOpen, true);
});

test('W6-004 51: failed gate persistence rolls the gate closed', () => {
  const { h, api } = setup();
  h.sandbox.GM_setValue = () => { throw new Error('storage unavailable'); };
  assert.strictEqual(api.setSaihandoffM1GateOpen(true), false);
  assert.strictEqual(api.isSaihandoffM1GateOpen(), false);
  assert.strictEqual(api.state.saihandoffM1GateClosed, true);
});

test('W6-004 52: missing Send waits with a persisted deadline, then fails send_not_ready', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  send.remove();

  const waiting = await api.stepSaihandoffM1();
  assert.strictEqual(waiting.state, 'WAITING_SEND_READY');
  const deadline = api.getSaihandoffM1Generation().sendReadyDeadlineAt;
  assert.ok(Number.isFinite(deadline) && deadline > 0);
  const persisted = JSON.parse(h.sessionStore.get('saihandoff_m1_send_generation_v1'));
  assert.strictEqual(persisted.sendReadyDeadlineAt, deadline);

  h.advance(deadline + 1);
  const failed = await api.stepSaihandoffM1();
  assert.strictEqual(failed.state, 'FAILED');
  assert.strictEqual(failed.reason, 'send_not_ready');
  assert.strictEqual(api.getSaihandoffM1Generation().lastErrorCode, 'send_not_ready');
});

test('W6-004 53: prompt/manual internal file injections never become operator M1 selections', async () => {
  for (const source of ['prompt', 'manual-archive']) {
    const { h, api } = setup();
    const { upload } = composerFixture(h);
    armRuntime(h, api);
    const file = source === 'prompt'
      ? new FakeFile([Buffer.from('prompt')], 'AUDAPACK_PROMPT.md', { type: 'text/markdown', lastModified: Date.now() })
      : archiveFile(NEW_ARCHIVE);
    assert.strictEqual(api.setNativeFileList(upload, [file], { source }), true);
    await h.settle();
    assert.strictEqual(api.saihandoffM1GenerationCounter, 0, `${source} injection must stay internal`);
  }
});

test('W6-004 54: disabled autoRuntime still progresses M1 through a bounded tick', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api, { enabled: false });
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  const tile = addComposerAttachmentTile(h, NEW_ARCHIVE, { busy: true });
  h.mutate(mainEl(h), [{ type: 'childList', target: tile.parentNode, addedNodes: [tile], removedNodes: [] }]);
  await h.settle();
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'WAITING_UPLOAD');

  tile.querySelector('.animate-spin').remove();
  h.mutate(mainEl(h), [{ type: 'childList', target: tile, addedNodes: [], removedNodes: [] }]);
  let clicks = 0;
  send.addEventListener('click', () => { clicks++; });
  h.advance(250);
  await h.settle();
  assert.strictEqual(clicks, 1);
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'SENT');
});

test('W6-004 55: click fence revalidates exact composer ownership after focus', async () => {
  const { h, api } = setup();
  const { send } = rawComposerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  let clicks = 0;
  send._sendFails = true;
  send.addEventListener('click', () => { clicks++; });
  send.focus = () => { addComposerAttachmentTile(h, 'notes.txt'); };

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(clicks, 0, 'extra attachment discovered during focus must block the click');
  assert.strictEqual(res.state, 'HOLD');
  assert.strictEqual(res.reason, 'unexpected_additional_files');
});

test('W6-004 56: one foreign archive or unrelated attachment blocks M1', async () => {
  for (const name of ['_SAICONT_09.09.26-T01-18-12.zip', 'notes.txt']) {
    const { h, api } = setup();
    const { send } = composerFixture(h);
    armRuntime(h, api);
    api.setSaihandoffM1GateOpen(true);
    await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
    await h.settle();
    addComposerAttachmentTile(h, name);
    let clicks = 0;
    send.addEventListener('click', () => { clicks++; });

    const res = await api.stepSaihandoffM1();
    assert.strictEqual(res.state, 'HOLD', name);
    assert.strictEqual(res.reason, 'unexpected_additional_files', name);
    assert.strictEqual(clicks, 0, name);
  }
});

test('W6-004 57: unresolved acknowledgement reconciles only with exact archive evidence', async () => {
  const cases = [
    ['proven', 'c:abc123', 'u-before-click', `sent ${NEW_ARCHIVE}`, 'SENT'],
    ['missing archive evidence', 'c:abc123', 'u-before-click', 'sent the archive', 'UNRESOLVED_SEND_ACKNOWLEDGEMENT'],
    ['wrong conversation', 'c:other', 'u-before-click', `sent ${NEW_ARCHIVE}`, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT'],
    ['empty prior identity', 'c:abc123', '', `sent ${NEW_ARCHIVE}`, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT']
  ];
  for (const [label, conversationKey, previousUserTurnId, text, expected] of cases) {
    const { h, api } = setup();
    const { send } = composerFixture(h);
    armRuntime(h, api);
    api.setSaihandoffM1GateOpen(true);
    const stored = persistedGeneration({
      conversationKey,
      previousUserTurnId,
      state: 'UNRESOLVED_SEND_ACKNOWLEDGEMENT',
      sentAt: null,
      lastErrorCode: 'send_not_accepted'
    });
    h.sessionStore.set('saihandoff_m1_send_generation_v1', JSON.stringify(stored));
    api.saihandoffM1CurrentGen = null;
    h.location.pathname = '/c/abc123';
    h.location.href = `https://chatgpt.com${h.location.pathname}`;
    addTurns(h, [userTurn(h, 'u-after-click', text)]);
    let clicks = 0;
    send.addEventListener('click', () => { clicks++; });

    const res = await api.stepSaihandoffM1();
    assert.strictEqual(res.state, expected, label);
    assert.strictEqual(clicks, 0, label);
  }
});

test('W6-004 58: M1 classification rejects broad AUDAPACK names without canonical or tracked authority', async () => {
  const { h, api } = setup();
  armRuntime(h, api);
  assert.strictEqual(api.classifyComposerDropFile(archiveFile(NEW_ARCHIVE)), 'PROJECT_ARCHIVE');
  assert.strictEqual(api.classifyComposerDropFile(archiveFile('AUDAPACK.zip')), 'OTHER_ZIP');
  assert.strictEqual(api.classifyComposerDropFile(archiveFile('_AUDAPACK_PROJECT.zip')), 'OTHER_ZIP');

  api.autoRuntime.archiveName = '_AUDAPACK_PROJECT.zip';
  assert.strictEqual(api.classifyComposerDropFile(archiveFile('_AUDAPACK_PROJECT.zip')), 'PROJECT_ARCHIVE');
  installBridge(h);
  const result = await api.handleSaihandoffM1DropOrSelection([archiveFile('_AUDAPACK_PROJECT.zip')]);
  await h.settle();
  assert.strictEqual(result.ok, true);
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'ARMED');
});

test('W6-004 59: failed click plus unrelated user turn never becomes SENT', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  armRuntime(h, api);
  api.setSaihandoffM1GateOpen(true);
  await api.handleSaihandoffM1DropOrSelection([archiveFile(NEW_ARCHIVE)]);
  await h.settle();
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  send._sendFails = true;
  send.addEventListener('click', () => {
    addTurns(h, [userTurn(h, 'unrelated-user-turn', 'unrelated operator message')]);
  });

  const res = await api.stepSaihandoffM1();
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.state, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT');
  assert.strictEqual(res.reason, 'send_not_accepted');
  assert.strictEqual(api.getSaihandoffM1Generation().state, 'UNRESOLVED_SEND_ACKNOWLEDGEMENT');
});

