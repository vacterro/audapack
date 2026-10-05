'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function pollHarness(overrides = {}) {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-3-1-xyz',
    lease_id: 'lease-0123456789abcdef',
    project_id: 'saitalk',
    project_name: 'SAITALK',
    campaign_run_id: 'acb-run-1',
    start_receipt: 'startcore-1'
  };
  api.autoRuntime = {
    ...api.emptyAutoRuntime({ enabled: true }),
    // T-261: the re-assert is gated on REAL wave ownership. `wait-core` is what
    // an adopted Core actually sits in; a lane whose window never adopted its
    // own Core must stay STARTED.
    stage: overrides.stage || 'wait-core',
    // Route hydration re-derives the runtime and can clear or change the run
    // id; the re-assert must not depend on it.
    runId: overrides.runId === undefined ? 'acb-run-1' : overrides.runId
  };

  const posted = [];
  h.httpResponder = options => {
    const url = String(options.url || '');
    let body = {};
    try { body = JSON.parse(options.data || '{}'); } catch (_) { }
    if (url.includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          job: null,
          owned_job: {
            dispatch_id: 'dsp-0123456789abcdef',
            state: overrides.ownedState || 'STARTED',
            recovery_state: overrides.recoveryState || '',
            campaign_run_id: 'acb-run-1',
            lease_id: 'lease-0123456789abcdef',
            error: overrides.ownedError || ''
          },
          worker_state: 'AUDITING',
          status: {}
        })
      };
    }
    if (url.includes('/state')) {
      posted.push(String(body.state || ''));
      return { status: 200, responseText: JSON.stringify({ ok: true, job: { state: body.state } }) };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };
  return { h, api, posted };
}

test('T87: a poll re-asserts AUDITING for a dispatch stranded in STARTED', async () => {
  // STARTED -> AUDITING is sent once, right after the irreversible Send, and
  // was never retried. A single lost response left the audit running in the
  // browser under a dispatch that said STARTED forever.
  const { h, api, posted } = pollHarness();

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('AUDITING'), `expected an AUDITING re-assert, saw ${JSON.stringify(posted)}`);
  assert.ok(api.browserWorkerLease, 'the lease must survive the re-assert');
});

// T-261: the live split brain, at Bridge level. This window's Core was sent
// but never adopted into the Auto3 runtime, so the desktop lane read AUDITING
// over an engine that owned no wave at all. The re-assert is now gated on real
// ownership: a STARTED lane with no bound wave stays STARTED and keeps being
// re-asserted the moment adoption lands.
test('T87: a STARTED lane whose window adopted nothing is NOT re-asserted', async () => {
  for (const stage of ['idle', 'await-core-user', 'complete', 'paused']) {
    const { h, api, posted } = pollHarness({ stage });

    const pending = api.browserWorkerPollOnce();
    await h.settle();
    await pending;

    assert.ok(!posted.includes('AUDITING'),
      `stage ${stage} owns no current wave; AUDITING would be a lie (saw ${JSON.stringify(posted)})`);
  }
});

test('T87: a dispatch already in AUDITING is not re-asserted', async () => {
  const { h, api, posted } = pollHarness({ ownedState: 'AUDITING' });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.deepStrictEqual(posted, [], `no transition expected, saw ${JSON.stringify(posted)}`);
});

test('T87: a terminal BLOCKED dispatch is acknowledged instead of throwing', async () => {
  // The ACK used `transition`, a local of browserWorkerConsume that does not
  // exist in the poll scope, so this path threw ReferenceError and the block
  // was never acknowledged.
  const { h, api, posted } = pollHarness({
    ownedState: 'BLOCKED',
    recoveryState: '',
    ownedError: 'file-injection-rejected'
  });
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('BLOCKED'), `expected a BLOCKED ack, saw ${JSON.stringify(posted)}`);
  assert.strictEqual(api.browserWorkerLease, null, 'a terminal block must clear the lease');
});

test('T92: a worker told its build is stale reloads to pick the new one up', async () => {
  // Tampermonkey injects the installed build at page load, so an already-open
  // worker window keeps the old script after an update: the Bridge rejects it
  // as STALE_WIDGET and the lane idles until a human reloads six windows.
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = null;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  h.httpResponder = options => {
    if (String(options.url || '').includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          job: null,
          owned_job: null,
          worker_state: 'FREE',
          worker_widget_stale: true,
          required_widget_build: '0.0.26',
          status: {}
        })
      };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const before = h.location.reloaded;
  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.strictEqual(h.location.reloaded, before + 1, 'a stale worker must reload itself');
});

