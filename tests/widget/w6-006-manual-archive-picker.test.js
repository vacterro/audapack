'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const nodeCrypto = require('node:crypto');
const {
  setup,
  mainEl,
  composerFixture,
  addComposerAttachmentTile
} = require('./helpers');
const { FakeEvent } = require('./harness');

const TOKEN = 'w6-006-test-token';
const NEW_ARCHIVE = '_AUDAPACK_09.09.26-T01-18-12.zip';
const NEW_ARCHIVE_2 = '_AUDAPACK_10.09.26-T02-00-00.zip';
const OLD_ARCHIVE = '_AUDAPACK_01.01.26-T00-00-00.zip';
const OTHER_ARCHIVE = '_FASTPROMPTER_05.05.26-T05-00-00.zip';
const USER_TEXT = 'Пусть клауд тоже будет виден';
const BRIDGE_TOKEN_KEY = 'ai_chatbuttons_bridge_token_v1';

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

function otherProject(overrides = {}) {
  return project({ project_id: 'fastprompter', display_name: 'FastPrompter', audit_name: 'FASTPROMPTER', ...overrides });
}

function installBridge(h, options = {}) {
  const bytes = Buffer.isBuffer(options.bytes) ? options.bytes : Buffer.from('PK\u0003\u0004 canonical archive bytes');
  const sha = nodeCrypto.createHash('sha256').update(bytes).digest('hex');
  const projects = options.projects || [project()];
  // The real Bridge only packs a project once: an ensure for a source it has
  // already packed answers REUSED_EXISTING. The fake Bridge models that, so a
  // second transaction against unchanged source is a metadata no-op while a
  // changed source (new SHA) packs again. State lives on the harness so it
  // survives the per-scenario responder swaps.
  const packed = h.fakeBridgePackedShas = h.fakeBridgePackedShas || new Set();
  const ensure = {
    project_id: 'audapack',
    display_name: 'AUDAPACK',
    filename: NEW_ARCHIVE,
    size: bytes.length,
    mtime: Math.floor(Date.now() / 1000),
    sha256: sha,
    reused: false,
    packed: true,
    ...(options.ensure || {})
  };
  const archiveStatus = options.archiveStatus || 200;
  const projectIdFrom = url => {
    const match = String(url || '').match(/\/v1\/projects\/([^/]+)\/archive/);
    return match ? decodeURIComponent(match[1]) : '';
  };
  h.httpResponder = request => {
    const url = String(request.url || '');
    if (options.offline) return { error: true };
    if (request.method === 'GET' && /\/v1\/projects$/.test(url)) {
      return { status: 200, responseText: JSON.stringify({ ok: true, projects }), delay: options.projectsDelay || 0 };
    }
    if (request.method === 'POST' && /\/archive\/ensure$/.test(url)) {
      if (options.ensureStatus && options.ensureStatus !== 200) {
        return {
          status: options.ensureStatus,
          responseText: JSON.stringify({ ok: false, error: { code: options.ensureErrorCode || 'archive_unavailable', message: 'ensure down', retriable: true } })
        };
      }
      const projectId = projectIdFrom(url);
      const explicit = (options.ensureByProject && options.ensureByProject[projectId]) || options.ensure || null;
      const candidateSha = String(explicit?.sha256 || sha);
      const alreadyPacked = packed.has(candidateSha);
      packed.add(candidateSha);
      let payload = {
        ...ensure,
        reused: explicit?.reused ?? alreadyPacked,
        packed: explicit?.packed ?? !alreadyPacked
      };
      if (options.ensureByProject && options.ensureByProject[projectId]) {
        const registered = projects.find(item => item.project_id === projectId) || {};
        payload = {
          project_id: projectId,
          display_name: registered.display_name || projectId,
          filename: NEW_ARCHIVE,
          size: bytes.length,
          mtime: Math.floor(Date.now() / 1000),
          sha256: candidateSha,
          reused: payload.reused,
          packed: payload.packed,
          ...options.ensureByProject[projectId]
        };
      }
      return { status: 200, responseText: JSON.stringify({ ok: true, ...payload }), delay: options.ensureDelay || 0 };
    }
    if (request.method === 'GET' && /\/archive$/.test(url)) {
      if (archiveStatus !== 200) {
        return { status: archiveStatus, responseText: JSON.stringify({ ok: false, error: { code: 'archive_unavailable', message: 'no archive' } }) };
      }
      return { status: 200, response: new Uint8Array(bytes), delay: options.archiveDelay || 0 };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
  return sha;
}

function manualSetup(options = {}) {
  const { h, api } = setup();
  api.mount();
  api.storage.gmSet(BRIDGE_TOKEN_KEY, TOKEN);
  if (options.autoRuntime) api.autoRuntime = options.autoRuntime;
  return { h, api };
}

function manualControl(h) {
  return h.dom.querySelector('#acb-manual-zip-btn');
}

function menuItems(h) {
  return h.dom.querySelectorAll('#acb-manual-zip-menu-list button[data-project-id]');
}

function composerTilesNamed(h, name) {
  return h.dom.querySelectorAll('[role="group"]').filter(element => element.getAttribute('aria-label') === name);
}

function ensureRequests(h) {
  return h.httpRequests.filter(request => request.method === 'POST' && /\/archive\/ensure$/.test(String(request.url || '')));
}

function archiveGets(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(String(request.url || '')));
}

function archiveRequests(h) {
  return archiveGets(h);
}

function sendClicks(h) {
  const send = h.dom.documentElement.querySelector('form[data-type="unified-composer"] button[data-testid="send-button"]');
  return Number(send?._clickCount || 0);
}

function projectsGets(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/v1\/projects$/.test(String(request.url || '')));
}

function attachComposer(h, options = {}) {
  const fixture = composerFixture(h);
  const counters = { injections: 0, uploads: 0 };
  fixture.  upload._onFilesSet = (element, files) => {
    counters.injections += 1;
    for (const file of files) {
      counters.uploads += 1;
      addComposerAttachmentTile(h, file.name);
    }
    // A real upload mutates the composer; the harness notifies observers only
    // when asked, so the widget's MutationObserver gets an explicit nudge.
    h.mutate(fixture.form);
  };
  installSendAcceptance(h, fixture, options);
  return { ...fixture, counters, sendRecorder: fixture.sendRecorder };
}

// P1 TARGET J: a click alone is never Send acceptance. This models the real
// ChatGPT outcome of an accepted submission -- the prepared composer (authored
// text and the attachment tiles) leaves the composer -- so the widget's
// positive-evidence check, not a test shortcut, decides the result. The
// recorder captures what was in the composer AT the moment of submission.
function installSendAcceptance(h, fixture, options = {}) {
  const recorded = [];
  // `accept` may be a boolean or a probe, so a test can model ChatGPT refusing
  // a submission and later accepting the identical retry.
  const clearComposer = () => (typeof options.accept === 'function' ? options.accept() : options.accept !== false);
  const originalClick = fixture.send.click.bind(fixture.send);
  const capture = () => {
    const tiles = fixture.form.children.filter(child => child.getAttribute && child.getAttribute('role') === 'group');
    recorded.push({
      text: String(fixture.input.textContent || ''),
      tiles: tiles.map(tile => tile.getAttribute('aria-label'))
    });
  };
  const detachTiles = () => {
    for (const tile of fixture.form.children.slice()) {
      if (tile.getAttribute && tile.getAttribute('role') === 'group') tile.remove();
    }
  };
  fixture.send.addEventListener('click', () => {
    capture();
    if (!clearComposer()) return;
    fixture.input.textContent = '';
    detachTiles();
  });
  fixture.send._detachTiles = detachTiles;
  fixture.sendRecorder = recorded;
  fixture.sendCapture = capture;
  fixture.sendRawClick = originalClick;
  return recorded;
}

