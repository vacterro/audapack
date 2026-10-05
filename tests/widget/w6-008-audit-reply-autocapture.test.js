'use strict';

// A reply to a user turn that carried a project archive is that project's
// audit. It goes to the INAUDIT Inbox without an IA click, and the archive
// name travels with it so the Bridge can pin the capture to the project.
// Same watch rule as SAIHANDOFF auto-capture: only a reply this runtime saw
// stream counts, once, and never an old conversation being opened.

const test = require('node:test');
const assert = require('node:assert/strict');
const { setup, userTurn, assistantTurn, addTurns } = require('./helpers');

const AUDIT = `# _ZAICODE audit\n\n${'Finding: the walker treats a vanished directory as unreadable.\n'.repeat(40)}`;

function archiveUserTurn(h, id, filename = '_ZAICODE.zip') {
  const turn = userTurn(h, id);
  turn.appendChild(h.el('div', { class: 'file-tile' }, `${filename} Zip Archive`));
  turn.appendChild(h.el('div', { 'data-message-content-part-type': 'text' }, 'Audit this project.'));
  return turn;
}

function replyTurn(h, id, body = AUDIT) {
  return assistantTurn(h, id, turn => {
    turn.appendChild(h.el('div', { class: 'markdown prose' }, body));
  });
}

function finish(h, turn) {
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', { 'data-testid': 'copy-turn-action-button', 'aria-label': 'Copy response' }));
  turn.appendChild(actions);
}

function installAck(api) {
  const requests = [];
  api.setInauditBridgeRequestForTest((_method, path, payload) => {
    requests.push({ path, payload });
    return {
      ok: true,
      status: 200,
      data: {
        ok: true,
        durable: true,
        duplicate: false,
        record: {
          capture_id: payload.capture_id,
          capture_kind: 'response',
          target_project_id: 'zaicode',
          target_project_name: '_ZAICODE'
        },
        pinned_project: '_ZAICODE'
      }
    };
  });
  return requests;
}

async function flush() {
  for (let i = 0; i < 4; i += 1) await new Promise(resolve => setImmediate(resolve));
}

test('audit reply: the archive name is read from the user turn that carried it', () => {
  const { h, api } = setup();
  assert.equal(api.archiveNameInUserTurn(archiveUserTurn(h, 'u1')), '_ZAICODE.zip');
  assert.equal(
    api.archiveNameInUserTurn(archiveUserTurn(h, 'u2', '_SAIPAL_24.09.26-T09-00-00.zip')),
    '_SAIPAL_24.09.26-T09-00-00.zip'
  );
  assert.equal(api.archiveNameInUserTurn(userTurn(h, 'u3', 'no archive here, just words')), '');
  assert.equal(api.archiveNameInUserTurn(userTurn(h, 'u4', 'see notes.zipper for details')), '');
});

test('audit reply: a streamed reply to an archive turn is captured once with the archive name', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-live');
  const reply = replyTurn(h, 'a-live');
  addTurns(h, [user, reply]);

  api.attachInauditActions(h.dom);
  assert.equal(requests.length, 0, 'nothing is captured while the reply streams');

  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/v1/inaudit/captures');
  assert.equal(requests[0].payload.capture_kind, 'response');
  assert.equal(requests[0].payload.archive_filename, '_ZAICODE.zip');
  assert.ok(requests[0].payload.project_hints.includes('_ZAICODE'));
  assert.match(requests[0].payload.text, /vanished directory/);

  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 1, 're-renders never capture the same reply twice');
});

test('audit reply: an old conversation is never captured on open', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-old');
  const reply = replyTurn(h, 'a-old');
  finish(h, reply);
  addTurns(h, [user, reply]);

  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('audit reply: a short acknowledgement is not an audit', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-short');
  const reply = replyTurn(h, 'a-short', 'Got the archive. What should I look at?');
  addTurns(h, [user, reply]);
  api.attachInauditActions(h.dom);
  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('audit reply: a reply to a turn without an archive is not captured', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  const requests = installAck(api);
  const user = userTurn(h, 'u-plain', 'Explain the walker.');
  const reply = replyTurn(h, 'a-plain');
  addTurns(h, [user, reply]);
  api.attachInauditActions(h.dom);
  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('audit reply: the toggle turns it off', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.auditAutoCapture = false;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-off');
  const reply = replyTurn(h, 'a-off');
  addTurns(h, [user, reply]);
  api.attachInauditActions(h.dom);
  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('audit reply: opt-in auto-assign sends the pinned capture to its project', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.state.auditAutoAssign = true;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-assign');
  const reply = replyTurn(h, 'a-assign');
  addTurns(h, [user, reply]);
  api.attachInauditActions(h.dom);
  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();

  assert.equal(requests.length, 2);
  const captureId = requests[0].payload.capture_id;
  assert.equal(requests[1].path, `/v1/inaudit/captures/${captureId}/assign`);
  assert.equal(JSON.stringify(requests[1].payload), JSON.stringify({ project_id: '' }), 'the Bridge uses the pin, never a guessed id');
});

test('audit reply: auto-assign is off by default', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  const requests = installAck(api);
  const user = archiveUserTurn(h, 'u-noassign');
  const reply = replyTurn(h, 'a-noassign');
  addTurns(h, [user, reply]);
  api.attachInauditActions(h.dom);
  finish(h, reply);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 1);
  assert.equal(api.state.auditAutoAssign, false);
});
