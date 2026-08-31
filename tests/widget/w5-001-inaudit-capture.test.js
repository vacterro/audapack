'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { setup, assistantTurn, addTurns, composerFixture } = require('./helpers');

function memorySpool() {
  const records = new Map();
  return {
    records,
    backend: {
      async list() { return Array.from(records.values()); },
      async put(record) { records.set(record.capture_id, structuredClone(record)); },
      async delete(captureId) { records.delete(captureId); }
    }
  };
}

function stableAssistant(h, id = 'a1', text = '# Stable audit\nExact response body', withBlock = false) {
  return assistantTurn(h, id, turn => {
    const markdown = h.el('div', { class: 'markdown prose' }, text);
    turn.appendChild(markdown);
    if (withBlock) {
      const wrapper = h.el('div');
      const pre = h.el('pre', {}, 'const exact = true;');
      wrapper.appendChild(pre);
      turn.appendChild(wrapper);
    }
    const actions = h.el('div', { 'aria-label': 'Response actions' });
    actions.appendChild(h.el('button', {
      'data-testid': 'copy-turn-action-button',
      'aria-label': 'Copy response'
    }));
    turn.appendChild(actions);
  });
}

function durableAck(payload, extra = {}) {
  return {
    ok: true,
    status: 200,
    data: {
      ok: true,
      durable: true,
      duplicate: false,
      record: {
        capture_id: payload.capture_id,
        classification_confidence: 0.96,
        suggested_project_name: 'AUDAPACK',
        ...extra
      }
    }
  };
}

test('W5-001: stable assistant response and block receive IA controls; streaming does not', () => {
  const { h, api } = setup();
  const stable = stableAssistant(h, 'stable', 'finished', true);
  const streaming = assistantTurn(h, 'streaming', turn => {
    turn.appendChild(h.el('div', { class: 'markdown prose' }, 'half response'));
  });
  addTurns(h, [stable, streaming]);

  assert.equal(api.attachInauditActions(h.dom), 2);
  assert.ok(stable.querySelector('[data-acb-inaudit-scope="response"]'));
  assert.ok(stable.querySelector('[data-acb-inaudit-scope="block"]'));
  assert.equal(streaming.querySelector('[data-acb-inaudit-scope="response"]'), null);
  assert.equal(api.assistantStableForInaudit(streaming), false);
});

test('W5-001: durable ACK captures exact response text and renders IA checkmark', async () => {
  const { h, api } = setup();
  const turn = stableAssistant(h, 'exact', '# Heading\nExact Ω body');
  addTurns(h, [turn]);
  const requests = [];
  api.setInauditBridgeRequestForTest((_method, _path, payload) => {
    requests.push(payload);
    return durableAck(payload);
  });
  const button = h.el('button');

  const result = await api.captureInauditTarget(turn, 'response', null, button);
  assert.equal(result.ok, true);
  assert.equal(result.queued, false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].text, '# Heading\nExact Ω body');
  assert.equal(button.textContent, 'IA ✓');
  assert.match(button.title, /Captured/);
});

test('W5-001: rendered headings and code blocks retain Markdown structure', () => {
  const { h, api } = setup();
  const surface = h.el('div', { class: 'markdown prose' });
  surface.appendChild(h.el('h2', {}, 'Repair plan'));
  surface.appendChild(h.el('p', {}, 'Keep the heading and fence.'));
  const pre = h.el('pre');
  pre.appendChild(h.el('code', { class: 'language-python' }, 'print("exact")'));
  surface.appendChild(pre);

  assert.equal(
    api.inauditMarkdownFromNode(surface),
    '## Repair plan\n\nKeep the heading and fence.\n\n```python\nprint("exact")\n```'
  );
});

test('W5-001: Bridge outage stores same capture_id in bounded IndexedDB abstraction', async () => {
  const { h, api } = setup();
  const turn = stableAssistant(h, 'offline');
  addTurns(h, [turn]);
  const spool = memorySpool();
  api.setInauditSpoolBackendForTest(spool.backend);
  api.setInauditBridgeRequestForTest(() => ({ ok: false, status: 0, errorCode: 'bridge_offline', message: 'offline' }));
  const button = h.el('button');

  const result = await api.captureInauditTarget(turn, 'response', null, button);
  assert.equal(result.ok, true);
  assert.equal(result.queued, true);
  assert.equal(button.textContent, 'IA QUEUED');
  const records = await api.listInauditSpool();
  assert.equal(records.length, 1);
  assert.equal(records[0].capture_id, records[0].payload.capture_id);
  assert.equal(records[0].payload.text, '# Stable audit\nExact response body');
});