// Fake HTTP responses arrive through the harness timers, and digest work runs
// on the real microtask queue; a single settle() can stop between those hops.
// T-248: the shared upload engine (discovery, composer-ownership proof,
// remount-safe retry) adds real async steps between injection and Send, so the
// fake clock needs more turns before the transaction settles. The assertions
// below are unchanged; only the number of turns moved.
async function drain(h, iterations = 30) {
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
// T-193 TARGET C/P: the manual control exists in a normal ChatGPT chat
// ---------------------------------------------------------------------------

test('W6-006/T-193: the manual ZIP control renders in the titlebar of a normal chat', () => {
  const { h, api } = manualSetup();
  const button = manualControl(h);
  assert.ok(button, 'manual ZIP control must exist');
  assert.strictEqual(button.textContent, 'ZIP \u25be');
  assert.strictEqual(button.dataset.state, 'unbound');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');
  assert.ok(menu);
  assert.strictEqual(menu.hidden, true, 'picker starts closed');
  assert.strictEqual(api.manualArchiveBindingFor(), null);
});

test('W6-006/T-193: clicking the unbound control loads the enabled project registry and opens the picker', async () => {
  const { h } = manualSetup();
  installBridge(h, {
    projects: [project(), otherProject(), project({ project_id: 'off', display_name: 'OFF', enabled: false })]
  });
  manualControl(h).click();
  await h.settle();
  assert.strictEqual(projectsGets(h).length, 1, 'one registry read');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');
  assert.strictEqual(menu.hidden, false);
  const items = menuItems(h);
  assert.deepStrictEqual(items.map(item => item.getAttribute('data-project-id')), ['audapack', 'fastprompter']);
});

test('W6-006/P1: selecting a project persists the binding, attaches the canonical ZIP and sends it automatically', async () => {
  const { h, api } = manualSetup();
  const { input, send, counters, sendRecorder } = attachComposer(h);
  input.textContent = USER_TEXT;
  installBridge(h);
  manualControl(h).click();
  await h.settle();
  menuItems(h)[0].click();
  await drain(h);
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack');
  assert.deepStrictEqual(Object.keys(api.readManualArchiveBindings()), ['c:abc123']);
  assert.strictEqual(ensureRequests(h).length, 1);
  assert.strictEqual(archiveRequests(h).length, 1);
  assert.strictEqual(counters.injections, 1, 'the archive was injected once');
  // The widget sent the exact payload the operator had authored -- it never
  // rewrote the text, it only submitted it.
  assert.strictEqual(send._clicked, true, 'the project selection sends automatically');
  assert.deepStrictEqual(sendRecorder, [{ text: USER_TEXT, tiles: [NEW_ARCHIVE] }],
    'the submitted payload is exactly the authored text plus the canonical archive');
  assert.strictEqual(api.composerPlainText(input), '', 'the composer was submitted, not rewritten');
});

// ---------------------------------------------------------------------------
// TARGET A/E/F/G: decoupling, fast no-op, ensure/download deduplication
// ---------------------------------------------------------------------------

test('W6-006/P1: manual ZIP runs the whole attach+send transaction without ever starting Auto Audit', async () => {
  const { h, api } = manualSetup();
  const { form } = attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  oldTile.querySelector('button').addEventListener('click', () => oldTile.remove());
  installBridge(h);
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, true);
  assert.notStrictEqual(api.autoRuntime?.enabled, true, 'manual ZIP never enables Auto');
  assert.notStrictEqual(String(api.autoRuntime?.stage || 'idle'), 'complete');
  assert.strictEqual(api.readStartAuditHandoff(), null, 'no START handoff is armed');
  assert.strictEqual(api.readA3Intent(), null, 'no A3 intent is written');
  assert.strictEqual(api.browserWorkerLease, null, 'no browser worker job is created');
  assert.ok(form);
});

test('W6-006/P1: manual ZIP sends exactly once and never advances a campaign', async () => {
  const { h, api } = manualSetup();
  const { send } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.code, 'SENT');
  assert.strictEqual(Number(send._clickCount || 0), 1, 'exactly one Send');
  assert.notStrictEqual(api.autoRuntime?.stage, 'complete');
});

test('W6-006/P1: a proven canonical tile is ALREADY_ATTACHED with zero GET/injection and still sends', async () => {
  const { h, api } = manualSetup();
  const { upload } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());

  // TARGET T: the attach-only escape hatch leaves a proven canonical tile in
  // the composer without sending it.
  const attached = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(attached.ok, true);
  assert.strictEqual(attached.code, 'ATTACHED_NEW');
  assert.strictEqual(sendClicks(h), 0, 'attach-only never sends');

  installBridge(h, { ensure: { reused: true, packed: false } });
  upload._onFilesSet = () => { throw new Error('no injection on the ALREADY_ATTACHED path'); };
  const getsBefore = archiveGets(h).length;

  // TARGET L: transport idempotency and Send intent are separate layers, so an
  // ALREADY_ATTACHED archive still proceeds to Send on a normal ZIP action.
  const second = await runManual(h, api);
  assert.strictEqual(second.ok, true);
  assert.strictEqual(second.code, 'SENT');
  assert.strictEqual(second.ensureCode, 'REUSED_EXISTING');
  assert.strictEqual(second.getCount, 0, 'no GET on the ALREADY_ATTACHED path');
  assert.strictEqual(second.injectionCount, 0, 'no injection on the ALREADY_ATTACHED path');
  assert.strictEqual(archiveGets(h).length, getsBefore, 'no GET on the ALREADY_ATTACHED path');
  assert.strictEqual(second.attachCode, undefined, 'the transport stage was a no-op');
  assert.strictEqual(sendClicks(h), 1, 'the ALREADY_ATTACHED path still sends exactly once');
});

test('W6-006/P1: an identical repeat after a verified Send is ALREADY_SENT with zero transport and zero Send', async () => {
  const { h, api } = manualSetup();
  const { upload } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  const first = await runManual(h, api);
  assert.strictEqual(first.code, 'SENT');
  assert.strictEqual(sendClicks(h), 1);

  upload._onFilesSet = () => { throw new Error('ALREADY_SENT must never inject'); };
  const getsBefore = archiveGets(h).length;

  // A double/accidental repeat lands inside the send-receipt window. One
  // Bridge ensure is how the widget proves the canonical SHA is still current;
  // it is a metadata no-op (REUSED_EXISTING), never a repack.
  const second = await runManual(h, api);
  assert.strictEqual(second.ok, true);
  assert.strictEqual(second.code, 'ALREADY_SENT', 'an identical payload must not create a second ChatGPT turn');
  assert.strictEqual(second.ensureCode, 'REUSED_EXISTING', 'no archive is repacked for a duplicate');
  assert.strictEqual(archiveGets(h).length, getsBefore, 'ALREADY_SENT performs zero GET');
  assert.strictEqual(second.injectionCount, 0, 'ALREADY_SENT injects nothing');
  assert.strictEqual(sendClicks(h), 1, 'ALREADY_SENT performs zero Send');
});

test('W6-006/P1: a changed composer payload defeats ALREADY_SENT and is sent', async () => {
  const { h, api } = manualSetup();
  const { input } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  const first = await runManual(h, api);
  assert.strictEqual(first.code, 'SENT');

  // Same canonical archive, genuinely different outgoing text.
  input.textContent = 'second, different prompt';
  const second = await runManual(h, api);
  assert.strictEqual(second.ok, true);
  assert.strictEqual(second.code, 'SENT', 'a changed payload is not a duplicate');
  assert.strictEqual(sendClicks(h), 2);
});

test('W6-006/P1: ten rapid clicks collapse into one transaction with one ensure, one GET, one injection and one Send', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  installBridge(h, { ensureDelay: 500 });
  api.setManualArchiveBinding(project());
  const promises = [];
  for (let i = 0; i < 10; i += 1) promises.push(api.manualArchiveZipAction());
  await drain(h);
  const results = await Promise.all(promises);
  assert.strictEqual(ensureRequests(h).length, 1, 'one ensure chain');
  assert.strictEqual(archiveGets(h).length, 1, 'one GET');
  assert.strictEqual(counters.injections, 1, 'one injection');
  assert.strictEqual(sendClicks(h), 1, 'one Send');
  assert.strictEqual(results.filter(result => result.dedupe === 'DUPLICATE_COALESCED').length, 9);
  assert.strictEqual(results.filter(result => result.ok).length, 10);
});

test('W6-006/P1: a manual ZIP and a concurrent Auto ZIP share one canonical archive transport', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  installBridge(h, { ensureDelay: 500 });
  api.autoRuntime = { ...api.emptyAutoRuntime({ enabled: false }), projectId: 'audapack', projectName: 'AUDAPACK' };
  api.setManualArchiveBinding(project());
  const auto = api.attachProjectArchiveForCurrentChat();
  const manual = api.manualArchiveZipAction();
  await drain(h);
  const [autoResult, manualResult] = await Promise.all([auto, manual]);
  assert.strictEqual(autoResult.ok, true);
  assert.strictEqual(manualResult.ok, true);
  assert.strictEqual(archiveGets(h).length, 1, 'one canonical download, not two');
  assert.strictEqual(counters.injections, 1, 'one injection');
  assert.strictEqual(sendClicks(h), 1, 'the manual transaction still owns exactly one Send');
  assert.strictEqual(manualResult.code, 'SENT');
  assert.strictEqual(manualResult.dedupe, 'DUPLICATE_COALESCED');
});

// ---------------------------------------------------------------------------
// TARGET J: filename is metadata, content identity is SHA
// ---------------------------------------------------------------------------

test('W6-006/T-193: same filename without a canonical proof is not trusted and is replaced once proven', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  const unprovenTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  unprovenTile.querySelector('button').addEventListener('click', () => unprovenTile.remove());
  installBridge(h, { ensure: { reused: true, packed: false } });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.code, 'REPLACED_OLD');
  assert.strictEqual(archiveGets(h).length, 1, 'unproven identical name still refreshes');
  assert.strictEqual(counters.injections, 1);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
  assert.strictEqual(sendClicks(h), 0, 'the attach-only escape hatch never sends');
});

