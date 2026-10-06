'use strict';

// P1 PERFORMANCE: the manual ZIP button must approach drag-and-drop latency on
// the warm unchanged path WITHOUT weakening any canonical guarantee.
//
// Each test here pins one of the fast paths the ticket introduced, because a
// fast path that is not pinned by evidence is a correctness regression waiting
// for a quiet afternoon:
//
//   TARGET B  the bound click never reads the project registry
//   TARGET C  the picker cache is minutes long and is not a second authority
//   TARGET G  a verified browser byte cache keyed on the canonical SHA-256
//   TARGET H  ALREADY_ATTACHED / ALREADY_SENT stay zero-transport no-ops
//   TARGET A  the click reports bounded phase timings, including the Bridge's
//   TARGET J  the button paints a concrete phase before any await can hide it
//
// Node's crypto.subtle is real here, so a "verified" digest in this suite means
// a digest the widget actually computed.

const { test } = require('node:test');
const assert = require('node:assert');
const nodeCrypto = require('node:crypto');
const {
  setup,
  composerFixture,
  addComposerAttachmentTile
} = require('./helpers');

const TOKEN = 'perf-011-test-token';
const BRIDGE_TOKEN_KEY = 'ai_chatbuttons_bridge_token_v1';
// The filename must resolve to the bound project's identity: the widget treats a
// tile whose artifact name belongs to the bound project as ITS OWN archive (not
// as unrelated payload), and that identity is what every ALREADY_ATTACHED and
// payload-stability decision is built on.
const ARCHIVE_A = '_AUDAPACK_10.09.26-T02-00-00.zip';
const ARCHIVE_B = '_AUDAPACK_11.09.26-T03-00-00.zip';
const USER_TEXT = 'audit this build';

function project(overrides = {}) {
  return {
    project_id: 'audapack',
    display_name: 'AUDAPACK',
    audit_name: 'AUDAPACK',
    group: 'MAIN0',
    slot: 1,
    enabled: true,
    ...overrides
  };
}

function manualSetup() {
  const { h, api } = setup();
  api.mount();
  api.storage.gmSet(BRIDGE_TOKEN_KEY, TOKEN);
  return { h, api };
}

function manualControl(h) {
  return h.dom.querySelector('#acb-manual-zip-btn');
}

function menuItems(h) {
  const list = h.dom.querySelector('#acb-manual-zip-menu-list');
  if (!list) return [];
  return list.children.filter(child => child.getAttribute && child.getAttribute('data-project-id'));
}

function projectsGets(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/v1\/projects$/.test(String(request.url)));
}

function archiveGets(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(String(request.url)));
}

function ensurePosts(h) {
  return h.httpRequests.filter(request => request.method === 'POST' && /\/archive\/ensure$/.test(String(request.url)));
}

function attachComposer(h) {
  const fixture = composerFixture(h);
  const counters = { injections: 0 };
  fixture.upload._onFilesSet = (element, files) => {
    counters.injections += 1;
    for (const file of files) addComposerAttachmentTile(h, file.name);
    h.mutate(fixture.form);
  };
  const recorded = [];
  fixture.send.addEventListener('click', () => {
    const tiles = fixture.form.children.filter(child => child.getAttribute && child.getAttribute('role') === 'group');
    recorded.push({
      text: String(fixture.input.textContent || ''),
      tiles: tiles.map(tile => tile.getAttribute('aria-label'))
    });
    fixture.input.textContent = '';
    for (const tile of fixture.form.children.slice()) {
      if (tile.getAttribute && tile.getAttribute('role') === 'group') tile.remove();
    }
  });
  return { ...fixture, counters, sendRecorder: recorded };
}