test('W5-001: permanent Bridge rejection is surfaced and never queued', async () => {
  const { h, api } = setup();
  const turn = stableAssistant(h, 'rejected');
  addTurns(h, [turn]);
  const spool = memorySpool();
  api.setInauditSpoolBackendForTest(spool.backend);
  api.setInauditBridgeRequestForTest(() => ({
    ok: false,
    status: 413,
    retriable: false,
    errorCode: 'capture_too_large',
    message: 'capture too large'
  }));
  const button = h.el('button');

  const result = await api.captureInauditTarget(turn, 'response', null, button);

  assert.equal(result.ok, false);
  assert.equal(result.queued, false);
  assert.equal(button.textContent, 'IA !');
  assert.equal(spool.records.size, 0);
});

test('W5-001: reconnect delivers queued identity once and removes only after durable ACK', async () => {
  const { h, api } = setup();
  const turn = stableAssistant(h, 'retry');
  addTurns(h, [turn]);
  const spool = memorySpool();
  api.setInauditSpoolBackendForTest(spool.backend);
  api.setInauditBridgeRequestForTest(() => ({ ok: false, status: 0, message: 'offline' }));
  const queued = await api.captureInauditTarget(turn, 'response', null, h.el('button'));
  const captureId = queued.capture_id;
  const sent = [];
  api.setInauditBridgeRequestForTest((_method, _path, payload) => {
    sent.push(payload.capture_id);
    return durableAck(payload, { status: 'NEW' });
  });
  const record = spool.records.get(captureId);
  record.next_retry_at = 0;
  spool.records.set(captureId, record);

  await api.flushInauditCaptureSpool();
  await api.flushInauditCaptureSpool();
  assert.deepEqual(sent, [captureId]);
  assert.equal(spool.records.size, 0);
});

test('W5-001: occupied chat capture leaves composer and browser dispatch lease unchanged', async () => {
  const { h, api } = setup();
  const composer = composerFixture(h);
  composer.input.textContent = 'manual draft stays';
  const turn = stableAssistant(h, 'occupied', 'old stable answer');
  addTurns(h, [turn]);
  api.browserWorkerLease = { dispatch_id: 'dispatch-1', lease_id: 'lease-1', worker_id: 'worker-1' };
  api.setInauditBridgeRequestForTest((_method, _path, payload) => durableAck(payload));

  const result = await api.captureInauditTarget(turn, 'response', null, h.el('button'));
  assert.equal(result.ok, true);
  assert.equal(composer.input.textContent, 'manual draft stays');
  assert.deepEqual(api.browserWorkerLease, { dispatch_id: 'dispatch-1', lease_id: 'lease-1', worker_id: 'worker-1' });
});

test('W5-001: block scope sends block only and retry exhaustion becomes terminal', async () => {
  const { h, api } = setup();
  const turn = stableAssistant(h, 'block', 'whole response', true);
  addTurns(h, [turn]);
  const block = turn.querySelector('pre');
  let sentText = '';
  api.setInauditBridgeRequestForTest((_method, _path, payload) => {
    sentText = payload.text;
    return durableAck(payload);
  });
  await api.captureInauditTarget(turn, 'block', block, h.el('button'));
  assert.equal(sentText, 'const exact = true;');

  const spool = memorySpool();
  api.setInauditSpoolBackendForTest(spool.backend);
  api.setInauditBridgeRequestForTest(() => ({ ok: false, status: 0, message: 'still offline' }));
  const captureId = '00000000-0000-4000-8000-000000009999';
  spool.records.set(captureId, {
    capture_id: captureId,
    payload: { capture_id: captureId, text: 'queued', capture_kind: 'response' },
    created_at_ms: 1,
    attempts: api.constants.INAUDIT_CAPTURE_MAX_ATTEMPTS - 1,
    next_retry_at: 0,
    terminal: false
  });
  await api.flushInauditCaptureSpool();
  const terminal = spool.records.get(captureId);
  assert.equal(terminal.attempts, api.constants.INAUDIT_CAPTURE_MAX_ATTEMPTS);
  assert.equal(terminal.terminal, true);
  assert.equal(terminal.next_retry_at, 0);
  assert.deepEqual(
    Array.from(api.constants.INAUDIT_CAPTURE_RETRY_DELAYS_MS),
    [2000, 5000, 15000, 30000, 60000, 300000]
  );
});