test('W6-006/T-193: same filename with a different canonical SHA is replaced safely after the new tile is ready', async () => {
  const { h, api } = manualSetup();
  const { upload } = attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });
  const oldTile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  assert.ok(oldTile);
  oldTile.querySelector('button').addEventListener('click', () => oldTile.remove());

  const bytesB = Buffer.from('PK\u0003\u0004 a genuinely different canonical archive');
  const shaB = nodeCrypto.createHash('sha256').update(bytesB).digest('hex');
  installBridge(h, { bytes: bytesB, ensure: { sha256: shaB } });
  let oldPresentWhenNewAdded = null;
  upload._onFilesSet = (element, files) => {
    oldPresentWhenNewAdded = Boolean(oldTile.parentNode);
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.code, 'REPLACED_OLD');
  assert.strictEqual(result.meta.sha256, shaB);
  assert.strictEqual(oldPresentWhenNewAdded, true, 'the old tile survives until the replacement is proven');
  assert.strictEqual(oldTile.parentNode, null);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
  assert.strictEqual(sendClicks(h), 0);
});

test('W6-006/T-193: a different filename with the identical proven canonical SHA is not redundantly uploaded', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  const first = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(first.ok, true);

  installBridge(h, { ensure: { filename: NEW_ARCHIVE_2, reused: true, packed: false } });
  const getsBefore = archiveGets(h).length;
  const tile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.code, 'ALREADY_ATTACHED', 'content-identical bytes are a duplicate regardless of filename');
  assert.strictEqual(archiveGets(h).length, getsBefore);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE)[0], tile);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE_2).length, 0);
  assert.strictEqual(sendClicks(h), 0);
});

test('W6-006/T-193: a changed canonical source invalidates the old proof and packs fresh', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });

  const firstTile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  assert.ok(firstTile);
  firstTile.querySelector('button').addEventListener('click', () => firstTile.remove());
  const bytesB = Buffer.from('PK\u0003\u0004 changed source archive');
  const shaB = nodeCrypto.createHash('sha256').update(bytesB).digest('hex');
  installBridge(h, { bytes: bytesB, ensure: { sha256: shaB, reused: false, packed: true } });
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.ensureCode, 'PACKED_NEW');
  assert.strictEqual(result.code, 'REPLACED_OLD');
  assert.strictEqual(result.meta.sha256, shaB);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
});

// ---------------------------------------------------------------------------
// TARGET I/O: failure paths preserve the previous archive; errors are bounded
// ---------------------------------------------------------------------------

test('W6-006/T-193: a failed download preserves the previous archive and fails visibly', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h, { archiveStatus: 500 });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'FAILED');
  assert.strictEqual(oldTile.parentNode !== null, true);
  assert.strictEqual(manualControl(h).dataset.state, 'error');
});

test('W6-006/T-193: a SHA mismatch fails closed and keeps the previous archive', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h, { ensure: { sha256: 'deadbeef' } });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'HASH_MISMATCH');
  assert.strictEqual(oldTile.parentNode !== null, true);
  assert.strictEqual(counters.injections, 0, 'nothing is injected on a digest mismatch');
});

test('W6-006/T-193: a size mismatch fails closed and keeps the previous archive', async () => {
  const { h, api } = manualSetup();
  const { counters } = attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h, { ensure: { size: 99999 } });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'SIZE_MISMATCH');
  assert.strictEqual(oldTile.parentNode !== null, true);
  assert.strictEqual(counters.injections, 0);
});

test('W6-006/T-193: an attachment-registration timeout preserves the previous archive', async () => {
  const { h, api } = manualSetup();
  const { upload } = attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h);
  api.setManualArchiveBinding(project());
  upload._onFilesSet = () => { };
  const promise = api.manualArchiveZipAction();
  await h.settle();
  h.advance(31000);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'ATTACH_TIMEOUT');
  assert.strictEqual(oldTile.parentNode !== null, true);
  assert.strictEqual(manualControl(h).dataset.state, 'error');
});

test('W6-006/T-193: Bridge offline fails quickly and visibly, never via a file chooser or stale bytes', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h, { offline: true });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'BRIDGE_OFFLINE');
  assert.strictEqual(oldTile.parentNode !== null, true);
  assert.strictEqual(manualControl(h).dataset.state, 'error');
  assert.strictEqual(manualControl(h).textContent.endsWith('ZIP !'), true);
});

// ---------------------------------------------------------------------------
// TARGET B/N: conversation binding, isolation, migration, navigation
// ---------------------------------------------------------------------------

test('W6-006/T-193: two chats keep independent project bindings', () => {
  const { api } = manualSetup();
  api.setManualArchiveBinding(project());
  api.setManualArchiveBinding(otherProject(), 'c:other');
  assert.strictEqual(api.manualArchiveBindingFor('c:abc123').project_id, 'audapack');
  assert.strictEqual(api.manualArchiveBindingFor('c:other').project_id, 'fastprompter');
  const stored = api.readManualArchiveBindings();
  assert.strictEqual(stored['c:abc123'].project_id, 'audapack');
  assert.strictEqual(stored['c:other'].project_id, 'fastprompter');
});

test('W6-006/T-193: SPA navigation restores the correct conversation binding and control label', () => {
  const { h, api } = manualSetup();
  api.setManualArchiveBinding(project());
  api.setManualArchiveBinding(otherProject(), 'c:second');

  h.location.pathname = '/c/second';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'fastprompter');
  assert.strictEqual(manualControl(h).textContent, 'FastPrompter · ZIP');

  h.location.pathname = '/c/abc123';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack');
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · ZIP');
});

test('W6-006/T-193: a new chat stays unbound and its selection migrates to the stable key after the first send', async () => {
  const { h, api } = manualSetup();
  h.location.pathname = '/';
  api.bindAutoRuntimeToCurrentConversation();
  const draftKey = api.currentConversationKey();
  assert.ok(String(draftKey).startsWith('draft:'));
  assert.strictEqual(api.manualArchiveBindingFor(), null, 'a new chat is unbound');
  api.setManualArchiveBinding(otherProject(), draftKey);

  h.location.pathname = '/c/stable-after-send';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.manualArchiveBindingFor('c:stable-after-send').project_id, 'fastprompter');
  assert.strictEqual(api.manualArchiveBindingFor(draftKey), null, 'the temporary identity keeps no binding');
  assert.strictEqual(api.readManualArchiveBindings()[draftKey], undefined);
});

test('W6-006/T-193: a stale binding from another chat never leaks into the current conversation', () => {
  const { h, api } = manualSetup();
  api.setManualArchiveBinding(project(), 'c:chat-a');
  assert.strictEqual(api.manualArchiveBindingFor(), null, 'Chat B does not inherit Chat A binding');
  assert.strictEqual(manualControl(h).textContent, 'ZIP \u25be');
});

test('W6-006/T-193: navigating away while the archive is in flight attaches nothing to the new chat', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, { ensureDelay: 200 });
  api.setManualArchiveBinding(project());
  const promise = api.manualArchiveZipAction();
  h.location.pathname = '/c/somewhere-else';
  await drain(h);
  h.advance(31000);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'conversation_changed');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 0, 'Project A bytes never land in Chat B');
});

test('W6-006/T-193: two simultaneous fresh ChatGPT tabs independently bind different projects without cross-deletion', () => {
  const { api } = manualSetup();
  const tabADraft = 'draft:tab-alpha:draft-1';
  const tabBDraft = 'draft:tab-beta:draft-1';

  // Tab A binds Project A
  assert.strictEqual(api.setManualArchiveBinding(project(), tabADraft), true);
  // Tab B binds Project B
  assert.strictEqual(api.setManualArchiveBinding(otherProject(), tabBDraft), true);

  // Both survive independently
  assert.strictEqual(api.manualArchiveBindingFor(tabADraft).project_id, 'audapack');
  assert.strictEqual(api.manualArchiveBindingFor(tabBDraft).project_id, 'fastprompter');

  const stored = api.readManualArchiveBindings();
  assert.strictEqual(stored[tabADraft].project_id, 'audapack');
  assert.strictEqual(stored[tabBDraft].project_id, 'fastprompter');

  // Tab A migrates draft->stable
  assert.strictEqual(api.migrateManualArchiveBinding(tabADraft, 'c:conv-alpha'), true);
  assert.strictEqual(api.manualArchiveBindingFor('c:conv-alpha').project_id, 'audapack');
  assert.strictEqual(api.manualArchiveBindingFor(tabADraft), null);
  // Tab B draft binding survives Tab A migration
  assert.strictEqual(api.manualArchiveBindingFor(tabBDraft).project_id, 'fastprompter');

  // Tab B migrates draft->stable
  assert.strictEqual(api.migrateManualArchiveBinding(tabBDraft, 'c:conv-beta'), true);
  assert.strictEqual(api.manualArchiveBindingFor('c:conv-beta').project_id, 'fastprompter');
  assert.strictEqual(api.manualArchiveBindingFor(tabBDraft), null);
});

