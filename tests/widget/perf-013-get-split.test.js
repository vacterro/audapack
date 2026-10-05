'use strict';

// SRC-083 (PERF-013 extension): the operator watched the ZIP control sit on
// "GET..." while a direct localhost GET of the same archive took ~8 ms. The one
// "download" number could not say where the time went, so the GET is now split
// into REQUEST START -> HEADERS -> FIRST BODY PROGRESS -> BODY COMPLETE ->
// BYTES NORMALIZED -> BROWSER SHA, and the browser hash is no longer displayed
// as a network GET. Each test pins one guarantee of that split.

const { test } = require('node:test');
const assert = require('node:assert');
const nodeCrypto = require('node:crypto');
const { setup, composerFixture, addComposerAttachmentTile } = require('./helpers');

const TOKEN = 'perf-013-split-token';
const BRIDGE_TOKEN_KEY = 'ai_chatbuttons_bridge_token_v1';
const ARCHIVE_A = '_AUDAPACK_10.09.26-T02-00-00.zip';
const BYTES = Buffer.from('PK\u0003\u0004 canonical archive bytes for the GET split');
const SHA = nodeCrypto.createHash('sha256').update(BYTES).digest('hex');
const SERVER_HEADERS = [
  'content-type: application/zip',
  `content-length: ${BYTES.length}`,
  'x-audapack-archive-sha256-source: receipt',
  'x-audapack-archive-prep-ms: 3.250',
  'x-audapack-archive-digest-ms: 0.500',
  'server-timing: auth;dur=0.1, resolve;dur=2.6, digest;dur=0.5, prep;dur=3.25'
].join('\r\n');

function project() {
  return { project_id: 'audapack', display_name: 'AUDAPACK', audit_name: 'AUDAPACK', group: 'MAIN0', slot: 1, enabled: true };
}

function manualSetup() {
  const { h, api } = setup();
  api.mount();
  api.storage.gmSet(BRIDGE_TOKEN_KEY, TOKEN);
  return { h, api };
}

function meta(overrides = {}) {
  return { filename: ARCHIVE_A, size: BYTES.length, sha256: SHA, ...overrides };
}

function archiveGets(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(String(request.url)));
}

// The GET body arrives at 1000 ms on the fake clock: headers at 20, first body
// progress at 50, last at 900. `archive` overrides any part of that answer.
function installBridge(h, archive = {}) {
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (request.method === 'POST' && /\/archive\/ensure$/.test(url)) {
      return {
        status: 200,
        responseText: JSON.stringify({
          ok: true,
          project_id: 'audapack',
          display_name: 'AUDAPACK',
          filename: ARCHIVE_A,
          size: BYTES.length,
          mtime: Math.floor(Date.now() / 1000),
          sha256: SHA,
          reused: true,
          packed: false,
          sha_source: 'receipt',
          timings: { ensure_total_ms: 1, freshness_probe_ms: 0, hot_proof: true, source_walk_skipped: true, sha_receipt_reused: true }
        })
      };
    }
    if (request.method === 'GET' && /\/archive$/.test(url)) {
      return {
        status: 200,
        response: new Uint8Array(BYTES),
        responseHeaders: SERVER_HEADERS,
        delay: 1000,
        progress: [
          { at: 20, readyState: 2 },
          { at: 50, loaded: 16, total: BYTES.length },
          { at: 900, loaded: BYTES.length, total: BYTES.length }
        ],
        ...archive
      };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
}

function attachComposer(h) {
  const fixture = composerFixture(h);
  fixture.upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
    h.mutate(fixture.form);
  };
  fixture.send.addEventListener('click', () => {
    fixture.input.textContent = '';
    for (const tile of fixture.form.children.slice()) {
      if (tile.getAttribute && tile.getAttribute('role') === 'group') tile.remove();
    }
  });
  return fixture;
}

