'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const nodeCrypto = require('node:crypto');
const {
  setup,
  mainEl,
  composerFixture,
  addComposerAttachmentTile,
  runtimeFixture,
  FakeEvent
} = require('./helpers');
const { FakeFile, FakeDataTransfer } = require('./harness');

const TOKEN = 'w6-001-test-token';
const NEW_ARCHIVE = '_AUDAPACK_09.09.26-T01-18-12.zip';
const OLD_ARCHIVE = '_AUDAPACK_01.01.26-T00-00-00.zip';
const USER_TEXT = 'Пусть клауд тоже будет виден';

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

function installBridge(h, options = {}) {
  const bytes = Buffer.isBuffer(options.bytes) ? options.bytes : Buffer.from('PK\u0003\u0004 canonical archive bytes');
  const sha = nodeCrypto.createHash('sha256').update(bytes).digest('hex');
  const projects = options.projects || [project()];
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
    if (request.method === 'GET' && /\/v1\/projects$/.test(url)) {
      return { status: 200, responseText: JSON.stringify({ ok: true, projects }) };
    }
    if (request.method === 'POST' && /\/archive\/ensure$/.test(url)) {
      const projectId = projectIdFrom(url);
      let payload = ensure;
      if (options.ensureByProject && options.ensureByProject[projectId]) {
        const registered = projects.find(item => item.project_id === projectId) || {};
        payload = {
          project_id: projectId,
          display_name: registered.display_name || projectId,
          filename: NEW_ARCHIVE,
          size: bytes.length,
          mtime: Math.floor(Date.now() / 1000),
          sha256: sha,
          reused: false,
          packed: true,
          ...options.ensureByProject[projectId]
        };
      }
      return { status: 200, responseText: JSON.stringify({ ok: true, ...payload }) };
    }
    if (request.method === 'GET' && /\/archive$/.test(url)) {
      if (archiveStatus !== 200) {
        return { status: archiveStatus, responseText: JSON.stringify({ ok: false, error: { code: 'archive_unavailable', message: 'no archive' } }) };
      }
      return { status: 200, response: new Uint8Array(bytes) };
    }
    return { status: 404, responseText: JSON.stringify({ ok: false, error: { code: 'not_found', message: 'nope' } }) };
  };
  return sha;
}

function armRuntime(api, overrides = {}) {
  api.autoRuntime = runtimeFixture({ projectName: 'AUDAPACK', stage: 'idle', enabled: false, ...overrides });
  api.storage.gmSet('ai_chatbuttons_bridge_token_v1', TOKEN);
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

function archiveFile(name) {
  return new FakeFile([Buffer.from('zip')], name, { type: 'application/zip', lastModified: Date.now() });
}

function composerTilesNamed(h, name) {
  return h.dom.querySelectorAll('[role="group"]').filter(element => element.getAttribute('aria-label') === name);
}

// ---------------------------------------------------------------------------
// T-180G: ChatGPT drop sanitizer
// ---------------------------------------------------------------------------

test('W6-001: canonical AUDAPACK archive names are recognized, other files are not', () => {
  const { api } = setup();
  assert.strictEqual(api.isAudapackProjectArchiveName(NEW_ARCHIVE), true);
  assert.strictEqual(api.isAudapackProjectArchiveName('_AUDAPACK_PROJECT.zip'), true);
  assert.strictEqual(api.isAudapackProjectArchiveName('TERMISAI_27.08.26-T06-28-02.zip'), true);
  assert.strictEqual(api.isAudapackProjectArchiveName('photo.png'), false);
  assert.strictEqual(api.isAudapackProjectArchiveName('report.zip'), false);
});

test('W6-001: dropping a project ZIP inserts the file once and never the file:/// URI text', () => {
  const { h, api } = setup();
  const { form, input, upload } = composerFixture(h);
  input.textContent = USER_TEXT;
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const event = dispatchDrop(h, input, {
    files: [archiveFile(NEW_ARCHIVE)],
    uri: `file:///V:/___VAC/__K/__CODE/_PY/_AUDAPACK/${NEW_ARCHIVE}`
  });

  assert.strictEqual(api.lastArchiveDropResult.action, 'sanitized');
  assert.strictEqual(api.lastArchiveDropResult.reason, 'archive-attached');
  assert.strictEqual(event.defaultPrevented, true, 'the editable-text drop must be cancelled');
  assert.strictEqual(upload.files.length, 1);
  assert.strictEqual(upload.files[0].name, NEW_ARCHIVE);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'one physical drop -> one attachment');
  assert.strictEqual(api.composerPlainText(input), USER_TEXT, 'composer text must stay byte-for-byte identical');
  assert.ok(!api.composerPlainText(input).includes('file:///'));
  assert.ok(!api.composerPlainText(input).includes(NEW_ARCHIVE));
});