test('W6-006/T-193: binding in the same tab prunes that tab previous temporary key but preserves other tabs', () => {
  const { api } = manualSetup();
  const tabA1 = 'draft:tab-one:draft-1';
  const tabA2 = 'draft:tab-one:draft-2';
  const tabB = 'draft:tab-two:draft-1';
  const tabCAuth = 'auth:tab-three';

  api.setManualArchiveBinding(project(), tabA1);
  api.setManualArchiveBinding(otherProject(), tabB);
  api.setManualArchiveBinding(project(), tabCAuth);

  // Tab One updates to draft-2
  api.setManualArchiveBinding(otherProject(), tabA2);

  const stored = api.readManualArchiveBindings();
  assert.strictEqual(stored[tabA1], undefined, 'old draft on same tab pruned');
  assert.strictEqual(stored[tabA2].project_id, 'fastprompter', 'new draft on same tab saved');
  assert.strictEqual(stored[tabB].project_id, 'fastprompter', 'tab B draft preserved');
  assert.strictEqual(stored[tabCAuth].project_id, 'audapack', 'tab C auth preserved');
});

test('W6-006/T-193: cross-chat flight race: Chat A pauses in async resolution, navigates to Chat B, B runs same project; A does not join B flight and returns conversation_changed while B completes cleanly', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, {
    projects: [project()],
    projectsDelay: 100,
    ensureDelay: 100
  });

  h.location.pathname = '/c/chat-a';
  api.setManualArchiveBinding(project(), 'c:chat-a');
  api.setManualArchiveBinding(project(), 'c:chat-b');

  // Chat A starts ZIP (pauses in async project resolution)
  const promiseA = api.manualArchiveZipAction({ attachOnly: true });

  // Navigation to Chat B occurs while Chat A is resolving
  h.location.pathname = '/c/chat-b';
  api.bindAutoRuntimeToCurrentConversation();

  // Chat B starts ZIP for the same project
  const promiseB = api.manualArchiveZipAction({ attachOnly: true });

  await drain(h);
  h.advance(35000);

  const [resultA, resultB] = await Promise.all([promiseA, promiseB]);

  assert.strictEqual(resultA.ok, false);
  assert.strictEqual(resultA.errorCode, 'conversation_changed');
  assert.strictEqual(resultA.dedupe, undefined, 'Chat A must not join Chat B flight');

  assert.strictEqual(resultB.ok, true);
  assert.strictEqual(resultB.code, 'ATTACHED_NEW');
  assert.strictEqual(resultB.dedupe, undefined);

  // Chat B has the tile
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);

  // Navigate back to Chat A: verify no proof/status/runtime from B was projected into A
  h.location.pathname = '/c/chat-a';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack');
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · ZIP');
});

test('W6-006/T-193: cross-chat flight race (reverse ordering): Chat B pauses in resolution, navigates to Chat A, A runs same project; B returns conversation_changed while A completes cleanly', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, {
    projects: [project()],
    projectsDelay: 100,
    ensureDelay: 100
  });

  h.location.pathname = '/c/chat-b';
  api.setManualArchiveBinding(project(), 'c:chat-a');
  api.setManualArchiveBinding(project(), 'c:chat-b');

  // Chat B starts ZIP
  const promiseB = api.manualArchiveZipAction({ attachOnly: true });

  // Navigation to Chat A occurs while Chat B is resolving
  h.location.pathname = '/c/chat-a';
  api.bindAutoRuntimeToCurrentConversation();

  // Chat A starts ZIP for the same project
  const promiseA = api.manualArchiveZipAction({ attachOnly: true });

  await drain(h);
  h.advance(35000);

  const [resultB, resultA] = await Promise.all([promiseB, promiseA]);

  assert.strictEqual(resultB.ok, false);
  assert.strictEqual(resultB.errorCode, 'conversation_changed');
  assert.strictEqual(resultB.dedupe, undefined, 'Chat B must not join Chat A flight');

  assert.strictEqual(resultA.ok, true);
  assert.strictEqual(resultA.code, 'ATTACHED_NEW');
  assert.strictEqual(resultA.dedupe, undefined);

  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);

  // Navigate back to Chat B: verify no proof/status/runtime from A was projected into B
  h.location.pathname = '/c/chat-b';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack');
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · ZIP');
});

// ---------------------------------------------------------------------------
// TARGET C: picker management -- change, clear, refresh, disabled projects
// ---------------------------------------------------------------------------

test('W6-006/T-193: clear returns the chat to UNBOUND and never touches project files', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api);
  assert.strictEqual(api.clearManualArchiveBinding(), true);
  assert.strictEqual(api.manualArchiveBindingFor(), null);
  assert.strictEqual(Object.keys(api.readManualArchiveBindings()).length, 0);
  assert.strictEqual(manualControl(h).dataset.state, 'unbound');
  assert.strictEqual(manualControl(h).textContent, 'ZIP \u25be');
  assert.strictEqual(archiveGets(h).length, 1, 'clearing performs no archive work');
});

test('W6-006/T-193: switching the bound project changes the next ZIP action', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api, { attachOnly: true });
  const aTile = composerTilesNamed(h, NEW_ARCHIVE)[0];

  installBridge(h, {
    projects: [project(), otherProject()],
    ensure: { project_id: 'fastprompter', display_name: 'FastPrompter', filename: OTHER_ARCHIVE, reused: false, packed: true }
  });
  api.clearManualArchiveProjectsCache();
  api.setManualArchiveBinding(otherProject());
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  const ensureUrls = ensureRequests(h).map(request => String(request.url));
  assert.strictEqual(ensureUrls.filter(url => url.includes('/fastprompter/')).length, 1);
  assert.strictEqual(aTile.parentNode !== null, true, 'the other project archive is unrelated and survives');
  assert.strictEqual(composerTilesNamed(h, OTHER_ARCHIVE).length, 1);
});

test('W6-006/T-193: explicit selection resolves ambiguous attached archives without removing the other project', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  const otherTile = addComposerAttachmentTile(h, OTHER_ARCHIVE);
  installBridge(h, { projects: [project(), otherProject()] });
  api.setManualArchiveBinding(project());
  const result = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(otherTile.parentNode !== null, true, 'the unrelated project archive survives');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
});

// P1 TARGET B: the bound ZIP path no longer reads the project registry, so a
// project that has become unusable is reported by the ARCHIVE ENSURE endpoint --
// the authority the click has to consult anyway. This replaces the old
// expectation that a bound click ran `/v1/projects` first and failed with
// `project_not_registered` before ever reaching the Bridge's archive authority.
// The binding is still never silently erased: it is marked stale, the exact
// ensure error is surfaced, and the picker is offered.
test('W6-006/P1 TARGET B: a bound project the Bridge rejects is marked stale with the exact error and the picker is offered', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api);
  assert.strictEqual(projectsGets(h).length, 0, 'the normal bound ZIP click never reads the project registry');

  // A refresh that no longer lists the project must not corrupt the binding.
  installBridge(h, { projects: [otherProject()] });
  await runPicker(h, api, { force: true });
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack', 'the binding survives a refresh');
  const readsAfterRefresh = projectsGets(h).length;

  installBridge(h, { projects: [otherProject()], ensureStatus: 404, ensureErrorCode: 'unknown_project' });
  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'unknown_project', 'the exact Bridge error is surfaced');
  assert.strictEqual(api.manualArchiveStaleMarker().code, 'unknown_project', 'the binding is marked stale');
  assert.strictEqual(manualControl(h).dataset.stale, 'true');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');
  assert.strictEqual(menu.hidden, false, 'the project picker is offered');
  assert.strictEqual(projectsGets(h).length, readsAfterRefresh + 1, 'only the offered picker reads the registry');
  assert.strictEqual(api.manualArchiveBindingFor().project_id, 'audapack', 'a failed ensure does not erase the binding');
  assert.deepStrictEqual(menuItems(h).map(item => item.getAttribute('data-project-id')), ['fastprompter']);

  // Choosing again is what clears the stale marker.
  installBridge(h, { projects: [project(), otherProject()] });
  api.setManualArchiveBinding(project());
  assert.strictEqual(api.manualArchiveStaleMarker(), null, 'an explicit choice retires the stale marker');
  assert.strictEqual(manualControl(h).dataset.stale, 'false');
});

test('W6-006/T-193: the picker shows no cached result for a disabled project and refresh re-reads the registry', async () => {
  const { h, api } = manualSetup();
  installBridge(h, { projects: [otherProject()] });
  await runPicker(h, api, {});
  assert.deepStrictEqual(menuItems(h).map(item => item.getAttribute('data-project-id')), ['fastprompter']);

  installBridge(h, { projects: [project(), otherProject()] });
  await runPicker(h, api, { force: true });
  assert.strictEqual(projectsGets(h).length, 2, 'Refresh list bypasses the TTL cache');
  assert.deepStrictEqual(menuItems(h).map(item => item.getAttribute('data-project-id')), ['audapack', 'fastprompter']);
});