async function drain(h, iterations = 8) {
  for (let i = 0; i < iterations; i += 1) {
    await h.settle();
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function settleWith(h, promise) {
  await drain(h);
  return promise;
}

test('SRC-083/A+B: one GET records headers, first progress, body complete and bytes separately', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  const download = await settleWith(h, api.fetchProjectArchive('audapack'));

  assert.strictEqual(download.ok, true);
  assert.strictEqual(archiveGets(h).length, 1, 'instrumentation adds no request');
  const probe = download.probe;
  assert.strictEqual(probe.headers_ms, 20, 'readyState 2 marks the response headers');
  assert.strictEqual(probe.first_progress_ms, 50, 'first body progress is its own mark');
  assert.strictEqual(probe.last_progress_ms, 900);
  assert.strictEqual(probe.onload_ms, 1000, 'body complete is onload, not the first byte');
  assert.strictEqual(probe.progress_events, 2);
  assert.strictEqual(probe.loaded, BYTES.length);
  assert.strictEqual(probe.total, BYTES.length);
  assert.strictEqual(probe.capability, 'progress');
  assert.strictEqual(download.bytes.length, BYTES.length);
  assert.deepStrictEqual({ ...download.server }, { prep_ms: 3.25, digest_ms: 0.5, digest_source: 'receipt', present: true });
});

test('SRC-083/B: a manager without progress callbacks stays valid and invents no first byte', async () => {
  const { h, api } = manualSetup();
  installBridge(h, { progress: undefined, responseHeaders: undefined });
  const download = await settleWith(h, api.fetchProjectArchive('audapack'));

  assert.strictEqual(download.ok, true);
  assert.strictEqual(download.probe.capability, 'none');
  assert.strictEqual(download.probe.first_progress_ms, 0, 'no fabricated first-byte evidence');
  assert.strictEqual(download.probe.headers_ms, 0);
  assert.strictEqual(download.probe.onload_ms, 1000, 'the total is still measured');
  // An old Bridge without diagnostic headers is not an error.
  assert.deepStrictEqual({ ...download.server }, { prep_ms: 0, digest_ms: 0, digest_source: '', present: false });
});

test('SRC-083/C: server timing headers are parsed as bounded numbers and a closed source vocabulary', () => {
  const { api } = setup();
  const parsed = api.parseArchiveServerTiming([
    'X-AUDAPACK-Archive-Prep-Ms: -4',
    'X-AUDAPACK-Archive-Digest-Ms: C:\\Users\\secret\\archive.zip',
    'X-AUDAPACK-Archive-SHA256-Source: /etc/passwd'
  ].join('\r\n'));
  assert.deepStrictEqual({ ...parsed }, { prep_ms: 0, digest_ms: 0, digest_source: '', present: false });
  assert.deepStrictEqual({ ...api.parseArchiveServerTiming(undefined) }, { prep_ms: 0, digest_ms: 0, digest_source: '', present: false });
  const huge = api.parseArchiveServerTiming('x-audapack-archive-prep-ms: 99999999');
  assert.strictEqual(huge.prep_ms, 0, 'an absurd value is dropped, not trusted');
});

test('SRC-083/J: the VERIFY stage fires after the body arrives and before the browser hash', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  const order = [];
  const realSubtle = h.sandbox.crypto.subtle;
  h.sandbox.crypto = {
    ...h.sandbox.crypto,
    subtle: { digest: (algorithm, data) => { order.push('sha'); return realSubtle.digest(algorithm, data); } }
  };
  const flight = api.canonicalArchiveBytesFlight('audapack', 'c:split', meta(), stage => {
    order.push(`stage:${stage}:gets=${archiveGets(h).length}`);
  });
  const transport = await settleWith(h, flight);

  assert.strictEqual(transport.ok, true);
  assert.deepStrictEqual(order, ['stage:verifying:gets=1', 'sha'], 'GET is finished before VERIFY, and VERIFY precedes SHA');
  assert.strictEqual(transport.getCount, 1);
  assert.strictEqual(transport.hashComputed, true);
  assert.strictEqual(transport.probe.onload_ms, 1000);
  assert.ok(transport.transportMs >= transport.fetchMs, 'transport completion includes the verified hash');
});