test('W6-001: ordinary dragged text is not intercepted', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  input.textContent = USER_TEXT;
  const event = dispatchDrop(h, input, { plain: 'just some dragged words' });
  assert.strictEqual(api.lastArchiveDropResult.action, 'ignored');
  assert.strictEqual(event.defaultPrevented, false);
});

test('W6-001: a dropped web URL is not intercepted', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const event = dispatchDrop(h, input, { uri: 'https://example.com/some/page' });
  assert.strictEqual(api.lastArchiveDropResult.action, 'ignored');
  assert.strictEqual(event.defaultPrevented, false);
});

test('W6-001: an unrelated dropped file is not intercepted', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const event = dispatchDrop(h, input, {
    files: [new FakeFile([Buffer.from('png')], 'photo.png', { type: 'image/png' })],
    uri: 'file:///V:/pictures/photo.png'
  });
  assert.strictEqual(api.lastArchiveDropResult.action, 'ignored');
  assert.strictEqual(api.lastArchiveDropResult.reason, 'not-project-archive');
  assert.strictEqual(event.defaultPrevented, false);
});

test('W6-001: a drop outside the composer is not intercepted', () => {
  const { h, api } = setup();
  composerFixture(h);
  const event = dispatchDrop(h, mainEl(h), {
    files: [archiveFile(NEW_ARCHIVE)],
    uri: `file:///V:/x/${NEW_ARCHIVE}`
  });
  assert.strictEqual(api.lastArchiveDropResult.action, 'ignored');
  assert.strictEqual(event.defaultPrevented, false);
});

test('W6-001: re-installing the sanitizer never duplicates the handler', () => {
  const { h, api } = setup();
  const { input, upload } = composerFixture(h);
  let uploads = 0;
  upload._onFilesSet = () => { uploads += 1; };

  assert.strictEqual(api.archiveDropSanitizerInstalled, true);
  assert.strictEqual(api.installComposerArchiveDropSanitizer(), false);
  assert.strictEqual(api.installComposerArchiveDropSanitizer(), false);

  dispatchDrop(h, input, { files: [archiveFile(NEW_ARCHIVE)], uri: `file:///V:/x/${NEW_ARCHIVE}` });
  assert.strictEqual(uploads, 1, 'one physical drop must produce exactly one upload');
});

// ---------------------------------------------------------------------------
// T-180H: AUTO ZIP transaction
// ---------------------------------------------------------------------------

test('W6-001: the AUTO ZIP button renders exactly once in the Auto state row', () => {
  const { h, api } = setup();
  api.mount();
  const buttons = h.dom.querySelectorAll('#acb-auto-state-row #acb-auto-zip');
  assert.strictEqual(buttons.length, 1);
  assert.strictEqual(buttons[0].textContent, '↻ ZIP');
  assert.strictEqual(buttons[0].dataset.state, 'idle');
});

test('W6-001: repeated AUTO ZIP clicks are single-flight', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api);
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  const second = await api.attachProjectArchiveForCurrentChat();
  assert.strictEqual(second.ok, false);
  assert.strictEqual(second.reason, 'in-flight');
  await h.settle();
  await first;
});

test('W6-001: project resolution is read-only and never registers a project', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api);
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await promise;
  assert.ok(h.httpRequests.some(request => /\/v1\/projects$/.test(request.url)), 'must read the registry');
  assert.ok(!h.httpRequests.some(request => /\/v1\/projects\/resolve$/.test(request.url)), 'must not call registration/resolve');
});

test('W6-001: an ambiguous project identity refuses to attach', async () => {
  const { h, api } = setup();
  composerFixture(h);
  armRuntime(api);
  installBridge(h, {
    projects: [
      project({ project_id: 'a', display_name: 'AUDAPACK', audit_name: 'AUDAPACK' }),
      project({ project_id: 'b', display_name: 'AUDAPACK', audit_name: 'AUDAPACK' })
    ]
  });
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});

