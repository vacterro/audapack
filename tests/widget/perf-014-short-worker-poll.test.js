'use strict';

// Worker polls must never be held open by the Bridge. Every AUDAPACK window
// sends its GM_xmlhttpRequest through one userscript-manager queue, so six
// managed workers each holding a ~6 s long poll put ~37 s in front of the
// operator's own ZIP ensure and GET. Measured live before this change:
// "GET split hdr 37.57s" against a 3 ms Bridge prep.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

test('PERF-014: a worker poll asks the Bridge for no long-poll hold', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  h.httpResponder = () => ({
    status: 200,
    responseText: JSON.stringify({ ok: true, job: null, owned_job: null, worker_state: 'FREE', status: {} })
  });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  const polls = h.httpRequests.filter(r => String(r.url || '').includes('/v1/browser/poll'));
  assert.strictEqual(polls.length, 1);
  const body = JSON.parse(polls[0].data || '{}');
  assert.strictEqual(body.wait_seconds, 0, 'the poll must not ask the Bridge to hold the request');
});
