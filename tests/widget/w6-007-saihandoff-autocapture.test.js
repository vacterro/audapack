'use strict';

// SRC-083: a reply that finishes with a SAIHANDOFF block is filed without a
// click. The operator used to copy the block into a scratchpad, export it as a
// file and paste that path into the next agent. Now the Bridge writes the file
// and the widget puts its path on the clipboard. These tests pin WHEN that may
// happen: only for a turn this runtime watched stream, once, and never for an
// old conversation being opened or scrolled.

const test = require('node:test');
const assert = require('node:assert/strict');
const { setup, assistantTurn, addTurns } = require('./helpers');

const HANDOFF = 'ProTrail\n\nSAIHANDOFF — RESUME T-59 FROM E-577\n\nMISSION\n\nResume the existing BUILD.\n';
const DROP_PATH = 'V:\\_TEMP_\\audapack_handoffs\\ProTrail_20260923_0244.md';

function handoffTurn(h, id, { final = false, code = HANDOFF } = {}) {
  return assistantTurn(h, id, turn => {
    turn.appendChild(h.el('div', { class: 'markdown prose' }, 'Updated the handoff below.'));
    const wrapper = h.el('div');
    const pre = h.el('pre');
    // ChatGPT's code-block chrome: a header with a label and a Copy control.
    pre.appendChild(h.el('div', { class: 'code-header' }, 'text Copy'));
    pre.appendChild(h.el('code', { class: 'language-text' }, code));
    wrapper.appendChild(pre);
    turn.appendChild(wrapper);
    if (final) finish(h, turn);
  });
}

function finish(h, turn) {
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', { 'data-testid': 'copy-turn-action-button', 'aria-label': 'Copy response' }));
  turn.appendChild(actions);
}

function installAck(h, api, handoff = { ok: true, label: 'ProTrail', path: DROP_PATH, filename: 'ProTrail_20260923_0244.md', reused: false }) {
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
        record: { capture_id: payload.capture_id, capture_kind: 'handoff', classification_confidence: 0.9 },
        handoff
      }
    };
  });
  const clipboard = [];
  h.sandbox.GM_setClipboard = text => { clipboard.push(String(text)); };
  return { requests, clipboard };
}

async function flush() {
  for (let i = 0; i < 4; i += 1) await new Promise(resolve => setImmediate(resolve));
}

test('SRC-083: detection accepts real handoff shapes and rejects ordinary code', () => {
  const { api } = setup();
  assert.equal(api.looksLikeSaihandoff(HANDOFF), true);
  assert.equal(api.looksLikeSaihandoff('AUDAPACK\n\nSAIHANDOFF APPEND — LIVE ESCAPED LATENCY\n'), true);
  assert.equal(api.looksLikeSaihandoff('Wintage — SAIHANDOFF — T-286 continuation\n'), true);
  assert.equal(api.looksLikeSaihandoff('SAIHANDOFF_V1\nHANDOFF_ID: x\n'), true);
  assert.equal(api.looksLikeSaihandoff('const exact = true;\n'), false);
  assert.equal(api.looksLikeSaihandoff(`${'line\n'.repeat(20)}SAIHANDOFF far too late\n`), false);
});

test('SRC-083: the block text is the code body, not the code-block header chrome', () => {
  const { h, api } = setup();
  const turn = handoffTurn(h, 'chrome', { final: true });
  assert.equal(api.handoffBlockText(turn.querySelector('pre')), HANDOFF);
});

test('SRC-083: a streamed reply that finishes with a SAIHANDOFF is filed once and its path is copied', async () => {
  const { h, api } = setup();
  const { requests, clipboard } = installAck(h, api);
  const turn = handoffTurn(h, 'live-1');
  addTurns(h, [turn]);

  api.attachInauditActions(h.dom);
  assert.equal(api.handoffTurnSighting(turn), 'live', 'first seen while streaming');
  assert.equal(requests.length, 0, 'nothing is filed while the reply is still streaming');

  finish(h, turn);
  api.attachInauditActions(h.dom);
  await flush();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/v1/inaudit/captures');
  assert.equal(requests[0].payload.capture_kind, 'handoff');
  assert.equal(requests[0].payload.text, HANDOFF, 'the exact block, byte for byte');
  assert.deepEqual(clipboard, [DROP_PATH], 'the ready file path is on the clipboard');
  const button = turn.querySelector('[data-acb-inaudit-scope="block"]');
  assert.match(button.title, /SAIHANDOFF file: .*ProTrail_20260923_0244\.md \(path copied\)/);

  // Re-renders and later attach passes never file the same turn again.
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 1);
  assert.equal(api.handoffTurnSighting(turn), 'captured');
});

test('SRC-083: the observer marks a streaming turn live even while the attach pass is debounced', () => {
  const { h, api } = setup();
  const turn = handoffTurn(h, 'observed');
  addTurns(h, [turn]);
  api.markInauditTurnsDirty([{ target: turn, addedNodes: [] }]);
  assert.equal(api.handoffTurnSighting(turn), 'live');
});