// ---------------------------------------------------------------------------
// Observability: bounded internal result codes
// ---------------------------------------------------------------------------

test('W6-006/T-193: the internal result codes stay observable without leaking into the compact UI', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  const unbound = await runManual(h, api);
  assert.strictEqual(unbound.code, 'UNBOUND');

  api.setManualArchiveBinding(project());
  const attached = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(attached.code, 'ATTACHED_NEW');
  assert.strictEqual(attached.ensureCode, 'PACKED_NEW');
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · ZIP \u2713');

  installBridge(h, { ensure: { reused: true, packed: false } });
  const sent = await runManual(h, api);
  assert.strictEqual(sent.code, 'SENT');
  assert.strictEqual(sent.ensureCode, 'REUSED_EXISTING');
  assert.strictEqual(sendClicks(h), 1);
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · SENT \u2713');

  // TARGET N: the identical payload inside the receipt window is a no-op.
  const repeat = await runManual(h, api);
  assert.strictEqual(repeat.code, 'ALREADY_SENT');
  assert.strictEqual(sendClicks(h), 1);
  assert.strictEqual(manualControl(h).textContent, 'AUDAPACK · ALREADY \u2713');
});

test('W6-006/P1: unrelated attachments at transaction start are intentional payload, not collateral', async () => {
  const { h, api } = manualSetup();
  const { sendRecorder } = attachComposer(h);
  const reportTile = addComposerAttachmentTile(h, 'report.pdf');
  const promptTile = addComposerAttachmentTile(h, 'AUDIT_CORE_ABC123.md');
  installBridge(h);
  api.setManualArchiveBinding(project());
  await runManual(h, api);
  // The widget never deleted them: they were still in the composer at the
  // moment it submitted the message, exactly as the operator left them.
  const recorded = sendRecorder.at(-1);
  assert.ok(recorded.tiles.includes('report.pdf'), 'the unrelated attachment was submitted with the payload');
  assert.ok(recorded.tiles.includes('AUDIT_CORE_ABC123.md'));
  assert.strictEqual(reportTile.parentNode, null, 'ChatGPT consumed the composer on a verified Send');
  assert.strictEqual(promptTile.parentNode, null);
});

// ---------------------------------------------------------------------------
// P1 REGRESSION / USABILITY: Responsive toolbar, disambiguation & menu popover
// ---------------------------------------------------------------------------

test('W6-006/P1: long project names are disambiguated with clean tail suffix in compact button', () => {
  const { api } = manualSetup();
  assert.strictEqual(
    api.disambiguateProjectName('_SMART_VAC_DUPLICATE_REMOVER'),
    '…DUPLICATE_REMOVER'
  );
  assert.strictEqual(
    api.disambiguateProjectName('_SMART_VAC_MEDIA_COMPRESSOR'),
    '…MEDIA_COMPRESSOR'
  );
  assert.strictEqual(
    api.disambiguateProjectName('AUDAPACK'),
    'AUDAPACK'
  );
  assert.strictEqual(
    api.disambiguateProjectName('FastPrompter'),
    'FastPrompter'
  );

  assert.strictEqual(
    api.manualArchiveControlLabel('idle', '_SMART_VAC_DUPLICATE_REMOVER'),
    '…DUPLICATE_REMOVER · ZIP'
  );
  assert.strictEqual(
    api.manualArchiveControlLabel('ready', '_SMART_VAC_MEDIA_COMPRESSOR'),
    '…MEDIA_COMPRESSOR · ZIP \u2713'
  );
});

test('W6-006/P1: opening and closing the picker manages panel data-menu-open and popover containment', async () => {
  const { h, api } = manualSetup();
  const panel = h.dom.querySelector('#acb-popup');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');
  assert.strictEqual(menu.hidden, true);
  assert.strictEqual(panel.dataset.menuOpen, undefined);

  api.openManualArchivePicker();
  assert.strictEqual(menu.hidden, false);
  assert.strictEqual(panel.dataset.menuOpen, 'true');

  api.closeManualArchivePicker();
  assert.strictEqual(menu.hidden, true);
  assert.strictEqual(panel.dataset.menuOpen, 'false');
});

test('W6-006/P1: Escape key dismisses the open project picker menu', () => {
  const { h, api } = manualSetup();
  const panel = h.dom.querySelector('#acb-popup');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');

  api.openManualArchivePicker();
  assert.strictEqual(menu.hidden, false);
  assert.strictEqual(panel.dataset.menuOpen, 'true');

  const escEvent = new FakeEvent('keydown');
  escEvent.key = 'Escape';
  h.dom.dispatchEvent(escEvent);

  assert.strictEqual(menu.hidden, true);
  assert.strictEqual(panel.dataset.menuOpen, 'false');
});

test('W6-006/P1: pointerdown outside the picker dismisses the open menu', () => {
  const { h, api } = manualSetup();
  const panel = h.dom.querySelector('#acb-popup');
  const menu = h.dom.querySelector('#acb-manual-zip-menu');

  api.openManualArchivePicker();
  assert.strictEqual(menu.hidden, false);
  assert.strictEqual(panel.dataset.menuOpen, 'true');

  const outsideEvent = new FakeEvent('pointerdown');
  outsideEvent.target = h.dom.documentElement;
  h.dom.dispatchEvent(outsideEvent);

  assert.strictEqual(menu.hidden, true);
  assert.strictEqual(panel.dataset.menuOpen, 'false');
});

test('W6-006/P1: project items in picker menu have full descriptive title tooltips', async () => {
  const { h, api } = manualSetup();
  installBridge(h, {
    projects: [
      project({ project_id: 'smart_vac_duplicate_remover', display_name: '_SMART_VAC_DUPLICATE_REMOVER', audit_name: 'Smart VAC Duplicate Remover' })
    ]
  });
  api.openManualArchivePicker();
  await h.settle();
  const items = menuItems(h);
  assert.strictEqual(items.length, 1);
  assert.strictEqual(items[0].getAttribute('title'), '_SMART_VAC_DUPLICATE_REMOVER (Smart VAC Duplicate Remover)');
});

test('W6-006/P1: Shift-click on bound button toggles picker without triggering zip action', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h);
  api.setManualArchiveBinding(project());
  const menu = h.dom.querySelector('#acb-manual-zip-menu');
  assert.strictEqual(menu.hidden, true);

  const shiftClick = new FakeEvent('click');
  shiftClick.shiftKey = true;
  manualControl(h).dispatchEvent(shiftClick);
  await h.settle();

  assert.strictEqual(menu.hidden, false, 'Shift-click opened picker');
  assert.strictEqual(ensureRequests(h).length, 0, 'zero ensure requests dispatched');

  // Second Shift-click closes it
  manualControl(h).dispatchEvent(shiftClick);
  assert.strictEqual(menu.hidden, true, 'second Shift-click closed picker');
});

// ---------------------------------------------------------------------------
// P1 TARGET B-F: the project registry can never remain in LOADING
// ---------------------------------------------------------------------------

function menuNoteRows(h) {
  const list = h.dom.querySelector('#acb-manual-zip-menu-list');
  if (!list) return [];
  // Project choices carry data-project-id; everything else is a state note.
  return list.children.filter(child => child.getAttribute && !child.getAttribute('data-project-id'));
}

function menuNoteTexts(h) {
  return menuNoteRows(h).map(note => String(note.textContent || ''));
}

function menuProjects(h) {
  return menuItems(h).map(item => item.getAttribute('data-project-id'));
}