test('W6-001: a proven canonical archive already attached is reused without re-upload', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const firstResult = await first;
  assert.strictEqual(firstResult.ok, true);

  installBridge(h, { ensure: { reused: true, packed: false } });
  const before = h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(request.url)).length;

  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await second;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.reused, true);
  const after = h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(request.url)).length;
  assert.strictEqual(after, before, 'a proven-fresh identical archive must not churn the upload');
});

test('W6-001: the new archive is attached before the old same-project archive is removed', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  oldTile.querySelector('button').addEventListener('click', () => oldTile.remove());
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h);

  let oldPresentWhenNewAdded = null;
  upload._onFilesSet = (element, files) => {
    for (const file of files) {
      oldPresentWhenNewAdded = Boolean(oldTile.parentNode);
      addComposerAttachmentTile(h, file.name);
    }
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(oldPresentWhenNewAdded, true, 'the old archive must survive until the new one is registered');
  assert.strictEqual(oldTile.parentNode, null, 'the old same-project archive is removed only after success');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
});

test('W6-001: unrelated attachments and audit prompt files survive an archive refresh', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  oldTile.querySelector('button').addEventListener('click', () => oldTile.remove());
  const reportTile = addComposerAttachmentTile(h, 'report.pdf');
  const promptTile = addComposerAttachmentTile(h, 'AUDIT_CORE_ABC123.md');
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(reportTile.parentNode, form, 'unrelated attachments must survive');
  assert.strictEqual(promptTile.parentNode, form, 'audit prompt files must survive');
});

test('W6-001: a digest mismatch refuses to attach the corrupt archive', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h, { ensure: { sha256: 'deadbeef' } });
  let uploads = 0;
  upload._onFilesSet = () => { uploads += 1; };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'archive-digest-mismatch');
  assert.strictEqual(uploads, 0);
  assert.strictEqual(oldTile.parentNode, form, 'old archive remains after a digest mismatch');
});

test('W6-001: a download failure leaves the old archive in place', async () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h, { archiveStatus: 500 });

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(oldTile.parentNode, form);
});

test('W6-001: a missing upload input leaves the old archive in place', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  upload.remove();
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h);

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'upload-input-unavailable');
  assert.strictEqual(oldTile.parentNode, form);
});

test('W6-001: an attachment-registration timeout leaves the old archive in place', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, OLD_ARCHIVE);
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = () => { };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  h.advance(31000);
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'attachment-timeout');
  assert.strictEqual(oldTile.parentNode, form);
});

test('W6-001: a successful refresh preserves composer text, sends nothing, and never starts the campaign', async () => {
  const { h, api } = setup();
  const { input, send, upload } = composerFixture(h);
  input.textContent = USER_TEXT;
  armRuntime(api, { archiveName: OLD_ARCHIVE });
  addComposerAttachmentTile(h, OLD_ARCHIVE);
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(api.composerPlainText(input), USER_TEXT, 'composer text must be unchanged');
  assert.notStrictEqual(send._clicked, true, 'AUTO ZIP must never click Send');
  assert.strictEqual(api.autoRuntime.stage, 'idle', 'AUTO ZIP must never advance the campaign');
  assert.strictEqual(api.autoRuntime.enabled, false);
  const freshness = api.currentAuditArchiveFreshness();
  assert.strictEqual(freshness.present, true);
  assert.strictEqual(freshness.name, NEW_ARCHIVE);
  assert.strictEqual(api.autoRuntime.archiveName, NEW_ARCHIVE, 'archive status becomes fresh immediately');
});

// ---------------------------------------------------------------------------
// T-181A: canonical attached-archive project resolution
// ---------------------------------------------------------------------------

test('W6-001/T-181: an attached canonical ZIP resolves the registered project when runtime identity is absent', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_AUDAPACK_09.09.26-T03-40-16.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  installBridge(h, {
    projects: [project({ project_id: 'audapack', display_name: '_AUDAPACK', audit_name: '_AUDAPACK' })]
  });
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'audapack');
});

test('W6-001/T-181: the attached archive identity outranks a stale runtime project name', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_09.09.26-T03-40-16.zip');
  armRuntime(api, { projectId: '', projectName: 'STALE' });
  installBridge(h, {
    projects: [
      project({ project_id: 'stale', display_name: 'STALE', audit_name: 'STALE' }),
      project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' })
    ]
  });
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'proja');
});

test('W6-001/T-181: colliding sanitized project identities are ambiguous', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_COLLIDE_09.09.26-T03-40-16.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  installBridge(h, {
    projects: [
      project({ project_id: 'collide-a', display_name: '_COLLIDE', audit_name: '_COLLIDE' }),
      project({ project_id: 'collide-b', display_name: 'COLLIDE', audit_name: 'COLLIDE' })
    ]
  });
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});