test('SRC-083: opening an old conversation never re-files its handoffs', async () => {
  const { h, api } = setup();
  const { requests, clipboard } = installAck(h, api);
  const old = handoffTurn(h, 'old-1', { final: true });
  addTurns(h, [old]);

  api.attachInauditActions(h.dom);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(api.handoffTurnSighting(old), 'historical');
  assert.equal(requests.length, 0);
  assert.deepEqual(clipboard, []);
  // The manual block button is still there for a deliberate capture.
  assert.ok(old.querySelector('[data-acb-inaudit-scope="block"]'));
});

test('SRC-083: an ordinary code block in a streamed reply is not filed', async () => {
  const { h, api } = setup();
  const { requests } = installAck(h, api);
  const turn = handoffTurn(h, 'plain', { code: 'const exact = true;\n' });
  addTurns(h, [turn]);
  api.attachInauditActions(h.dom);
  finish(h, turn);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('SRC-083: auto-save off leaves a finished handoff to the manual button', async () => {
  const { h, api } = setup();
  const { requests } = installAck(h, api);
  api.state.handoffAutoCapture = false;
  const turn = handoffTurn(h, 'off');
  addTurns(h, [turn]);
  api.attachInauditActions(h.dom);
  finish(h, turn);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 0);
});

test('SRC-083: a manual block capture of a handoff also hands over the path', async () => {
  const { h, api } = setup();
  const { requests, clipboard } = installAck(h, api, { ok: true, label: 'ProTrail', path: DROP_PATH, filename: 'ProTrail_20260923_0244.md', reused: true });
  const turn = handoffTurn(h, 'manual', { final: true });
  addTurns(h, [turn]);
  const button = h.el('button');
  const result = await api.captureInauditTarget(turn, 'block', turn.querySelector('pre'), button);
  assert.equal(result.ok, true);
  assert.equal(result.handoff.path, DROP_PATH);
  assert.equal(result.handoff.reused, true);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].payload.text, HANDOFF, 'the manual button files the code body, not the header chrome');
  assert.deepEqual(clipboard, [DROP_PATH]);
});

test('SRC-083: a Bridge without handoff support changes nothing', async () => {
  const { h, api } = setup();
  const { clipboard } = installAck(h, api, null);
  const turn = handoffTurn(h, 'old-bridge');
  addTurns(h, [turn]);
  api.attachInauditActions(h.dom);
  finish(h, turn);
  api.attachInauditActions(h.dom);
  await flush();
  assert.deepEqual(clipboard, [], 'no path, no clipboard write');
});

const BRIEF = 'LIMISAW\n\nMISSION\n\nContinue the authoritative LIMISAW repository under SAIPEN control.\n\nDo not restart.\n\nVERIFY\n\nAll gates green.\n';

test('SRC-083: a project-addressed brief without the SAIHANDOFF word is a weak candidate', () => {
  const { api } = setup();
  assert.equal(api.handoffCandidateStrength(HANDOFF), 'strong');
  assert.equal(api.handoffCandidateStrength(BRIEF), 'weak');
  assert.equal(api.handoffCandidateStrength('LIMISAW\nshort\n'), '', 'a name needs a body');
  assert.equal(api.handoffCandidateStrength('ADD AUTOMATIC TRAY SELECTION MODE — ANY AVAILABLE\na\nb\nc\nd\n'), '', 'a sentence is not a name');
  assert.equal(api.handoffCandidateStrength('import os;\na\nb\nc\nd\n'), '');
});

test('SRC-083: a weak candidate is offered handoff-only and filed when the Bridge recognizes the project', async () => {
  const { h, api } = setup();
  const { requests, clipboard } = installAck(h, api, { ok: true, label: 'LIMISAW', path: 'V:\_TEMP_\audapack_handoffs\LIMISAW_20260923_0301.md', filename: 'LIMISAW_20260923_0301.md', reused: false });
  const turn = handoffTurn(h, 'brief', { code: BRIEF });
  addTurns(h, [turn]);
  api.attachInauditActions(h.dom);
  finish(h, turn);
  api.attachInauditActions(h.dom);
  await flush();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].payload.handoff_only, true, 'the Bridge decides; nothing is filed unless it recognizes the project');
  assert.equal(requests[0].payload.capture_kind, 'block');
  assert.equal(requests[0].payload.text, BRIEF);
  assert.deepEqual(clipboard, ['V:\_TEMP_\audapack_handoffs\LIMISAW_20260923_0301.md']);
});

test('SRC-083: a weak candidate the Bridge rejects leaves no clipboard write and no spooled retry', async () => {
  const { h, api } = setup();
  const requests = [];
  api.setInauditBridgeRequestForTest((_method, path, payload) => {
    requests.push({ path, payload });
    return { ok: true, status: 200, data: { ok: true, committed: false, durable: false, handoff: null, skipped: 'not_a_handoff' } };
  });
  const clipboard = [];
  h.sandbox.GM_setClipboard = text => { clipboard.push(String(text)); };
  const result = await api.offerHandoffCandidate(BRIEF, null);
  assert.equal(result.ok, false);
  assert.equal(result.skipped, 'not_a_handoff');
  assert.deepEqual(clipboard, []);
  assert.equal(requests.length, 1);
  await flush();
  assert.equal(requests.length, 1, 'a rejected candidate is not retried');
});