// A scripted registry responder: each `/v1/projects` read answers with the
// scripted entry for that call, so a test can model a slow first read and a
// fast second one without touching any other endpoint.
function registryScript(calls) {
  let index = 0;
  return request => {
    const url = String(request.url || '');
    if (request.method === 'GET' && /\/v1\/projects$/.test(url)) {
      const scripted = calls[Math.min(index, calls.length - 1)] || {};
      index += 1;
      if (scripted.stall) return { stall: true };
      if (scripted.error) return { error: true };
      if (scripted.status && scripted.status !== 200) {
        return {
          status: scripted.status,
          responseText: JSON.stringify({ ok: false, error: { code: scripted.errorCode || 'invalid_auth', message: 'denied' } })
        };
      }
      return {
        status: 200,
        responseText: JSON.stringify({ ok: true, projects: scripted.projects || [] }),
        delay: scripted.delay || 0
      };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
}

async function step(h, ms = 40) {
  await new Promise(resolve => setImmediate(resolve));
  h.advance(ms);
  await new Promise(resolve => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// T-229: a semantic ChatGPT composer remount after attachment registration is
// NOT a human edit. ChatGPT legitimately replaces #prompt-textarea and/or the
// whole composer shell when a file registers; the safety fence must protect
// SEMANTIC composer state (conversation, normalized text, unrelated attachment
// identities), never JavaScript object identity. The regression that escaped
// 0.0.62: manual ZIP attached the archive, Send never fired, sendClicks === 0.
// ---------------------------------------------------------------------------

// Live-DOM probes. A React remount replaces nodes, so these tests always read
// the CURRENT composer rather than a reference captured before the transition.
function liveForm(h) {
  return h.dom.documentElement.querySelector('form[data-type="unified-composer"]');
}
function liveInput(h) {
  const form = liveForm(h);
  return form ? form.querySelector('#prompt-textarea') : null;
}
function liveSend(h) {
  const form = liveForm(h);
  return form ? form.querySelector('button[data-testid="send-button"]') : null;
}
function liveSendClicks(h) {
  return Number(liveSend(h)?._clickCount || 0);
}

// A live-DOM send-acceptance model: the prepared composer (authored text and
// attachment tiles) leaves the composer when ChatGPT accepts the submission.
function installLiveAcceptance(h, recorded, accept) {
  liveSend(h).addEventListener('click', () => {
    const form = liveForm(h);
    const input = liveInput(h);
    const tiles = form ? form.children.filter(child => child.getAttribute && child.getAttribute('role') === 'group') : [];
    recorded.push({
      text: input ? String(input.textContent || '') : '',
      tiles: tiles.map(tile => tile.getAttribute('aria-label'))
    });
    if (!accept()) return;
    if (input) input.textContent = '';
    for (const tile of tiles.slice()) tile.remove();
  });
}

function mountLiveComposer(h, options = {}) {
  const accept = typeof options.accept === 'function'
    ? options.accept
    : () => options.accept !== false;
  const fixture = composerFixture(h);
  fixture.input.textContent = options.text || '';
  fixture.upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
    if (typeof options.onInject === 'function') options.onInject(files);
    h.mutate(liveForm(h));
  };
  const recorded = Array.isArray(options.recorded) ? options.recorded : [];
  installLiveAcceptance(h, recorded, accept);
  return { fixture, recorded };
}

// React remount of the composer INPUT only: the original #prompt-textarea is
// removed and an equivalent one created, preserving semantic text.
function remountComposerInput(h) {
  const form = liveForm(h);
  const previous = form.querySelector('#prompt-textarea');
  const replacement = h.el('div', {
    id: 'prompt-textarea',
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Chat with ChatGPT'
  });
  replacement.isContentEditable = true;
  replacement.textContent = previous ? String(previous.textContent || '') : '';
  form.replaceChild(replacement, previous);
  h.mutate(form);
  return replacement;
}

// React remount of the WHOLE composer: the entire form/shell is replaced while
// conversation, authored text and canonical archive identity are preserved.
function remountWholeComposer(h, options = {}) {
  const previous = liveForm(h);
  if (previous) previous.remove();
  return mountLiveComposer(h, options);
}

test('W6-006/T-229: manual ZIP survives a semantic ChatGPT composer input remount after attachment registration', async () => {
  const { h, api } = manualSetup();
  const { recorded } = mountLiveComposer(h, {
    text: USER_TEXT,
    onInject: () => remountComposerInput(h)
  });
  installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await runManual(h, api);
  assert.strictEqual(result.ok, true, 'a semantic remount must not cancel Send');
  assert.strictEqual(result.code, 'SENT');
  assert.strictEqual(result.errorCode, '');
  assert.strictEqual(liveSendClicks(h), 1, 'exactly one automatic Send');
  assert.deepStrictEqual(recorded.at(-1).tiles, [NEW_ARCHIVE],
    'the canonical archive was submitted from the live composer');
});

test('W6-006/T-229: manual ZIP survives a WHOLE composer remount after attachment registration', async () => {
  const { h, api } = manualSetup();
  const recorded = [];
  mountLiveComposer(h, { text: USER_TEXT, recorded });
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  // The whole shell is retired; an equivalent one takes its place carrying the
  // same authored text, and the archive is injected into the NEW composer. The
  // acceptance recorder is shared across the remount.
  remountWholeComposer(h, { text: USER_TEXT, recorded });

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, true, 'a whole-composer remount must not cancel Send');
  assert.strictEqual(result.code, 'SENT');
  assert.strictEqual(liveSendClicks(h), 1, 'exactly one automatic Send after the remount');
  assert.deepStrictEqual(recorded.at(-1).tiles, [NEW_ARCHIVE],
    'the archive registered in the live composer and was submitted');
});

test('W6-006/T-229: a remount plus a real text edit still cancels automatic Send', async () => {
  const { h, api } = manualSetup();
  mountLiveComposer(h, { text: 'original prompt' });
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  remountComposerInput(h);
  liveInput(h).textContent = 'edited while the ZIP was preparing';

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'COMPOSER_CHANGED_BEFORE_SEND');
  assert.strictEqual(result.errorCode, 'composer_changed');
  assert.strictEqual(liveSendClicks(h), 0, 'a real edit is never sent');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'the ready archive was kept');
});

test('W6-006/T-229: a remount plus a new unrelated attachment still cancels automatic Send', async () => {
  const { h, api } = manualSetup();
  mountLiveComposer(h);
  addComposerAttachmentTile(h, 'notes.pdf');
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  remountWholeComposer(h);
  addComposerAttachmentTile(h, 'notes.pdf');
  const added = addComposerAttachmentTile(h, 'other.pdf');

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'COMPOSER_CHANGED_BEFORE_SEND');
  assert.strictEqual(result.errorCode, 'attachment_changed');
  assert.strictEqual(liveSendClicks(h), 0);
  assert.strictEqual(added.parentNode !== null, true, 'the operator attachment is never discarded');
});

test('W6-006/T-229: an unrelated attachment remounted to an equivalent tile is not attachment_changed', async () => {
  const { h, api } = manualSetup();
  mountLiveComposer(h, { text: USER_TEXT });
  api.setManualArchiveBinding(project());
  const report = addComposerAttachmentTile(h, 'notes.pdf');
  const snapshot = api.captureManualArchiveComposerSnapshot(api.currentConversationKey(), { project: project() });
  assert.strictEqual(snapshot.attachments.includes('notes.pdf:9'), true,
    'the unrelated attachment carries a strong semantic identity');

  // React remounts notes.pdf as a NEW DOM element with the same identity.
  report.remove();
  const remounted = addComposerAttachmentTile(h, 'notes.pdf');
  assert.notStrictEqual(remounted, report, 'the DOM node is genuinely new');
  assert.strictEqual(api.manualArchiveSnapshotStaleReason(snapshot), '',
    'a strong identity match is not a human edit');

  // Control: the same snapshot must still detect a genuinely different file.
  addComposerAttachmentTile(h, 'other.pdf');
  assert.strictEqual(api.manualArchiveSnapshotStaleReason(snapshot), 'attachment_changed');
  assert.strictEqual(liveSendClicks(h), 0);
});

test('W6-006/T-229: a failed automatic Send is visible on the compact control (TARGET K)', async () => {
  const { h, api } = manualSetup();
  mountLiveComposer(h, { text: USER_TEXT });
  installBridge(h);
  api.setManualArchiveBinding(project());
  liveSend(h).disabled = true;

  const result = await runManual(h, api);
  assert.strictEqual(result.code, 'SEND_TIMEOUT');
  assert.strictEqual(manualControl(h).dataset.state, 'send_timeout');
  assert.ok(manualControl(h).textContent.includes('SEND !'), 'the failed Send is visible');

  // Attach-only remains a distinct, non-send completion.
  liveSend(h).disabled = false;
  const attachOnly = await runManual(h, api, { attachOnly: true });
  assert.strictEqual(attachOnly.code, 'ALREADY_ATTACHED');
  assert.strictEqual(liveSendClicks(h), 0, 'attach-only performs zero Send');
  assert.strictEqual(manualControl(h).dataset.state, 'ready');
});

test('W6-006/P1: a successful registry read renders the projects and leaves LOADING', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project(), otherProject()] }]);
  const pending = api.openManualArchivePicker();
  await h.settle();
  await pending;

  const state = api.manualArchiveMenuState;
  assert.strictEqual(state.loading, false, 'the picker left LOADING');
  assert.strictEqual(state.error, '');
  assert.deepStrictEqual(menuProjects(h), ['audapack', 'fastprompter']);
  assert.strictEqual(menuNoteTexts(h).includes('Loading projects...'), false);

  // Bounded diagnostics: enough to debug a stuck picker, never a secret.
  const debug = api.manualArchiveDebug;
  assert.strictEqual(debug.generation > 0, true);
  assert.strictEqual(debug.status, 200, 'the real HTTP status is captured');
  assert.strictEqual(debug.ok, true);
  assert.strictEqual(debug.pickerOpenAtCompletion, true);
  assert.strictEqual(debug.elapsedMs < api.constants_MANUAL_ARCHIVE_PICKER_DEADLINE_MS, true);
  assert.strictEqual(String(debug.conversationKey).includes('abc123'), false, 'the conversation key is short-hashed');
  assert.strictEqual(JSON.stringify(debug).includes(TOKEN), false, 'diagnostics never carry the Bridge token');
  assert.strictEqual(JSON.stringify(debug).includes('X-ACB-Token'), false);
});