// ---------------------------------------------------------------------------
// T-181B: project-scoped archive cleanup
// ---------------------------------------------------------------------------

test('W6-001/T-181: refreshing Project A never removes the Project B archive', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldA = addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  const oldB = addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  oldA.querySelector('button').addEventListener('click', () => oldA.remove());
  armRuntime(api, { projectId: 'proja', projectName: 'PROJA', archiveName: '_PROJA_08.09.26-T01-00-00.zip' });
  installBridge(h, {
    projects: [
      project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' }),
      project({ project_id: 'projb', display_name: 'PROJB', audit_name: 'PROJB' })
    ],
    ensureByProject: { proja: { filename: '_PROJA_09.09.26-T03-00-00.zip' } }
  });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(oldB.parentNode, form, 'Project B archive must survive a Project A refresh');
  assert.strictEqual(oldA.parentNode, null, 'the old Project A archive is removed');
  assert.strictEqual(composerTilesNamed(h, '_PROJA_09.09.26-T03-00-00.zip').length, 1);
});

test('W6-001/T-181: multiple stale same-project archives are all removed while another project survives', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldA1 = addComposerAttachmentTile(h, '_PROJA_01.01.26-T00-00-00.zip');
  const oldA2 = addComposerAttachmentTile(h, '_PROJA_02.01.26-T00-00-00.zip');
  const oldB = addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  oldA1.querySelector('button').addEventListener('click', () => oldA1.remove());
  oldA2.querySelector('button').addEventListener('click', () => oldA2.remove());
  armRuntime(api, { projectId: 'proja', projectName: 'PROJA' });
  installBridge(h, {
    projects: [
      project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' }),
      project({ project_id: 'projb', display_name: 'PROJB', audit_name: 'PROJB' })
    ],
    ensureByProject: { proja: { filename: '_PROJA_03.03.26-T03-00-00.zip' } }
  });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(oldA1.parentNode, null);
  assert.strictEqual(oldA2.parentNode, null);
  assert.strictEqual(oldB.parentNode, form);
  assert.strictEqual(composerTilesNamed(h, '_PROJA_03.03.26-T03-00-00.zip').length, 1);
});

// ---------------------------------------------------------------------------
// T-181C: mixed drop preserves every File
// ---------------------------------------------------------------------------

test('W6-001/T-181: a mixed ZIP + PDF drop injects every file in order and suppresses file:/// text', () => {
  const { h, api } = setup();
  const { input, upload } = composerFixture(h);
  input.textContent = USER_TEXT;
  const zip = archiveFile(NEW_ARCHIVE);
  const pdf = new FakeFile([Buffer.from('%PDF-1.7')], 'report.pdf', { type: 'application/pdf' });
  let injected = [];
  upload._onFilesSet = (element, files) => {
    injected = files.map(file => file.name);
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const event = dispatchDrop(h, input, {
    files: [zip, pdf],
    uri: `file:///V:/___VAC/__K/__CODE/_PY/_AUDAPACK/${NEW_ARCHIVE}`
  });

  assert.strictEqual(api.lastArchiveDropResult.action, 'sanitized');
  assert.strictEqual(event.defaultPrevented, true);
  assert.deepStrictEqual(injected, [NEW_ARCHIVE, 'report.pdf'], 'every original File is injected in order');
  assert.strictEqual(upload.files.length, 2);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
  assert.strictEqual(composerTilesNamed(h, 'report.pdf').length, 1);
  assert.strictEqual(api.composerPlainText(input), USER_TEXT);
  assert.ok(!api.composerPlainText(input).includes('file:///'));
});

test('W6-001/T-181: a ZIP + image + text document drop preserves every File', () => {
  const { h, api } = setup();
  const { input, upload } = composerFixture(h);
  const files = [
    archiveFile(NEW_ARCHIVE),
    new FakeFile([Buffer.from('png')], 'photo.png', { type: 'image/png' }),
    new FakeFile([Buffer.from('notes')], 'notes.txt', { type: 'text/plain' })
  ];
  let injected = [];
  upload._onFilesSet = (element, list) => {
    injected = list.map(file => file.name);
    for (const file of list) addComposerAttachmentTile(h, file.name);
  };

  dispatchDrop(h, input, { files, uri: `file:///V:/x/${NEW_ARCHIVE}` });

  assert.deepStrictEqual(injected, [NEW_ARCHIVE, 'photo.png', 'notes.txt']);
  assert.strictEqual(upload.files.length, 3);
  assert.strictEqual(composerTilesNamed(h, 'photo.png').length, 1);
  assert.strictEqual(composerTilesNamed(h, 'notes.txt').length, 1);
});

test('W6-001/T-181: an ordinary multi-file drop without a project ZIP is not intercepted', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const event = dispatchDrop(h, input, {
    files: [
      new FakeFile([Buffer.from('%PDF-1.7')], 'report.pdf', { type: 'application/pdf' }),
      new FakeFile([Buffer.from('png')], 'photo.png', { type: 'image/png' })
    ],
    uri: 'file:///V:/x/report.pdf'
  });
  assert.strictEqual(api.lastArchiveDropResult.action, 'ignored');
  assert.strictEqual(event.defaultPrevented, false);
});