test('SRC-083/N: a warm verified cache performs zero GET, zero SHA and no VERIFY stage', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  await settleWith(h, api.canonicalArchiveBytesFlight('audapack', 'c:warm', meta()));
  assert.strictEqual(archiveGets(h).length, 1);

  const stages = [];
  const warm = await settleWith(h, api.canonicalArchiveBytesFlight('audapack', 'c:warm', meta(), stage => stages.push(stage)));
  assert.strictEqual(warm.fromCache, true);
  assert.strictEqual(warm.getCount, 0);
  assert.strictEqual(warm.fetchMs, 0);
  assert.strictEqual(warm.hashMs, 0);
  assert.strictEqual(warm.hashComputed, false);
  assert.deepStrictEqual(stages, [], 'a cache hit never shows VERIFY');
  assert.strictEqual(archiveGets(h).length, 1, 'the second identical request chose the cached transport');

  const timings = api.manualArchiveTimingsFromTransport(api.manualArchiveTimingBegin(), warm);
  assert.strictEqual(timings.download_ms, 0);
  assert.strictEqual(timings.browser_sha_ms, 0);
  assert.strictEqual(timings.get_total_ms, 0);
  assert.strictEqual(api.manualArchiveGetSplitLine(timings), '', 'no GET, no split line');
});

test('SRC-083/P: size and hash checks stay mandatory, and a failed check never caches bytes', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  const sized = await settleWith(h, api.canonicalArchiveBytesFlight('audapack', 'c:size', meta({ size: BYTES.length + 1 })));
  assert.strictEqual(sized.ok, false);
  assert.strictEqual(sized.errorCode, 'archive-size-mismatch');

  const hashed = await settleWith(h, api.canonicalArchiveBytesFlight('audapack', 'c:hash', meta({ sha256: 'f'.repeat(64) })));
  assert.strictEqual(hashed.ok, false);
  assert.strictEqual(hashed.errorCode, 'archive-digest-mismatch');
  assert.strictEqual(api.manualArchiveByteCacheStats().items, 0, 'unverified bytes never enter the cache');
});

test('SRC-083/P: request errors keep their codes and the GET keeps ONE timeout budget', async () => {
  const { h, api } = manualSetup();
  installBridge(h, { error: true });
  const failed = await settleWith(h, api.fetchProjectArchive('audapack'));
  assert.strictEqual(failed.ok, false);
  assert.strictEqual(failed.errorCode, 'archive-request-failed');

  installBridge(h, { status: 500, response: null });
  const http = await settleWith(h, api.fetchProjectArchive('audapack'));
  assert.strictEqual(http.errorCode, 'archive-http-500');

  installBridge(h, { timeout: true });
  const slow = await settleWith(h, api.fetchProjectArchive('audapack'));
  assert.strictEqual(slow.errorCode, 'archive-request-timeout');

  const gets = archiveGets(h);
  assert.strictEqual(gets.length, 3, 'progress callbacks never trigger a retry request');
  for (const request of gets) {
    assert.strictEqual(request.timeout, 120000, 'one bounded budget per GET, owned by the manager');
    assert.strictEqual(request.headers['X-ACB-Token'], TOKEN, 'the GET stays authenticated');
  }
});