function installBridge(h, options = {}) {
  const bytes = Buffer.isBuffer(options.bytes) ? options.bytes : Buffer.from('PK\u0003\u0004 canonical archive bytes');
  const sha = nodeCrypto.createHash('sha256').update(bytes).digest('hex');
  const projects = options.projects || [project()];
  const revision = String(options.revision || 'rev-1');
  let ensureCount = 0;
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (request.method === 'GET' && /\/v1\/projects$/.test(url)) {
      return {
        status: 200,
        responseText: JSON.stringify({ ok: true, revision, projects }),
        delay: options.projectsDelay || 0
      };
    }
    if (request.method === 'POST' && /\/archive\/ensure$/.test(url)) {
      ensureCount += 1;
      const payload = {
        ok: true,
        project_id: 'audapack',
        display_name: 'AUDAPACK',
        filename: options.filename || ARCHIVE_A,
        size: bytes.length,
        mtime: Math.floor(Date.now() / 1000),
        sha256: sha,
        reused: options.reused !== false,
        packed: options.reused === false,
        sha_source: 'receipt',
        timings: {
          ensure_total_ms: 12.5,
          freshness_probe_ms: 9.25,
          archive_pack_ms: 0,
          server_archive_sha_ms: 0.5,
          hot_proof: true,
          source_walk_skipped: true,
          sha_receipt_reused: true
        },
        ...(options.ensure || {})
      };
      return {
        status: 200,
        responseText: JSON.stringify(payload),
        delay: options.ensureDelay || 0
      };
    }
    if (request.method === 'GET' && /\/archive$/.test(url)) {
      return { status: 200, response: new Uint8Array(bytes), delay: options.archiveDelay || 0 };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
  h.bridgeEnsureCount = () => ensureCount;
  return sha;
}

async function drain(h, iterations = 6) {
  for (let i = 0; i < iterations; i += 1) {
    await h.settle();
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function runManual(h, api, options) {
  const promise = api.manualArchiveZipAction(options);
  await drain(h);
  return promise;
}

async function runPicker(h, api, options) {
  const promise = api.openManualArchivePicker(options);
  await drain(h);
  return promise;
}

// ---------------------------------------------------------------------------
// TARGET B: the bound click never pays for the project registry
// ---------------------------------------------------------------------------

test('PERF-011/TARGET B: a bound ZIP click never reads the project registry and reports bounded timings', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await runManual(h, api);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(projectsGets(h).length, 0, 'the normal bound click must not fetch /v1/projects');
  assert.strictEqual(ensurePosts(h).length, 1, 'it goes straight to the canonical archive ensure');
  assert.strictEqual(archiveGets(h).length, 1);
  assert.strictEqual(counters.injections, 1);

  const timings = result.timings;
  assert.ok(timings, 'the click must report phase timings');
  for (const key of [
    'project_resolution_ms',
    'registry_ms',
    'ensure_total_ms',
    'freshness_probe_ms',
    'server_archive_sha_ms',
    'download_ms',
    'browser_sha_ms',
    'attachment_injection_ms',
    'attachment_ready_ms',
    'send_ready_ms',
    'send_verify_ms',
    'total_ms'
  ]) {
    assert.strictEqual(typeof timings[key], 'number', `${key} must be a measured duration`);
  }
  assert.strictEqual(timings.registry_requests, 0);
  assert.strictEqual(timings.registry_ms, 0);
  assert.strictEqual(timings.ensure_result, 'REUSED_EXISTING');
  assert.strictEqual(timings.get_count, 1);
  assert.strictEqual(timings.injection_count, 1);
  assert.strictEqual(timings.send_count, 1);
  assert.strictEqual(timings.freshness_probe_ms, 9.25, 'the Bridge phase evidence is carried through');
  assert.strictEqual(timings.server_archive_sha_ms, 0.5);
  assert.strictEqual(timings.sha_receipt_reused, true);
  assert.strictEqual(timings.hot_proof, true);
  assert.strictEqual(timings.source_walk_skipped, true);
  assert.strictEqual(timings.browser_sha_computed, true);
  assert.strictEqual(api.manualArchiveLastTimings, timings, 'the click and the transaction share ONE timings record');
  // No token, no archive bytes and no composer text may reach the evidence log.
  const serialized = JSON.stringify(api.manualArchiveLogSnapshot());
  assert.strictEqual(serialized.includes(TOKEN), false);
  assert.strictEqual(serialized.includes(USER_TEXT), false);
});

test('PERF-011/TARGET B/C: the cache age never turns a bound click into a registry read', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  await runManual(h, api);
  // A long time later the bound path is still registry-free.
  api.manualArchiveProjectsCache.fetchedAt = Date.now() - (60 * 60 * 1000);
  const later = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(later.ok, true);
  assert.strictEqual(projectsGets(h).length, 0, 'no amount of cache age makes the bound path read the registry');
});

// ---------------------------------------------------------------------------
// TARGET C: a minutes-long, revision-aware picker cache
// ---------------------------------------------------------------------------

test('PERF-011/TARGET C: the picker cache is minutes long, not seconds, and only an explicit refresh bypasses it', async () => {
  const { h, api } = manualSetup();
  installBridge(h);
  assert.ok(api.constants_MANUAL_ARCHIVE_PROJECTS_TTL_MS >= 60000,
    'a 10-second TTL is the defect this replaces');

  await runPicker(h, api);
  assert.strictEqual(projectsGets(h).length, 1);
  assert.strictEqual(api.manualArchiveProjectsCache.revision, 'rev-1');

  // A second open inside the window paints the cached list with zero requests.
  await runPicker(h, api);
  assert.strictEqual(projectsGets(h).length, 1, 'a cached picker list must not re-fetch the registry');
  assert.deepStrictEqual(menuItems(h).map(item => item.getAttribute('data-project-id')), ['audapack']);

  // An aged cache still paints instantly, then revalidates.
  api.manualArchiveProjectsCache.fetchedAt = Date.now() - (api.constants_MANUAL_ARCHIVE_PROJECTS_TTL_MS + 1000);
  const revalidating = api.openManualArchivePicker();
  assert.deepStrictEqual(menuItems(h).map(item => item.getAttribute('data-project-id')), ['audapack'],
    'the cached list is shown immediately while the refresh is in flight');
  await drain(h);
  await revalidating;
  assert.strictEqual(projectsGets(h).length, 2, 'an aged cache revalidates exactly once');

  // Explicit Refresh always owns a new generation.
  await runPicker(h, api, { force: true });
  assert.strictEqual(projectsGets(h).length, 3, 'Refresh list always requests a fresh generation');
});

test('PERF-011/TARGET C: an ensure error for the bound project overrides any cached registry assumption', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });
  assert.strictEqual(projectsGets(h).length, 0);

  // The cache still lists the project, but the Bridge's archive authority says
  // the project is gone. The cache must not be treated as authority.
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (request.method === 'POST' && /\/archive\/ensure$/.test(url)) {
      return {
        status: 404,
        responseText: JSON.stringify({ ok: false, error: { code: 'unknown_project', message: 'Project is not registered' } })
      };
    }
    return { status: 200, responseText: JSON.stringify({ ok: true, projects: [project()] }) };
  };
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'unknown_project');
  assert.strictEqual(api.manualArchiveStaleMarker().code, 'unknown_project');
});