// ---------------------------------------------------------------------------
// T-181D: exact-archive proof
// ---------------------------------------------------------------------------

test('W6-001/T-181: same filename with a different known size forces a canonical refresh', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'proj', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;

  // A manually attached same-name archive with far fewer bytes.
  upload.files = [new FakeFile([Buffer.from('zip')], NEW_ARCHIVE, { type: 'application/zip' })];
  installBridge(h, { bytes: Buffer.alloc(999, 1), ensure: { reused: true, packed: false } });
  const before = h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(request.url)).length;

  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await second;
  const after = h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(request.url)).length;

  assert.notStrictEqual(result.reused, true, 'filename equality alone must not satisfy reuse');
  assert.strictEqual(after, before + 1, 'a size mismatch must force a canonical download');
});

test('W6-001/T-181: same filename without trusted digest proof is refreshed conservatively', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  addComposerAttachmentTile(h, NEW_ARCHIVE);
  armRuntime(api, { projectId: 'proj', archiveName: NEW_ARCHIVE });
  installBridge(h, { ensure: { reused: true, packed: false } });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.notStrictEqual(result.reused, true);
  assert.ok(h.httpRequests.some(request => request.method === 'GET' && /\/archive$/.test(request.url)), 'no proof means download the canonical archive');
});

test('W6-001/T-181: canonical proof for another project cannot satisfy reuse', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  const projects = [
    project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' }),
    project({ project_id: 'projb', display_name: 'PROJB', audit_name: 'PROJB' })
  ];
  armRuntime(api, { projectId: 'proja', archiveName: '_PROJA_09.09.26-T03-00-00.zip' });
  installBridge(h, { projects, ensureByProject: { proja: { filename: '_PROJA_09.09.26-T03-00-00.zip' } } });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;

  if (api.autoRuntime) api.autoRuntime.projectId = 'projb';
  const bridge = installBridge(h, { projects, ensureByProject: { projb: { filename: '_PROJA_09.09.26-T03-00-00.zip', reused: true } } });
  const injected = [];
  upload._onFilesSet = (element, files) => {
    for (const file of files) {
      injected.push(nodeCrypto.createHash('sha256').update(Buffer.from(file._bytes)).digest('hex'));
      addComposerAttachmentTile(h, file.name);
    }
  };
  const ensureCallsBefore = h.httpRequests.filter(request => request.method === 'POST' && /\/archive\/ensure$/.test(request.url)).length;

  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await second;

  assert.notStrictEqual(result.reused, true);
  // P1 TARGET G: the transport may legitimately be served by this runtime's
  // content-addressed VERIFIED byte cache, so the old "a GET must happen" proxy
  // is replaced by the stronger claim it was standing in for: project B's own
  // ensure ran, and the bytes actually injected are project B's canonical bytes
  // (proved by their SHA-256), never a proof carried over from project A.
  const ensureCallsAfter = h.httpRequests.filter(request => request.method === 'POST' && /\/archive\/ensure$/.test(request.url)).length;
  assert.strictEqual(ensureCallsAfter, ensureCallsBefore + 1, "project B resolves its own canonical archive");
  assert.strictEqual(injected.length, 1, 'exactly one attachment was injected');
  assert.strictEqual(injected[0], bridge, 'the injected bytes are the canonical bytes for project B');
});

// ---------------------------------------------------------------------------
// T-181E: same-name replacement tile transaction
// ---------------------------------------------------------------------------