test('SRC-083/A: a real manual delivery carries the whole GET split, once, into the published diagnostic', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await settleWith(h, api.manualArchiveZipAction({ attachOnly: true }));
  assert.strictEqual(result.ok, true);
  const t = result.timings;
  assert.strictEqual(t.get_count, 1);
  assert.strictEqual(t.get_headers_ms, 20);
  assert.strictEqual(t.get_first_progress_ms, 50);
  assert.strictEqual(t.get_total_ms, 1000);
  assert.strictEqual(t.get_transfer_ms, 950, 'transfer is body progress to body complete');
  assert.strictEqual(t.get_bytes, BYTES.length);
  assert.strictEqual(t.get_progress_events, 2);
  assert.strictEqual(t.get_progress_capability, 'progress');
  assert.strictEqual(t.server_get_prep_ms, 3.25);
  assert.strictEqual(t.server_digest_ms, 0.5);
  assert.strictEqual(t.server_digest_source, 'receipt');
  assert.ok(t.download_ms >= 1000, 'download_ms keeps its meaning: request start to normalized bytes');
  assert.ok(t.transport_complete_ms >= t.download_ms);

  const records = [];
  for (const value of h.gmStore.values()) {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    if (!text.includes('delivery_timing')) continue;
    for (const entry of JSON.parse(text)) if (entry.event === 'delivery_timing') records.push(entry);
  }
  assert.strictEqual(records.length, 1, 'exactly one delivery record');
  const record = records[0];
  assert.match(record.message, /GET split hdr 0\.02s \| 1st 0\.05s \| body 0\.95s \| onload 1\.00s/);
  assert.strictEqual(record.timing.get_total_ms, 1000, 'the structured split rides the mirrored record');
  assert.strictEqual(record.timing.server_digest_source, 'receipt');
  const serialized = JSON.stringify(record);
  assert.strictEqual(serialized.includes(TOKEN), false, 'no token in diagnostics');
  assert.strictEqual(serialized.includes('PK'), false, 'no archive bytes in diagnostics');
  assert.strictEqual(/[A-Za-z]:\\\\/.test(serialized), false, 'no filesystem path in diagnostics');
});

test('SRC-083/J+K: VERIFY is a named busy phase and a slow phase shows its elapsed time', () => {
  const { api } = manualSetup();
  assert.strictEqual(api.manualArchivePhaseIsBusy('verifying'), true);
  const base = { detail: '', projectId: 'audapack', projectName: 'AUDAPACK', code: '', conversationKey: '', operationGeneration: 0 };

  api.manualArchiveUiState = { ...base, phase: 'verifying', phaseStartedAt: Date.now(), at: Date.now() };
  assert.match(api.manualArchiveControlLabel('verifying', 'AUDAPACK'), /VERIFY\.\.\.$/, 'an instant phase stays quiet');

  api.manualArchiveUiState = { ...base, phase: 'downloading', phaseStartedAt: Date.now() - 1800, at: Date.now() };
  assert.match(api.manualArchiveControlLabel('downloading', 'AUDAPACK'), /GET 1\.8s$/, 'a stall names itself');

  // A non-busy phase never carries a timer.
  api.manualArchiveUiState = { ...base, phase: 'ready', phaseStartedAt: Date.now() - 5000, at: Date.now() };
  assert.doesNotMatch(api.manualArchiveControlLabel('ready', 'AUDAPACK'), /\d\.\ds$/);
});

test('SRC-083/K: the elapsed ticker runs only while a phase is busy', async () => {
  const { h, api } = manualSetup();
  api.setManualArchiveUiState('downloading', 'Downloading canonical archive...', project());
  assert.strictEqual(api.manualArchiveElapsedTimerActive, true, 'a busy phase schedules the bounded ticker');
  api.setManualArchiveUiState('ready', 'Attached.', project());
  h.advance(600);
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(api.manualArchiveElapsedTimerActive, false, 'the ticker stops with the busy phase');
});

function installProbeBridge(h) {
  const archiveResponder = h.httpResponder;
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (request.method === 'GET' && /\/v1\/probe\/bytes\?size=\d+$/.test(url)) {
      const size = Number(url.split('size=')[1]);
      return {
        status: 200,
        response: new Uint8Array(size),
        delay: 400,
        progress: [{ at: 300, readyState: 2 }, { at: 350, loaded: size, total: size }]
      };
    }
    return archiveResponder(request);
  };
}

function probeRecords(h) {
  const records = [];
  for (const value of h.gmStore.values()) {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    if (!text.includes('transport_probe')) continue;
    for (const entry of JSON.parse(text)) if (entry.event === 'transport_probe') records.push(entry);
  }
  return records;
}