// ---------------------------------------------------------------------------
// TARGET G: content-addressed verified byte cache
// ---------------------------------------------------------------------------

test('PERF-011/TARGET G: an exact SHA cache hit skips the GET and the second browser hash', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  const first = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(first.code, 'ATTACHED_NEW');
  assert.strictEqual(archiveGets(h).length, 1);
  assert.strictEqual(first.timings.browser_sha_cache_hit, false);
  const stats = api.manualArchiveByteCacheStats();
  assert.strictEqual(stats.items, 1, 'the verified bytes are cached under their canonical SHA');

  // The composer is cleared (as after a Send or a manual removal), so the next
  // click MUST re-attach -- but it already holds these exact verified bytes.
  for (const tile of h.dom.documentElement.querySelectorAll('[role="group"]')) tile.remove();
  h.mutate(h.dom.querySelector('form[data-type="unified-composer"]'));

  const second = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(second.ok, true);
  assert.strictEqual(archiveGets(h).length, 1, 'a cache hit must not pay for the Bridge GET');
  assert.strictEqual(counters.injections, 2, 'the tile still had to be re-injected');
  assert.strictEqual(second.timings.browser_sha_cache_hit, true);
  assert.strictEqual(second.timings.browser_sha_computed, false, 'a cache hit skips the second browser hash');
  assert.strictEqual(second.timings.get_count, 0);
  assert.strictEqual(second.timings.ensure_result, 'REUSED_EXISTING');
});

test('PERF-011/TARGET G: a cache MISS performs the GET and the browser hash', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  const shaA = installBridge(h, { archiveDelay: 250 });
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });

  for (const tile of h.dom.documentElement.querySelectorAll('[role="group"]')) tile.remove();
  h.mutate(h.dom.querySelector('form[data-type="unified-composer"]'));

  // A different canonical generation: same project, new bytes, new SHA.
  const bytesB = Buffer.from('PK\u0003\u0004 a genuinely different generation');
  const shaB = installBridge(h, { bytes: bytesB, filename: ARCHIVE_B, archiveDelay: 250 });
  assert.notStrictEqual(shaA, shaB);

  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(archiveGets(h).length, 2, 'a miss downloads');
  assert.strictEqual(result.timings.browser_sha_cache_hit, false);
  assert.strictEqual(result.timings.browser_sha_computed, true, 'a miss verifies the bytes in the browser');
  assert.strictEqual(result.timings.get_count, 1);
  assert.strictEqual(typeof result.timings.browser_sha_ms, 'number');
  assert.strictEqual(result.timings.download_ms > 0, true, 'the transport wait is measured');
  assert.strictEqual(result.meta.sha256, shaB);
});

