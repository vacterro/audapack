'use strict';

// PERF-001 (audit/9.md): INAUDIT capture retry scheduling must arm exactly one
// real timer after the in-flight guard clears, with earliest-deadline-wins.
//
// The older `PERF-001 (audit/1.md)` regression in w5-001 only asserted that a
// record carried a future `next_retry_at`; it never asserted a timer existed to
// honour it. These tests assert timer LIVENESS, which is the defect.

const test = require('node:test');
const assert = require('node:assert/strict');
const { setup } = require('./helpers');

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

function seed(spool, index, overrides = {}) {
  const cid = `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
  spool.records.set(cid, {
    capture_id: cid,
    payload: { capture_id: cid, text: `item ${index}`, capture_kind: 'response' },
    created_at_ms: index,
    attempts: 1,
    next_retry_at: 0,
    terminal: false,
    ...overrides
  });
  return cid;
}

// The sandbox shares the host realm's `Date`, so patching `Date.now` here also
// moves the widget's clock. Fake timers and the clock are advanced together.
function fakeClock() {
  const real = Date.now;
  let now = real.call(Date);
  Date.now = () => now;
  return {
    get now() { return now; },
    advance(ms) { now += ms; },
    restore() { Date.now = real; }
  };
}

function offline() {
  return { ok: false, status: 0, errorCode: 'bridge_offline', message: 'offline' };
}

// Bootstrap arms one flush timer at load. Drain it against an empty spool so a
// scheduler test starts from a provably timer-free state.
async function cleanSlate(h, api) {
  api.setInauditSpoolBackendForTest(memorySpool().backend);
  h.advance(5000);
  await h.settle();
  assert.equal(api.inauditCaptureFlushTimerState().armed, false, 'empty spool must leave no timer');
}

test('PERF-001 (audit/9.md): an offline flush leaves the record retriable AND arms a real timer', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    const cid = seed(spool, 1);
    api.setInauditBridgeRequestForTest(() => offline());

    const baseline = h.timers.pending().length;
    await api.flushInauditCaptureSpool();

    const record = spool.records.get(cid);
    assert.equal(record.terminal, false, 'an offline endpoint is never permanent');
    assert.ok(record.next_retry_at > Date.now(), 'record must carry a future retry window');

    const state = api.inauditCaptureFlushTimerState();
    assert.equal(state.inFlight, false, 'guard must be released before arming');
    assert.equal(state.armed, true, 'a retry timer must exist after the flush returns');
    assert.ok(state.dueAt > Date.now(), 'armed deadline must be in the future');
    assert.equal(state.pendingDueAt, 0, 'the pending deadline is consumed by the finally block');
    assert.equal(h.timers.pending().length, baseline, 'the single flush timer is replaced, never stacked');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): the armed timer really performs a second send attempt', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    seed(spool, 1);
    let requests = 0;
    api.setInauditBridgeRequestForTest(() => { requests += 1; return offline(); });

    await api.flushInauditCaptureSpool();
    assert.equal(requests, 1);

    const dueAt = api.inauditCaptureFlushTimerState().dueAt;
    const wait = Math.max(0, dueAt - Date.now());
    clock.advance(wait);
    h.advance(wait);
    await h.settle();

    assert.ok(requests >= 2, 'the armed timer must retry without any other API or event');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): not-yet-due records still leave exactly one timer armed', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    seed(spool, 1, { next_retry_at: Date.now() + 120000 });
    let requests = 0;
    api.setInauditBridgeRequestForTest(() => { requests += 1; return offline(); });

    const baseline = h.timers.pending().length;
    await api.flushInauditCaptureSpool();

    assert.equal(requests, 0, 'a not-yet-due record must not be sent');
    assert.equal(api.inauditCaptureFlushTimerState().armed, true);
    assert.equal(h.timers.pending().length, baseline, 'the single flush timer is replaced, never stacked');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): surviving records after a successful pass arm one timer', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    const first = seed(spool, 1);
    const second = seed(spool, 2);
    api.setInauditBridgeRequestForTest((_method, _path, payload) => {
      if (payload.capture_id === first) {
        return {
          ok: true,
          status: 200,
          data: { ok: true, durable: true, record: { capture_id: payload.capture_id } }
        };
      }
      return { ok: false, status: 503, errorCode: 'bridge_unavailable', message: 'busy' };
    });

    const baseline = h.timers.pending().length;
    await api.flushInauditCaptureSpool();

    assert.equal(spool.records.has(first), false, 'durable ACK still deletes the record');
    assert.equal(spool.records.get(second).terminal, false);
    assert.equal(api.inauditCaptureFlushTimerState().armed, true, 'a survivor must keep a live timer');
    assert.equal(h.timers.pending().length, baseline, 'the single flush timer is replaced, never stacked');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): an internal exception still leaves one recovery timer', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    api.setInauditSpoolBackendForTest({
      async list() { throw new Error('IndexedDB unavailable'); },
      async put() {},
      async delete() {}
    });
    api.setInauditBridgeRequestForTest(() => offline());

    const baseline = h.timers.pending().length;
    const ok = await api.flushInauditCaptureSpool();

    assert.equal(ok, false, 'the flush reports its own failure');
    assert.equal(api.inauditCaptureFlushTimerState().armed, true, 'recovery timer must exist');
    assert.equal(h.timers.pending().length, baseline, 'exactly one recovery timer, not an extra one');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): a schedule request raised during an active flush survives to finally', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    seed(spool, 1);
    let sawInFlight = null;
    api.setInauditBridgeRequestForTest(() => {
      // A capture/online wake landing mid-flush must not be discarded.
      api.scheduleInauditCaptureFlush(2000);
      sawInFlight = api.inauditCaptureFlushTimerState();
      return {
        ok: true,
        status: 200,
        data: { ok: true, durable: true, record: { capture_id: spool.records.keys().next().value } }
      };
    });

    await api.flushInauditCaptureSpool();

    assert.equal(sawInFlight.inFlight, true, 'the request ran inside the guard');
    assert.equal(sawInFlight.armed, false, 'no timer may be armed while the flush holds the guard');
    assert.ok(sawInFlight.pendingDueAt > 0, 'the request must be recorded, not discarded');

    const after = api.inauditCaptureFlushTimerState();
    assert.equal(after.armed, true, 'the pending deadline is armed once the guard clears');
    assert.equal(after.pendingDueAt, 0);
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): a 2 s wake brings a 5 min backoff forward', async () => {
  const { h, api } = setup();
  await cleanSlate(h, api);
  const clock = fakeClock();
  try {
    const baseline = h.timers.pending().length;
    api.scheduleInauditCaptureFlush(300000);
    const long = api.inauditCaptureFlushTimerState();
    assert.equal(long.armed, true);

    api.scheduleInauditCaptureFlush(2000);
    const short = api.inauditCaptureFlushTimerState();
    assert.equal(short.armed, true);
    assert.ok(short.dueAt < long.dueAt, 'earliest deadline must win');
    assert.equal(short.dueAt, Date.now() + 2000);
    assert.equal(h.timers.pending().length, baseline + 1, 'still exactly one timer');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): a later deadline cannot postpone an armed earlier one', async () => {
  const { h, api } = setup();
  await cleanSlate(h, api);
  const clock = fakeClock();
  try {
    const baseline = h.timers.pending().length;
    api.scheduleInauditCaptureFlush(2000);
    const early = api.inauditCaptureFlushTimerState();

    api.scheduleInauditCaptureFlush(300000);
    const after = api.inauditCaptureFlushTimerState();

    assert.equal(after.dueAt, early.dueAt, 'an armed earlier timer must not move backward');
    assert.equal(h.timers.pending().length, baseline + 1, 'still exactly one timer');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): repeated scheduler calls keep exactly one active timer', async () => {
  const { h, api } = setup();
  await cleanSlate(h, api);
  const clock = fakeClock();
  try {
    const baseline = h.timers.pending().length;
    for (let i = 0; i < 25; i++) api.scheduleInauditCaptureFlush(5000);
    assert.equal(api.inauditCaptureFlushTimerState().armed, true);
    assert.equal(h.timers.pending().length, baseline + 1, 'no timer storm');
  } finally {
    clock.restore();
  }
});

test('PERF-001 (audit/9.md): one probe still serves many due records, and durable ACK still deletes', async () => {
  const { h, api } = setup();
  const clock = fakeClock();
  try {
    const spool = memorySpool();
    api.setInauditSpoolBackendForTest(spool.backend);
    for (let i = 0; i < 12; i++) seed(spool, i);

    let requests = 0;
    api.setInauditBridgeRequestForTest(() => { requests += 1; return offline(); });
    await api.flushInauditCaptureSpool();
    assert.equal(requests, 1, 'a global outage must probe once, not once per record');
    const attempts = Array.from(spool.records.values()).map(record => Number(record.attempts));
    assert.equal(attempts.filter(value => value === 2).length, 1, 'only the probed record spends an attempt');
    assert.equal(api.inauditCaptureFlushTimerState().armed, true);

    // Durable ACK path unchanged.
    api.setInauditBridgeRequestForTest((_method, _path, payload) => ({
      ok: true,
      status: 200,
      data: { ok: true, durable: true, record: { capture_id: payload.capture_id } }
    }));
    for (const record of spool.records.values()) record.next_retry_at = 0;
    await api.flushInauditCaptureSpool();
    assert.equal(spool.records.size, 0, 'durable ACK removes every record');
  } finally {
    clock.restore();
  }
});