test('W6-001/T-181: an untouched pre-existing same-name tile is not accepted as a new upload', async () => {
  const { h, api } = setup();
  const { form, upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  armRuntime(api, { projectId: 'proj', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = () => { };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  h.advance(31000);
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'attachment-timeout');
  assert.strictEqual(oldTile.parentNode, form, 'the old tile is not treated as the new upload');
});

test('W6-001/T-181: a genuinely new same-name tile proves success and cleanup keeps only it', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  const oldTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  oldTile.querySelector('button').addEventListener('click', () => oldTile.remove());
  armRuntime(api, { projectId: 'proj', archiveName: NEW_ARCHIVE });
  installBridge(h);
  let newTile = null;
  upload._onFilesSet = (element, files) => {
    for (const file of files) newTile = addComposerAttachmentTile(h, file.name);
  };

  const promise = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.notStrictEqual(newTile, oldTile);
  assert.strictEqual(oldTile.parentNode, null, 'the old same-name tile is removed only after success');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1);
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE)[0], newTile, 'cleanup must exclude the proven new tile');
});

// ---------------------------------------------------------------------------
// T-182A: tile-bound canonical proof
// ---------------------------------------------------------------------------

function archiveRequests(h) {
  return h.httpRequests.filter(request => request.method === 'GET' && /\/archive$/.test(request.url));
}

function addRemovableTile(h, name) {
  const tile = addComposerAttachmentTile(h, name);
  tile.querySelector('button').addEventListener('click', () => tile.remove());
  return tile;
}

test('W6-001/T-182: a removed proven tile never authorizes manually substituted bytes (exact reproduction)', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  const bytesA = Buffer.from('AAAAAAAAAA');
  const canonicalSha = nodeCrypto.createHash('sha256').update(bytesA).digest('hex');
  installBridge(h, { bytes: bytesA, ensure: { reused: true, packed: false } });
  const substituted = [];
  upload._onFilesSet = (element, files) => {
    for (const file of files) {
      substituted.push(nodeCrypto.createHash('sha256').update(Buffer.from(file._bytes)).digest('hex'));
      addComposerAttachmentTile(h, file.name);
    }
  };

  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const firstResult = await first;
  assert.strictEqual(firstResult.ok, true, 'successful canonical AUTO ZIP creates a proof');
  const provenTile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  assert.ok(provenTile, 'the canonical attachment tile exists');
  assert.ok(api.canonicalArchiveProofForTile(provenTile), 'the proven tile owns a canonical proof');

  // Remove the proven tile, then manually attach different bytes with the
  // same filename and the same size.
  provenTile.remove();
  assert.strictEqual(provenTile.isConnected, false);
  upload.files = [new FakeFile([Buffer.from('BBBBBBBBBB')], NEW_ARCHIVE, { type: 'application/zip' })];
  const manualTile = addComposerAttachmentTile(h, NEW_ARCHIVE);
  assert.notStrictEqual(manualTile, provenTile);

  const before = archiveRequests(h).length;
  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await second;
  const after = archiveRequests(h).length;

  assert.strictEqual(result.ok, true);
  assert.notStrictEqual(result.reused, true, 'the stale proof must not report reused=true for substituted bytes');
  // P1 TARGET G: the transport may be served by this runtime's content-addressed
  // VERIFIED byte cache, so "a GET happened" is no longer the right proxy for
  // "the canonical bytes were used". The claim the test is named for is now
  // asserted directly: the bytes that entered the composer are the canonical
  // ones, and the operator's substituted bytes never authorize themselves. The
  // two byte sets have the SAME SIZE on purpose, so this cannot pass by size.
  const substitutedSha = nodeCrypto.createHash('sha256').update(Buffer.from('BBBBBBBBBB')).digest('hex');
  assert.notStrictEqual(substitutedSha, canonicalSha);
  // 1 = the canonical AUTO attach, 2 = the operator's same-name substitution
  // (the harness reports a direct `files` assignment the same way), 3 = the
  // canonical bytes the widget had to re-attach.
  assert.strictEqual(substituted.length, 3, 'the operator substitution and the canonical re-attach were observed');
  assert.strictEqual(substituted[1], substitutedSha, 'the operator really substituted different bytes');
  assert.strictEqual(substituted[substituted.length - 1], canonicalSha, 'the substituted bytes never authorize themselves');
  // The canonical proof is bound to the widget's OWN new tile, never to the
  // substituted element that happens to share the filename and the byte size.
  const provenTiles = composerTilesNamed(h, NEW_ARCHIVE).filter(tile => api.canonicalArchiveProofForTile(tile));
  assert.strictEqual(provenTiles.length, 1, 'exactly one tile carries the canonical proof');
  assert.notStrictEqual(provenTiles[0], manualTile, 'the substituted tile is never the proven one');
  assert.strictEqual(String(api.canonicalArchiveProofForTile(provenTiles[0]).sha256), canonicalSha,
    'the proven tile is bound to the canonical SHA, not to the substituted bytes');
  assert.ok(after >= before, 'no proof of the removed tile may be reused as if it were this transaction');
});