test('PERF-011/TARGET G: the byte cache is bounded in both items and bytes with deterministic eviction', () => {
  const { api } = manualSetup();
  api.manualArchiveByteCacheClear();
  const maxItems = api.constants_MANUAL_ARCHIVE_BYTE_CACHE_MAX_ITEMS;
  const maxBytes = api.constants_MANUAL_ARCHIVE_BYTE_CACHE_MAX_BYTES;
  assert.ok(maxItems >= 1 && maxBytes > 0);

  for (let index = 0; index < maxItems + 3; index += 1) {
    const sha = String(index).padStart(64, '0');
    api.manualArchiveByteCachePut(sha, new Uint8Array(16));
  }
  const stats = api.manualArchiveByteCacheStats();
  assert.ok(stats.items <= maxItems, `item bound violated: ${stats.items}`);
  assert.ok(stats.bytes <= maxBytes, `byte bound violated: ${stats.bytes}`);

  // Oldest-first: the earliest key was evicted, the newest survived.
  assert.strictEqual(api.manualArchiveByteCacheGet('0'.repeat(64)), null);
  assert.ok(api.manualArchiveByteCacheGet(String(maxItems + 2).padStart(64, '0')));

  // An item larger than the whole budget is refused rather than admitted.
  assert.strictEqual(api.manualArchiveByteCachePut('f'.repeat(64), new Uint8Array(maxBytes + 1)), false);
  api.manualArchiveByteCacheClear();
  const cleared = api.manualArchiveByteCacheStats();
  assert.strictEqual(cleared.items, 0);
  assert.strictEqual(cleared.bytes, 0);
});

test('PERF-011/TARGET G: the byte cache is keyed on content, so a same-named different generation is never reused', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });

  for (const tile of h.dom.documentElement.querySelectorAll('[role="group"]')) tile.remove();
  h.mutate(h.dom.querySelector('form[data-type="unified-composer"]'));

  // SAME filename, DIFFERENT canonical bytes.
  const bytesB = Buffer.from('PK\u0003\u0004 same name, different bytes entirely');
  const shaB = installBridge(h, { bytes: bytesB, filename: ARCHIVE_A });
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.meta.sha256, shaB);
  assert.strictEqual(result.timings.browser_sha_cache_hit, false);
  assert.strictEqual(archiveGets(h).length, 2);
});

// ---------------------------------------------------------------------------
// TARGET H: the zero-work paths stay zero
// ---------------------------------------------------------------------------

test('PERF-011/TARGET H: ALREADY_ATTACHED stays zero GET, zero injection and zero DOM replacement', async () => {
  const { h, api } = manualSetup();
  const { counters, upload } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  const attached = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(attached.code, 'ATTACHED_NEW');
  const getsAfterAttach = archiveGets(h).length;
  const tile = h.dom.documentElement.querySelectorAll('[role="group"]')[0];

  upload._onFilesSet = () => { throw new Error('no injection on the ALREADY_ATTACHED path'); };
  const repeat = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(repeat.ok, true);
  assert.strictEqual(repeat.code, 'ALREADY_ATTACHED');
  assert.strictEqual(archiveGets(h).length, getsAfterAttach, 'no second GET');
  assert.strictEqual(counters.injections, 1, 'no second injection');
  assert.strictEqual(h.dom.documentElement.querySelectorAll('[role="group"]')[0], tile, 'the proven tile is untouched');
  assert.strictEqual(repeat.timings.get_count, 0);
  assert.strictEqual(repeat.timings.injection_count, 0);
});

test('PERF-011/TARGET H: ALREADY_SENT stays zero GET, zero injection and zero Send', async () => {
  const { h, api } = manualSetup();
  const { input, send, counters } = attachComposer(h);
  input.textContent = USER_TEXT;
  installBridge(h);
  api.setManualArchiveBinding(project());

  const sent = await runManual(h, api);
  assert.strictEqual(sent.code, 'SENT');
  assert.strictEqual(send._clickCount, 1);
  const gets = archiveGets(h).length;

  // The receipt window is what makes the identical repeat a no-op; the composer
  // is empty afterwards, exactly as a real accepted Send leaves it.
  const repeat = await runManual(h, api);
  assert.strictEqual(repeat.code, 'ALREADY_SENT');
  assert.strictEqual(send._clickCount, 1, 'a duplicate must not send again');
  assert.strictEqual(archiveGets(h).length, gets, 'a duplicate must not download again');
  assert.strictEqual(counters.injections, 1, 'a duplicate must not inject again');
  assert.strictEqual(repeat.timings.send_count, 0);
  assert.strictEqual(repeat.timings.get_count, 0);
  assert.strictEqual(repeat.timings.injection_count, 0);
});