test('W6-006/P1: with no cache an offline Bridge leaves LOADING inside the hard deadline', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ error: true }]);
  const pending = api.openManualArchivePicker();
  await h.settle();
  await pending;

  assert.strictEqual(api.manualArchiveMenuState.loading, false);
  assert.strictEqual(api.manualArchiveMenuState.error, 'Bridge offline');
  assert.deepStrictEqual(menuNoteTexts(h), ['Bridge offline']);
  assert.strictEqual(h.dom.querySelector('#acb-manual-zip-refresh').disabled, false, 'Refresh list stays usable');
});

test('W6-006/P1: a stalled registry request is bounded by the widget hard deadline', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ stall: true }]);
  const pending = api.openManualArchivePicker();
  await step(h, 50);

  assert.strictEqual(api.manualArchiveMenuState.loading, true, 'genuinely loading only until the deadline');
  assert.strictEqual(menuNoteTexts(h).includes('Loading projects...'), true);
  assert.strictEqual(api.manualArchiveRegistryDeadlineActive, true);

  h.advance(api.constants_MANUAL_ARCHIVE_REGISTRY_DEADLINE_MS + 50);
  await h.settle();
  await pending;

  const state = api.manualArchiveMenuState;
  assert.strictEqual(state.loading, false, 'the hard deadline ended LOADING');
  assert.strictEqual(state.error, 'Registry timed out');
  assert.strictEqual(state.deadlined, true);
  assert.strictEqual(api.manualArchiveDebug.errorCode, 'registry-deadline');
  // The harness runs timers on a fake clock while elapsedMs is wall-clock, so
  // the bound is proven by the deadline firing (deadlined === true), not by
  // comparing the two clocks.
  assert.strictEqual(Number.isFinite(api.manualArchiveDebug.elapsedMs) && api.manualArchiveDebug.elapsedMs >= 0, true);
  assert.strictEqual(api.manualArchiveRegistryInFlight, false, 'the deadline released the single-flight slot');
});

test('W6-006/P1: an authorization failure leaves LOADING immediately with the real error', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ status: 401, errorCode: 'invalid_auth' }]);
  const pending = api.openManualArchivePicker();
  await h.settle();
  await pending;

  assert.strictEqual(api.manualArchiveMenuState.loading, false);
  assert.strictEqual(api.manualArchiveMenuState.error, 'Bridge authorization failed');
  assert.strictEqual(api.manualArchiveMenuState.errorCode, 'invalid_auth');
  assert.strictEqual(api.manualArchiveDebug.elapsedMs < api.constants_MANUAL_ARCHIVE_PICKER_DEADLINE_MS, true);
});

test('W6-006/P1: a usable cache is shown immediately while the refresh is still in flight', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()] }, { projects: [project(), otherProject()], delay: 3000 }]);

  await runPicker(h, api, {});
  assert.deepStrictEqual(menuProjects(h), ['audapack']);

  const pending = api.openManualArchivePicker({ force: true });
  await step(h, 50);

  assert.deepStrictEqual(menuProjects(h), ['audapack'], 'the cached list is never replaced by a blank spinner');
  assert.strictEqual(menuNoteTexts(h).includes('Loading projects...'), false);
  assert.strictEqual(menuNoteTexts(h).includes('Refreshing...'), true);
  assert.strictEqual(api.manualArchiveMenuState.loading, false);

  await drain(h);
  await pending;
  assert.deepStrictEqual(menuProjects(h), ['audapack', 'fastprompter'], 'the fresh result replaces the cache');
});

test('W6-006/P1: a failed refresh keeps the cached list usable and reports the failure', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()] }, { error: true }]);

  await runPicker(h, api, {});
  await runPicker(h, api, { force: true });

  assert.deepStrictEqual(menuProjects(h), ['audapack'], 'the cached projects remain usable');
  assert.strictEqual(menuNoteTexts(h).includes('Bridge offline'), true);
  assert.strictEqual(api.manualArchiveMenuState.loading, false);
});

test('W6-006/P1: a superseded registry generation cannot overwrite the newer result', async () => {
  const { h, api } = manualSetup();
  // Generation 1 stalls (the real /v1/projects read that never came back),
  // the widget hard deadline retires it, then a refresh opens generation 2
  // which answers normally. When generation 1 finally answers, it must not
  // overwrite what the operator is already looking at.
  h.httpResponder = registryScript([
    { projects: [project()], delay: 20000 },
    { projects: [project(), otherProject()] }
  ]);

  const pending = api.openManualArchivePicker();
  await step(h, 50);
  const firstGeneration = api.manualArchiveRegistryGeneration;
  h.advance(api.constants_MANUAL_ARCHIVE_REGISTRY_DEADLINE_MS + 50);
  await h.settle();
  await pending;
  assert.strictEqual(api.manualArchiveMenuState.error, 'Registry timed out');

  api.openManualArchivePicker({ force: true });
  await drain(h);

  assert.strictEqual(api.manualArchiveRegistryGeneration > firstGeneration, true, 'a refresh owns a new generation');
  assert.deepStrictEqual(menuProjects(h), ['audapack', 'fastprompter'], 'the newer result owns the picker');

  // The dead generation-1 response lands now: stale, and ignored by the UI.
  h.advance(25000);
  await h.settle();
  assert.deepStrictEqual(menuProjects(h), ['audapack', 'fastprompter'], 'the stale response cannot overwrite the newer result');
  assert.strictEqual(api.manualArchiveDebug.staleGeneration, firstGeneration, 'the late response is recorded as stale');
  assert.strictEqual(api.manualArchiveMenuState.loading, false);
});

test('W6-006/P1: closing and reopening the picker never strands the loading state', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()], delay: 3000 }]);

  const first = api.openManualArchivePicker();
  await step(h, 40);
  api.closeManualArchivePicker();
  assert.strictEqual(api.manualArchiveMenuState.loading, false, 'closing clears loading');

  const second = api.openManualArchivePicker();
  await drain(h);
  await Promise.all([first, second]);

  assert.strictEqual(api.manualArchiveMenuState.loading, false);
  assert.deepStrictEqual(menuProjects(h), ['audapack']);
});

test('W6-006/P1: repeated picker opens coalesce into one registry read', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()], delay: 500 }]);

  const opened = [api.openManualArchivePicker(), api.openManualArchivePicker(), api.openManualArchivePicker()];
  await drain(h);
  await Promise.all(opened);

  assert.strictEqual(projectsGets(h).length, 1, 'one registry read for repeated opens');
  assert.deepStrictEqual(menuProjects(h), ['audapack']);
});

test('W6-006/P1: repeated Refresh never starts parallel registry reads', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()], delay: 500 }]);

  const refreshes = [];
  for (let i = 0; i < 5; i += 1) refreshes.push(api.manualArchiveRefreshProjects());
  await drain(h);
  await Promise.all(refreshes);

  assert.strictEqual(projectsGets(h).length, 1, 'one in-flight registry read is reused');
  assert.deepStrictEqual(menuProjects(h), ['audapack']);
});

test('W6-006/P1: a late response after the deadline never resurrects LOADING', async () => {
  const { h, api } = manualSetup();
  h.httpResponder = registryScript([{ projects: [project()], delay: 20000 }]);

  const pending = api.openManualArchivePicker();
  await h.settle();
  h.advance(api.constants_MANUAL_ARCHIVE_REGISTRY_DEADLINE_MS + 50);
  await h.settle();
  await pending;
  assert.strictEqual(api.manualArchiveMenuState.error, 'Registry timed out');

  const reads = projectsGets(h).length;
  h.advance(25000);
  await h.settle();

  assert.strictEqual(api.manualArchiveMenuState.loading, false, 'a late result can never resurrect LOADING');
  assert.strictEqual(menuNoteTexts(h).includes('Loading projects...'), false);
  assert.strictEqual(projectsGets(h).length, reads, 'no extra registry read is dispatched');
});

// ---------------------------------------------------------------------------
// P1 TARGET G-T: attach + verified auto-send
// ---------------------------------------------------------------------------

test('W6-006/P1: Send happens only after the attachment tile is genuinely registered', async () => {
  const { h, api } = manualSetup();
  const { upload, form, sendRecorder } = attachComposer(h);
  // ChatGPT takes a moment to register the uploaded file as a ready tile.
  upload._onFilesSet = (element, files) => {
    for (const file of files) {
      h.timers.setTimeout(() => {
        addComposerAttachmentTile(h, file.name);
        h.mutate(form);
      }, 400);
    }
  };
  installBridge(h);
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  let observedRegisteringWindow = false;
  for (let i = 0; i < 60; i += 1) {
    await step(h, 30);
    if (sendClicks(h)) break;
    // While the upload is still registering there is no ready tile and the
    // widget must not have clicked Send yet.
    if (!composerTilesNamed(h, NEW_ARCHIVE).length) observedRegisteringWindow = true;
    assert.strictEqual(sendClicks(h), 0, 'never Send while the attachment is still registering');
  }

  await drain(h);
  const result = await promise;
  assert.strictEqual(observedRegisteringWindow, true, 'the test really observed the registering window');
  assert.strictEqual(result.code, 'SENT');
  assert.strictEqual(sendClicks(h), 1);
  assert.deepStrictEqual(sendRecorder.at(-1).tiles, [NEW_ARCHIVE],
    'the attachment was already registered when Send was clicked');
});