test('W6-001/T-182: the exact proven tile still gets fast reuse', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  assert.strictEqual((await first).ok, true);
  const provenTile = composerTilesNamed(h, NEW_ARCHIVE)[0];

  installBridge(h, { ensure: { reused: true, packed: false } });
  const before = archiveRequests(h).length;
  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const result = await second;
  const after = archiveRequests(h).length;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.reused, true, 'the exact live proven tile keeps fast reuse');
  assert.strictEqual(after, before, 'no canonical download for the proven tile');
});

test('W6-001/T-182: a disconnected proven tile cannot satisfy the proof gate', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;
  const tile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  const proof = api.canonicalArchiveProofForTile(tile);
  assert.ok(proof && proof.sha256);

  tile.remove();
  assert.strictEqual(
    api.canonicalArchiveProofMatches('audapack', { filename: proof.filename, size: proof.size, sha256: proof.sha256 }, { tile, name: proof.filename, size: proof.size }),
    false,
    'a disconnected tile is proof-unusable'
  );
});

test('W6-001/T-182: a recreated DOM tile with the same filename/size has no proof', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;
  const provenTile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  const proof = api.canonicalArchiveProofForTile(provenTile);
  assert.ok(proof);

  const remounted = addComposerAttachmentTile(h, NEW_ARCHIVE);
  assert.notStrictEqual(remounted, provenTile);
  assert.strictEqual(api.canonicalArchiveProofForTile(remounted), null, 'a new DOM tile owns nothing');
  assert.strictEqual(
    api.canonicalArchiveProofMatches('audapack', { filename: proof.filename, size: proof.size, sha256: proof.sha256 }, { tile: remounted, name: proof.filename, size: proof.size }),
    false,
    'SPA remount same basename must not inherit the proof'
  );
});

test('W6-001/T-182: a manual same-name same-size attachment never inherits the proof', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;
  const provenTile = composerTilesNamed(h, NEW_ARCHIVE)[0];
  const proof = api.canonicalArchiveProofForTile(provenTile);

  const manual = addComposerAttachmentTile(h, NEW_ARCHIVE);
  assert.strictEqual(
    api.canonicalArchiveProofMatches('audapack', { filename: proof.filename, size: proof.size, sha256: proof.sha256 }, { tile: manual, name: proof.filename, size: proof.size }),
    false,
    'without byte proof: refresh canonically'
  );
});

test('W6-001/T-182: Project A tile proof never authorizes Project B', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'proja', archiveName: '_PROJA_09.09.26-T03-00-00.zip' });
  installBridge(h, {
    projects: [
      project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' }),
      project({ project_id: 'projb', display_name: 'PROJB', audit_name: 'PROJB' })
    ],
    ensureByProject: { proja: { filename: '_PROJA_09.09.26-T03-00-00.zip' } }
  });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addComposerAttachmentTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;
  const tileA = composerTilesNamed(h, '_PROJA_09.09.26-T03-00-00.zip')[0];
  const proofA = api.canonicalArchiveProofForTile(tileA);
  assert.ok(proofA && proofA.project_id === 'proja');

  assert.strictEqual(
    api.canonicalArchiveProofMatches('projb', { filename: proofA.filename, size: proofA.size, sha256: proofA.sha256 }, { tile: tileA, name: proofA.filename, size: proofA.size }),
    false,
    "Project A's proof must never authorize Project B"
  );
});