test('SRC-083/F: the transport probe compares GM and native fetch without ever handing fetch the token', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  installProbeBridge(h);
  const fetchCalls = [];
  h.sandbox.fetch = async (url, init) => {
    fetchCalls.push({ url: String(url), init });
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(4096) };
  };

  const result = await settleWith(h, api.archiveTransportProbe({ size: 4096 }));
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.size, 4096);
  assert.deepStrictEqual([...result.runs.map(run => `${run.via}${run.round}`)], ['gm1', 'native1', 'gm2', 'native2']);
  const gm = result.runs.find(run => run.via === 'gm');
  assert.strictEqual(gm.headers_ms, 300);
  assert.strictEqual(gm.total_ms, 400);
  assert.strictEqual(gm.bytes, 4096);
  const native = result.runs.find(run => run.via === 'native');
  assert.strictEqual(native.capability, 'NATIVE_FETCH_SAFE');
  assert.strictEqual(native.bytes, 4096);

  // TARGET F / regression 14: native fetch only ever sees the content-free
  // probe route, with no credential of any kind.
  assert.strictEqual(fetchCalls.length, 2);
  for (const call of fetchCalls) {
    assert.match(call.url, /\/v1\/probe\/bytes\?size=4096$/);
    assert.strictEqual(call.init.credentials, 'omit');
    assert.strictEqual(JSON.stringify(call.init).includes(TOKEN), false);
    assert.strictEqual(call.init.headers, undefined, 'native fetch carries no headers at all');
  }
  const probeGets = h.httpRequests.filter(request => /\/v1\/probe\/bytes/.test(String(request.url)));
  assert.strictEqual(probeGets.length, 2);
  for (const request of probeGets) assert.strictEqual(request.headers['X-ACB-Token'], undefined, 'the probe needs no token');
  assert.strictEqual(archiveGets(h).length, 0, 'the probe never downloads a project archive');

  const records = probeRecords(h);
  assert.strictEqual(records.length, 1, 'one mirrored probe record');
  assert.strictEqual(records[0].timing.gm1_total_ms, 400);
  assert.strictEqual(records[0].timing.native1_capability, 'NATIVE_FETCH_SAFE');
  assert.strictEqual(JSON.stringify(records[0]).includes(TOKEN), false);
});

test('SRC-083/F: a blocked native fetch is recorded as unavailable and production stays on one GM GET', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  installProbeBridge(h);
  const fetchCalls = [];
  h.sandbox.fetch = async url => {
    fetchCalls.push(String(url));
    throw new TypeError('Failed to fetch');
  };

  const probe = await settleWith(h, api.archiveTransportProbe({ size: 2048 }));
  const native = probe.runs.filter(run => run.via === 'native');
  assert.ok(native.every(run => run.ok === false && run.capability === 'NATIVE_FETCH_UNAVAILABLE'));
  assert.match(native[0].error, /^TypeError: Failed to fetch$/);
  assert.ok(probe.runs.filter(run => run.via === 'gm').every(run => run.ok === true), 'GM keeps working');

  // Regression 15/16: the delivery path is untouched by the probe -- exactly
  // one GM archive GET, and native fetch never touches the archive route.
  api.setManualArchiveBinding(project());
  const delivered = await settleWith(h, api.manualArchiveZipAction({ attachOnly: true }));
  assert.strictEqual(delivered.ok, true);
  assert.strictEqual(archiveGets(h).length, 1);
  assert.strictEqual(fetchCalls.length, 2);
  assert.ok(fetchCalls.every(url => /\/v1\/probe\/bytes/.test(url)));
});

test('SRC-083/F: the probe is clamped to the Bridge ceiling and is single-flight', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  installProbeBridge(h);
  h.sandbox.fetch = async () => ({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) });
  const first = api.archiveTransportProbe({ size: 999999999 });
  const second = api.archiveTransportProbe({ size: 16 });
  const [a, b] = await settleWith(h, Promise.all([first, second]));
  assert.strictEqual(a, b, 'a second click joins the running probe');
  assert.strictEqual(a.size, 8 * 1024 * 1024);
  assert.strictEqual(h.httpRequests.filter(request => /\/v1\/probe\/bytes/.test(String(request.url))).length, 2);
});