test('W6-006/P1: editing the composer while the archive is in flight cancels automatic Send', async () => {
  const { h, api } = manualSetup();
  const { input } = attachComposer(h);
  input.textContent = 'original prompt';
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  assert.strictEqual(ensureRequests(h).length, 1);
  input.textContent = 'edited while the ZIP was preparing';

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'COMPOSER_CHANGED_BEFORE_SEND');
  assert.strictEqual(result.errorCode, 'composer_changed');
  assert.strictEqual(sendClicks(h), 0, 'the widget never sends its stale assumption');
  assert.strictEqual(api.composerPlainText(input), 'edited while the ZIP was preparing', 'the operator edit survives');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'the ready archive was kept');
});

test('W6-006/P1: adding an attachment while the archive is in flight cancels automatic Send', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  const added = addComposerAttachmentTile(h, 'unrelated-notes.pdf');

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'COMPOSER_CHANGED_BEFORE_SEND');
  assert.strictEqual(result.errorCode, 'attachment_changed');
  assert.strictEqual(sendClicks(h), 0);
  assert.strictEqual(added.parentNode !== null, true, 'the operator attachment is never discarded');
});

test('W6-006/P1: navigating away before Send cancels automatic Send in the other chat', async () => {
  const { h, api } = manualSetup();
  attachComposer(h);
  installBridge(h, { ensureDelay: 600 });
  api.setManualArchiveBinding(project());
  const originKey = api.currentConversationKey();

  const promise = api.manualArchiveZipAction();
  for (let i = 0; i < 12 && ensureRequests(h).length === 0; i += 1) await step(h, 20);
  h.location.pathname = '/c/other-chat';

  await drain(h);
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'conversation_changed');
  assert.strictEqual(sendClicks(h), 0, 'Chat A never sends in Chat B');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 0, 'Project A bytes never land in the other chat');

  h.location.pathname = '/c/abc123';
  api.bindAutoRuntimeToCurrentConversation();
  assert.strictEqual(api.currentConversationKey(), originKey);
});

test('W6-006/P1: an unavailable Send control is a bounded SEND_TIMEOUT that keeps the archive attached', async () => {
  const { h, api } = manualSetup();
  const { input, send } = attachComposer(h);
  input.textContent = USER_TEXT;
  installBridge(h);
  api.setManualArchiveBinding(project());
  send.disabled = true;

  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'SEND_TIMEOUT');
  assert.strictEqual(result.errorCode, 'send-timeout');
  assert.strictEqual(sendClicks(h), 0);
  assert.strictEqual(api.manualArchiveSentStore().entries.length, 0, 'no receipt for a bounded timeout');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'the canonical archive stays attached');
  assert.strictEqual(api.composerPlainText(input), USER_TEXT, 'the payload stays in the composer');

  // The next ZIP click retries ONLY the Send stage: no re-upload, no injection.
  send.disabled = false;
  const getsBefore = archiveGets(h).length;
  const retry = await runManual(h, api);
  assert.strictEqual(retry.ok, true);
  assert.strictEqual(retry.code, 'SENT');
  assert.strictEqual(archiveGets(h).length, getsBefore, 'a Send-only retry performs zero GET');
  assert.strictEqual(retry.injectionCount, 0, 'a Send-only retry injects nothing');
  assert.strictEqual(retry.ensureCode, 'REUSED_EXISTING', 'the retry is a metadata no-op, not a repack');
  assert.strictEqual(sendClicks(h), 1);
});

test('W6-006/P1: only a positively verified Send writes a bounded receipt without payload text', async () => {
  const { h, api } = manualSetup();
  const { input } = attachComposer(h);
  input.textContent = USER_TEXT;
  const sha = installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await runManual(h, api);
  assert.strictEqual(result.code, 'SENT');

  const store = api.manualArchiveSentStore();
  assert.strictEqual(store.entries.length, 1, 'a verified Send is receipted');
  const entry = store.entries[0];
  assert.strictEqual(entry.project_id, 'audapack');
  assert.strictEqual(entry.sha256, sha);
  assert.strictEqual(entry.key, api.currentConversationKey());
  assert.ok(Number(entry.at) > 0);
  assert.strictEqual(store.entries.length <= api.constants_MANUAL_ARCHIVE_SENT_MAX, true, 'the receipt store is bounded');
  const serialized = JSON.stringify(store);
  assert.strictEqual(serialized.includes(USER_TEXT), false, 'the receipt never stores the composer text');
  assert.strictEqual(serialized.includes('X-ACB-Token') || serialized.includes(TOKEN), false, 'the receipt never stores the token');
});

test('W6-006/P1: an unverified Send leaves the payload in place and writes no receipt', async () => {
  const { h, api } = manualSetup();
  let chatGptAccepts = false;
  const { input } = attachComposer(h, { accept: () => chatGptAccepts });
  input.textContent = USER_TEXT;
  installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await runManual(h, api);
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.code, 'SEND_PENDING');
  assert.strictEqual(result.errorCode, 'send-unverified');
  assert.strictEqual(sendClicks(h), 1, 'the click happened');
  assert.strictEqual(api.manualArchiveSentStore().entries.length, 0, 'no success receipt without positive acceptance');
  assert.strictEqual(api.composerPlainText(input), USER_TEXT, 'the unsubmitted payload survives');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'the ready archive stays attached');

  // TARGET P: an uncertain Send poisons nothing, so the identical retry is
  // still allowed to send -- and once it is genuinely accepted, it receipted.
  chatGptAccepts = true;
  const retry = await runManual(h, api);
  assert.strictEqual(retry.code, 'SENT');
  assert.strictEqual(retry.injectionCount, 0, 'the retry reuses the attached archive');
  assert.strictEqual(sendClicks(h), 2);
  assert.strictEqual(api.manualArchiveSentStore().entries.length, 1, 'the accepted retry writes the receipt');
});

test('W6-006/P1: a changed canonical SHA defeats ALREADY_SENT and is sent as a fresh generation', async () => {
  const { h, api } = manualSetup();
  const { input } = attachComposer(h);
  input.textContent = USER_TEXT;
  installBridge(h);
  api.setManualArchiveBinding(project());

  const first = await runManual(h, api);
  assert.strictEqual(first.code, 'SENT');

  // The project source changed: the canonical digest changes with it.
  const bytesB = Buffer.from('PK\u0003\u0004 a changed source archive for the resend path');
  const shaB = nodeCrypto.createHash('sha256').update(bytesB).digest('hex');
  installBridge(h, { bytes: bytesB, ensure: { sha256: shaB, reused: false, packed: true } });

  // Same conversation, no authored payload, but a NEW canonical generation:
  // the receipt for shaA must not swallow the changed source.
  const second = await runManual(h, api);
  assert.strictEqual(second.ok, true);
  assert.strictEqual(second.code, 'SENT', 'content identity beats the stale receipt');
  assert.strictEqual(second.ensureCode, 'PACKED_NEW');
  assert.strictEqual(second.meta.sha256, shaB);
  assert.strictEqual(archiveGets(h).length, 2, 'the new generation was downloaded');
  assert.strictEqual(sendClicks(h), 2, 'the changed source is sent as a fresh generation');
});

test('W6-006/P1: the whole manual transaction records bounded evidence and no secrets', async () => {
  const { h, api } = manualSetup();
  const { input } = attachComposer(h);
  input.textContent = USER_TEXT;
  const sha = installBridge(h);
  api.setManualArchiveBinding(project());

  const result = await runManual(h, api);
  assert.strictEqual(result.code, 'SENT');

  const log = api.manualArchiveLogSnapshot();
  const record = log[log.length - 1];
  assert.strictEqual(record.projectId, 'audapack');
  assert.strictEqual(record.operationGeneration > 0, true);
  assert.strictEqual(record.getCount, 1);
  assert.strictEqual(record.injectionCount, 1);
  assert.strictEqual(record.sendAttempts, 1);
  assert.strictEqual(record.sendAccepted, true);
  assert.strictEqual(record.resultCode, 'SENT');
  assert.strictEqual(record.shaPrefix, sha.slice(0, 12));
  const serialized = JSON.stringify(log);
  assert.strictEqual(serialized.includes(USER_TEXT), false, 'the log never stores composer text');
  assert.strictEqual(serialized.includes(TOKEN), false, 'the log never stores the Bridge token');
  assert.strictEqual(log.length <= api.constants_MANUAL_ARCHIVE_LOG_MAX, true, 'the evidence log is bounded');
});