// ---------------------------------------------------------------------------
// TARGET I: condition-driven waits, never fixed penalties
// ---------------------------------------------------------------------------

test('PERF-011/TARGET I: a tile that becomes ready shortly after the click continues then, not at the timeout', async () => {
  const { h, api } = manualSetup();
  const { send } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  const attached = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(attached.code, 'ATTACHED_NEW');
  const tile = h.dom.documentElement.querySelectorAll('[role="group"]')[0];
  const form = h.dom.querySelector('form[data-type="unified-composer"]');

  // ChatGPT is still registering the tile: its spinner is live. It clears 300 ms
  // later -- far inside the tile-ready bound, and the point is that the click
  // must NOT wait for the bound.
  const spinner = h.el('span', { class: 'animate-spin' });
  tile.appendChild(spinner);
  h.timers.setTimeout(() => {
    spinner.remove();
    h.mutate(form);
  }, 300);

  const startedAt = h.timers.now;
  const promise = api.manualArchiveZipAction();
  for (let index = 0; index < 200 && !send._clicked; index += 1) {
    h.advance(25);
    await new Promise(resolve => setImmediate(resolve));
  }
  const settledAt = h.timers.now;
  const result = await promise;

  assert.strictEqual(send._clickCount, 1, 'the registered tile still sends exactly once');
  assert.strictEqual(result.code, 'SENT');
  assert.strictEqual(settledAt - startedAt < api.constants_MANUAL_ARCHIVE_TILE_READY_TIMEOUT_MS, true,
    `the click waited for the fixed tile timeout instead of the condition (${settledAt - startedAt} ms)`);
});

// ---------------------------------------------------------------------------
// TARGET J: the button reacts visually immediately
// ---------------------------------------------------------------------------

test('PERF-011/TARGET J: the button paints a concrete phase before the archive work can swallow the frame', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, { ensureDelay: 800, archiveDelay: 800 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  // One frame is all the operator should need to see that the click registered.
  h.advance(32);
  const label = manualControl(h).textContent;
  assert.match(label, /CHECK/, `the first painted phase must be concrete, saw ${label}`);
  assert.strictEqual(manualControl(h).dataset.state, 'checking');

  await drain(h);
  h.advance(5000);
  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, true);
});

test('PERF-011/TARGET J: a click that needs real work paints the transport phases as they happen', async () => {
  const { h, api } = manualSetup();
  const { upload } = attachComposer(h);
  installBridge(h, { reused: false, ensureDelay: 600, archiveDelay: 600 });
  api.setManualArchiveBinding(project());
  // A real ChatGPT does not register the uploaded tile synchronously; the
  // operator genuinely waits through ATTACH, so the phase must be observable.
  upload._onFilesSet = (element, files) => {
    for (const file of files) {
      h.timers.setTimeout(() => {
        addComposerAttachmentTile(h, file.name);
        h.mutate(h.dom.querySelector('form[data-type="unified-composer"]'));
      }, 400);
    }
  };

  const promise = api.manualArchiveZipAction({ attachOnly: true });
  const observed = [manualControl(h).dataset.state];
  let settled = false;
  promise.then(() => { settled = true; }, () => { settled = true; });
  // Step the fake clock in small slices WITHOUT settle()'s fast-forward, so the
  // sequence the operator would actually see is observable.
  for (let index = 0; index < 200 && !settled; index += 1) {
    h.advance(25);
    await new Promise(resolve => setImmediate(resolve));
    observed.push(manualControl(h).dataset.state);
  }
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(observed[0], 'checking', 'the click paints CHECK first');
  // PACK and GET are written in one synchronous block (a reuse pack does no
  // work, so nothing is packed at that instant); what must never happen is a
  // silent click. DOWNLOAD and ATTACH are the phases the operator actually
  // waits through here.
  for (const phase of ['downloading', 'attaching', 'ready']) {
    assert.ok(observed.includes(phase), `the ${phase} phase was never painted: ${observed.join(',')}`);
  }
  assert.ok(observed.indexOf('checking') < observed.indexOf('downloading'),
    'phases are painted in the order the work happens');
});