test('T92: a worker mid-audit never reloads to chase a build', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-1-1-xyz',
    lease_id: 'lease-0123456789abcdef'
  };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'running', runId: 'acb-run-1' };

  const before = h.location.reloaded;
  assert.strictEqual(api.browserWorkerReloadForStaleBuild('0.0.26'), false);
  assert.strictEqual(h.location.reloaded, before, 'a run in flight outranks a build update');
});

test('T93: the re-assert survives a runtime that lost its run id', async () => {
  // Route hydration re-arms A3 from the visible receipt and can leave the
  // runtime without the original run id. Requiring it, or re-sending it,
  // turned a harmless progress marker into a permanent STARTED lane.
  const { h, api, posted } = pollHarness({ runId: '' });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('AUDITING'), `expected an AUDITING re-assert, saw ${JSON.stringify(posted)}`);
});

test('T93: the re-assert carries no campaign_run_id', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'w', lease_id: 'lease-0123456789abcdef'
  };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'wait-core', runId: 'acb-drifted' };

  const bodies = [];
  h.httpResponder = options => {
    const url = String(options.url || '');
    if (url.includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true, job: null,
          owned_job: { dispatch_id: 'dsp-0123456789abcdef', state: 'STARTED', lease_id: 'lease-0123456789abcdef' },
          worker_state: 'AUDITING', status: {}
        })
      };
    }
    if (url.includes('/state')) {
      try { bodies.push(JSON.parse(options.data || '{}')); } catch (_) { }
      return { status: 200, responseText: JSON.stringify({ ok: true }) };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  const auditing = bodies.filter(b => b.state === 'AUDITING');
  assert.strictEqual(auditing.length, 1);
  assert.ok(!('campaign_run_id' in auditing[0]), 'a re-assert must not re-send the run id');
});

test('T94: a window parked on a finished audit chat still takes the new build', async () => {
  // autoRuntime.runId survives a terminal dispatch, and treating that as
  // "mid-audit" pinned the window on the old build forever. Observed live:
  // slot 1 stuck at 0.0.26 while the Bridge required 0.0.28.
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  api.browserWorkerLease = null;
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'complete', runId: 'acb-finished' };

  h.httpResponder = options => {
    if (String(options.url || '').includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true, job: null, owned_job: null,
          worker_state: 'FREE', worker_widget_stale: true,
          required_widget_build: '0.0.29', status: {}
        })
      };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const before = h.location.reloaded;
  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.strictEqual(h.location.reloaded, before + 1, 'a finished chat must not pin the old build');
});

test('T94: a window the Bridge says still owns a dispatch never reloads', async () => {
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  api.browserWorkerLease = null;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  const before = h.location.reloaded;
  const blocked = api.browserWorkerReloadForStaleBuild('0.0.29', { dispatch_id: 'dsp-0123456789abcdef', state: 'AUDITING' });
  assert.strictEqual(blocked, false);
  assert.strictEqual(h.location.reloaded, before, 'an owned run outranks a build update');
});

test('T124: a poll re-asserts STARTED for a dispatch stranded in START_PREPARED', async () => {
  // The same lost call one step earlier. START_PREPARED -> STARTED is sent
  // once, and the re-assert above only fires on STARTED, so a lost response
  // left the dispatch at START_PREPARED while the browser audited straight
  // past it. Observed live: SAIPEN with 2 of 3 waves already on disk and its
  // lane still reading START_PREPARED 110 minutes after the Core went out.
  const { h, api, posted } = pollHarness({ ownedState: 'START_PREPARED' });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(posted.includes('STARTED'), `expected a STARTED re-assert, saw ${JSON.stringify(posted)}`);
  assert.ok(api.browserWorkerLease, 'the lease must survive the re-assert');
});

test('T124: START_PREPARED is not re-asserted without proof the Core was sent', async () => {
  // START_PREPARED is the exactly-once boundary. Claiming STARTED from a
  // window that cannot show a committed handoff or a live runtime would be
  // asserting a send that may never have happened.
  const { h, api, posted } = pollHarness({ ownedState: 'START_PREPARED', runId: '' });
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  assert.ok(!posted.includes('STARTED'), `no STARTED expected, saw ${JSON.stringify(posted)}`);
});