test('W6-001/T-182: sequential canonical AUTO ZIPs bind each proof to its exact tile only', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h, { bytes: Buffer.from('AAAAAAAAAA') });
  upload._onFilesSet = (element, files) => {
    for (const file of files) addRemovableTile(h, file.name);
  };
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  assert.strictEqual((await first).ok, true);
  const tile1 = composerTilesNamed(h, NEW_ARCHIVE)[0];
  const proof1 = api.canonicalArchiveProofForTile(tile1);
  assert.ok(proof1 && proof1.size === 10);

  installBridge(h, { bytes: Buffer.from('CCCCCCCCCC'), ensure: { reused: false, packed: true } });
  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  const secondResult = await second;
  assert.strictEqual(secondResult.ok, true);
  assert.strictEqual(secondResult.reused, false);

  const tile2 = composerTilesNamed(h, NEW_ARCHIVE)[0];
  assert.notStrictEqual(tile2, tile1, 'a new canonical transaction proves a new tile');
  assert.strictEqual(tile1.parentNode, null, 'the previous proven tile is replaced');
  const proof2 = api.canonicalArchiveProofForTile(tile2);
  assert.ok(proof2, 'the new tile owns its own proof');
  assert.notStrictEqual(proof2.sha256, proof1.sha256, 'each exact tile owns its own proof');
  assert.strictEqual(composerTilesNamed(h, NEW_ARCHIVE).length, 1, 'no duplicate attachment nodes');
});

test('W6-001/T-182: tile-bound proofs leak no persistent metadata or duplicate nodes', async () => {
  const { h, api } = setup();
  const { upload } = composerFixture(h);
  armRuntime(api, { projectId: 'audapack', archiveName: NEW_ARCHIVE });
  installBridge(h);
  upload._onFilesSet = (element, files) => {
    for (const file of files) addRemovableTile(h, file.name);
  };
  const keysBefore = new Set(h.gmStore.keys());
  const first = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await first;
  const second = api.attachProjectArchiveForCurrentChat();
  await h.settle();
  await second;

  const tiles = composerTilesNamed(h, NEW_ARCHIVE);
  assert.strictEqual(tiles.length, 1, 'tile binding must not duplicate UI nodes');
  for (const key of h.gmStore.keys()) {
    if (!keysBefore.has(key)) {
      assert.ok(!/proof|canonical|sha/i.test(key), 'no canonical proof metadata is persisted to GM storage');
    }
  }
});

// ---------------------------------------------------------------------------
// T-182B: multi-project archive resolution
// ---------------------------------------------------------------------------

function multiProjectBridge(h, extra = {}) {
  return installBridge(h, {
    projects: [
      project({ project_id: 'proja', display_name: 'PROJA', audit_name: 'PROJA' }),
      project({ project_id: 'projb', display_name: 'PROJB', audit_name: 'PROJB' })
    ],
    ...extra
  });
}

test('W6-001/T-182: two different project archives with empty runtime identity are ambiguous_project', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});

test('W6-001/T-182: newest timestamp must not decide between two projects (B newer)', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project', 'Project B is newer but that is not project intent');
});

test('W6-001/T-182: oldest timestamp must not decide between two projects (A newer)', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_09.09.26-T02-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_08.09.26-T01-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});

test('W6-001/T-182: multiple generations of one project resolve that project (two archives)', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_01.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'proja');
});

test('W6-001/T-182: three archives of one project still resolve it', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_01.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJA_05.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'proja');
});

test('W6-001/T-182: a stale runtime name cannot hide two attached project identities', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  armRuntime(api, { projectId: '', projectName: 'PROJA' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project', 'conflicting attached identities must not fall through to runtimeName');
});

test('W6-001/T-182: a valid runtime projectId outranks attached project archives', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  armRuntime(api, { projectId: 'proja', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'proja', 'runtime projectId remains highest authority');
});

test('W6-001/T-182: an invalid runtime id falls through to attached-identity ambiguity', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  addComposerAttachmentTile(h, '_PROJB_09.09.26-T02-00-00.zip');
  armRuntime(api, { projectId: 'ghost', projectName: '' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});

test('W6-001/T-182: one attached archive outranks a stale runtime project name', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_PROJA_08.09.26-T01-00-00.zip');
  armRuntime(api, { projectId: '', projectName: 'PROJB' });
  multiProjectBridge(h);
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.project.project_id, 'proja');
});

test('W6-001/T-182: one attached identity matching two registered projects stays ambiguous', async () => {
  const { h, api } = setup();
  composerFixture(h);
  addComposerAttachmentTile(h, '_COLLIDE_08.09.26-T01-00-00.zip');
  armRuntime(api, { projectId: '', projectName: '' });
  installBridge(h, {
    projects: [
      project({ project_id: 'collide-a', display_name: '_COLLIDE', audit_name: '_COLLIDE' }),
      project({ project_id: 'collide-b', display_name: 'COLLIDE', audit_name: 'COLLIDE' })
    ]
  });
  const promise = api.resolveRegisteredProjectForArchive();
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.errorCode, 'ambiguous_project');
});