test('T261: the AUDITING re-assert promotes the job off the phantom draft key', async () => {
  // STARTED is written exactly once, immediately after the irreversible Send,
  // and it pins conversation_id to whatever the route was AT THAT MOMENT: a
  // draft. ChatGPT then hydrates /c/<id>, and no transition ever re-sent the
  // key, so the job kept naming a conversation that no longer exists while its
  // worker sat on the real one. Observed live: dsp-6e79f5c354f54b56 held
  // "draft:audapack-managed-2-1-6f397f4d6rgnm3:5926b6c2-9a3a-4384-a276-fc366e2f506c"
  // for its whole life while the window sat on
  // /c/6ac17c95-4798-83eb-a8af-2424c23f75ef -- so nothing downstream could ever
  // prove adoption, and the post-start job became uncancellable.
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  // ChatGPT has hydrated the real route by the time the poll runs.
  h.location.pathname = '/c/6ac17c95-4798-83eb-a8af-2424c23f75ef';
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-2-1-xyz',
    lease_id: 'lease-0123456789abcdef',
    campaign_run_id: 'acb-run-1'
  };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'wait-core', runId: 'acb-run-1' };

  const bodies = [];
  h.httpResponder = options => {
    const url = String(options.url || '');
    if (url.includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true, job: null,
          owned_job: {
            dispatch_id: 'dsp-0123456789abcdef',
            state: 'STARTED',
            lease_id: 'lease-0123456789abcdef',
            conversation_id: 'draft:audapack-managed-2-1-xyz:5926b6c2'
          },
          worker_state: 'AUDITING', status: {}
        })
      };
    }
    if (url.includes('/state')) {
      try { bodies.push(JSON.parse(options.data || '{}')); } catch (_) { }
      return { status: 200, responseText: JSON.stringify({ ok: true }) };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  const auditing = bodies.filter(b => b.state === 'AUDITING');
  assert.strictEqual(auditing.length, 1, `expected one AUDITING re-assert, saw ${JSON.stringify(bodies.map(b => b.state))}`);
  assert.strictEqual(auditing[0].conversation_id, 'c:6ac17c95-4798-83eb-a8af-2424c23f75ef',
    'the re-assert must carry the LIVE conversation key so the job promotes off the dead draft id');
});

test('T261: promotion never downgrades a live job key back to a draft', async () => {
  // The same re-assert runs on every poll, including polls taken while the page
  // is still a draft. Preferring the live route must not mean preferring a
  // draft: a job already promoted to `c:` keeps it, or the promotion would
  // oscillate with every poll and the Bridge would chase the key forever.
  const { h, api } = setup();
  api.state.bridgeEnabled = true;
  h.gmStore.set('ai_chatbuttons_bridge_token_v1', 'test-token');
  h.location.pathname = '';
  api.browserWorkerLease = {
    dispatch_id: 'dsp-0123456789abcdef',
    worker_id: 'audapack-managed-2-1-xyz',
    lease_id: 'lease-0123456789abcdef',
    campaign_run_id: 'acb-run-1'
  };
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: true }), stage: 'wait-core', runId: 'acb-run-1' };

  const bodies = [];
  h.httpResponder = options => {
    const url = String(options.url || '');
    if (url.includes('/v1/browser/poll')) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true, job: null,
          owned_job: {
            dispatch_id: 'dsp-0123456789abcdef',
            state: 'AUDITING',
            lease_id: 'lease-0123456789abcdef',
            conversation_id: 'c:6ac17c95-4798-83eb-a8af-2424c23f75ef'
          },
          worker_state: 'AUDITING', status: {}
        })
      };
    }
    if (url.includes('/state')) {
      try { bodies.push(JSON.parse(options.data || '{}')); } catch (_) { }
      return { status: 200, responseText: JSON.stringify({ ok: true }) };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true }) };
  };

  const pending = api.browserWorkerPollOnce();
  await h.settle();
  await pending;

  for (const body of bodies) {
    if (body.state !== 'AUDITING') continue;
    assert.ok(!String(body.conversation_id || '').startsWith('draft:'),
      `a draft route must never be reported onto the job (saw ${body.conversation_id})`);
  }
});
