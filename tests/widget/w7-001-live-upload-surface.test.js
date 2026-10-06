'use strict';

// T-248. On 2026-09-27 all six A3 lanes died in BLOCKED PRE-START with the one
// opaque code `file-injection-rejected` while the 39 synthetic DOM tests stayed
// green. These are the DOM shapes the old single selector could not see, each
// one a red control for the shared upload engine.

const test = require('node:test');
const assert = require('node:assert');
const { createHarness } = require('./harness');

function boot() {
  const h = createHarness();
  const api = h.load();
  if (h.loadError) throw h.loadError;
  return { h, api };
}

function main(h) {
  return h.dom.documentElement.querySelector('main');
}

function composerTextInput(h) {
  const input = h.el('div', {
    id: 'prompt-textarea',
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Chat with ChatGPT'
  });
  input.isContentEditable = true;
  return input;
}

function attachButton(h, attrs = {}) {
  return h.el('button', { 'data-testid': 'composer-attach-files', 'aria-label': 'Attach files', ...attrs });
}

function fileInput(h, attrs = {}) {
  return h.el('input', { type: 'file', ...attrs });
}

function zipFile(name = 'DEMO_27.09.26-T10-00-00.zip') {
  return new File([new Uint8Array([80, 75, 3, 4])], name, { type: 'application/zip' });
}

function rootContains(root, node) {
  let current = node;
  while (current) {
    if (current === root) return true;
    current = current.parentNode;
  }
  return false;
}

function filesOf(input) {
  return Array.from(input.files || []).map(file => String(file.name || ''));
}

// The engine's bounded wait runs on the harness fake clock, so the clock has to
// keep moving while the injection promise is pending.
async function injectAndSettle(h, api, file) {
  const pending = api.injectComposerArchiveFile(file);
  await h.settle();
  return pending;
}

test('T-248-01: file input without `multiple` is still a composer upload surface', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  const upload = fileInput(h, { id: 'upload-files' });
  form.appendChild(composerTextInput(h));
  form.appendChild(upload);
  main(h).appendChild(form);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.input, upload);
});

test('T-248-02: a file input with no id and no testid inside the composer is found', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  const upload = fileInput(h);
  form.appendChild(composerTextInput(h));
  form.appendChild(upload);
  main(h).appendChild(form);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.input, upload);
});

test('T-248-03: a portal file input the composer declares via aria-controls is owned', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  form.appendChild(attachButton(h, { 'aria-controls': 'composer-file-portal' }));
  main(h).appendChild(form);

  // ChatGPT renders the attachment menu in a body-level portal, outside <form>.
  const portal = h.el('div', { id: 'composer-file-portal', role: 'menu' });
  const upload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  portal.appendChild(upload);
  h.dom.body.appendChild(portal);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.input, upload);
  assert.strictEqual(api.chatGPTUploadBoundOwner(upload, form), form, 'the composer declares the surface');
});

test('T-248-03b: a portal file input with no stable id is still owned by the composer', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  form.appendChild(attachButton(h, { 'aria-controls': 'composer-file-portal' }));
  main(h).appendChild(form);

  const portal = h.el('div', { id: 'composer-file-portal', role: 'menu' });
  const upload = fileInput(h);
  portal.appendChild(upload);
  h.dom.body.appendChild(portal);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.reason, 'portal-input');
  assert.strictEqual(surface.input, upload);
});

test('T-248-04: a file input that appears only after the attachment control opens is prepared', async () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  const attach = attachButton(h, { 'aria-controls': 'composer-file-portal', 'aria-expanded': 'false' });
  form.appendChild(attach);
  main(h).appendChild(form);

  let portal = null;
  let opened = 0;
  attach.addEventListener('click', () => {
    if (attach.getAttribute('aria-expanded') === 'true') {
      attach.setAttribute('aria-expanded', 'false');
      if (portal) portal.remove();
      return;
    }
    opened += 1;
    attach.setAttribute('aria-expanded', 'true');
    portal = h.el('div', { id: 'composer-file-portal', role: 'menu' });
    portal.appendChild(fileInput(h, { id: 'upload-files', multiple: 'true' }));
    h.dom.body.appendChild(portal);
  });

  assert.strictEqual(api.chatGPTUploadSurface().ok, false, 'nothing is discoverable before the surface opens');
  const outcome = await injectAndSettle(h, api, zipFile());
  assert.strictEqual(outcome.ok, true, `reason was ${outcome.reason}`);
  assert.strictEqual(opened, 1, 'the attachment surface was opened exactly once');
  assert.strictEqual(attach.getAttribute('aria-expanded'), 'false', 'the menu was closed again');
});

test('T-248-05: a composer remount mid-attempt is re-acquired, never reused', async () => {
  const { h, api } = boot();
  const shell = h.el('div', { id: 'composer-shell' });
  const first = h.el('form', { 'data-type': 'unified-composer' });
  const firstUpload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  first.appendChild(composerTextInput(h));
  first.appendChild(firstUpload);
  shell.appendChild(first);
  main(h).appendChild(shell);

  // React tore the composer down on the very commit that refused the FileList
  // assignment, and mounted a new one. The engine must not carry the dead
  // input identity into the retry.
  let nextUpload = null;
  firstUpload._onFilesSet = () => {
    first.remove();
    const next = h.el('form', { 'data-type': 'unified-composer' });
    nextUpload = fileInput(h, { id: 'upload-files', multiple: 'true' });
    next.appendChild(composerTextInput(h));
    next.appendChild(nextUpload);
    shell.appendChild(next);
    throw new Error('composer remounted mid-assignment');
  };

  const file = zipFile();
  const outcome = await injectAndSettle(h, api, file);
  assert.strictEqual(outcome.ok, true, `reason was ${outcome.reason}`);
  assert.ok(nextUpload, 'the live composer was reacquired');
  assert.strictEqual(firstUpload.isConnected, false, 'the torn-down input is gone from the document');
  assert.strictEqual(outcome.input, nextUpload, 'delivery landed on the live composer, not the dead identity');
  assert.deepStrictEqual(filesOf(nextUpload), [file.name]);
});

test('T-248-06: an input that refuses one assignment is retried against the live composer', async () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  const upload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  form.appendChild(composerTextInput(h));
  form.appendChild(upload);
  main(h).appendChild(form);

  // One React commit refuses the assignment; the next one accepts it. A single
  // refusal is a transient remount, never a dead lane.
  let armed = 1;
  upload._onFilesSet = () => {
    if (!armed) return;
    armed -= 1;
    throw new Error('composer remounted mid-assignment');
  };
  const file = zipFile();
  const outcome = await injectAndSettle(h, api, file);
  assert.strictEqual(outcome.ok, true, `reason was ${outcome.reason}`);
  assert.strictEqual(armed, 0, 'the refused assignment was retried exactly once');
  assert.deepStrictEqual(filesOf(upload), [file.name]);
});

test('T-248-07: two unproven composer file inputs fail closed and inject into neither', async () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  const left = fileInput(h);
  const right = fileInput(h);
  form.appendChild(composerTextInput(h));
  form.appendChild(left);
  form.appendChild(right);
  main(h).appendChild(form);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, false);
  assert.strictEqual(surface.reason, 'upload-input-ambiguous');
  const outcome = await injectAndSettle(h, api, zipFile());
  assert.strictEqual(outcome.ok, false);
  assert.strictEqual(outcome.reason, 'upload-input-ambiguous');
  assert.deepStrictEqual(filesOf(left), []);
  assert.deepStrictEqual(filesOf(right), []);
});

test('T-248-08: two inputs where exactly one is provably composer-owned is accepted', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  form.appendChild(attachButton(h, { 'aria-controls': 'composer-file-portal' }));
  main(h).appendChild(form);

  const portal = h.el('div', { id: 'composer-file-portal', role: 'menu' });
  const owned = fileInput(h, { id: 'upload-files', multiple: 'true' });
  portal.appendChild(owned);
  h.dom.body.appendChild(portal);

  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.input, owned);
});

test('T-248-09: a file input in a hidden stale composer is never injected', async () => {
  const { h, api } = boot();
  const stale = h.el('form', { 'data-type': 'unified-composer' });
  const staleUpload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  stale.appendChild(staleUpload);
  stale.hidden = true;
  main(h).appendChild(stale);

  const live = h.el('form', { 'data-type': 'unified-composer' });
  live.appendChild(composerTextInput(h));
  main(h).appendChild(live);

  const outcome = await injectAndSettle(h, api, zipFile());
  assert.strictEqual(outcome.ok, false);
  assert.strictEqual(outcome.reason, 'upload-input-unavailable');
  assert.deepStrictEqual(filesOf(staleUpload), [], 'the stale composer input stays untouched');
});

test('T-248-10: an unrelated page-level file input is never injected', async () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  main(h).appendChild(form);

  const settings = h.el('div', { role: 'dialog', 'aria-label': 'Settings' });
  const picker = fileInput(h, { id: 'avatar-upload', multiple: 'true' });
  settings.appendChild(picker);
  h.dom.body.appendChild(settings);

  const outcome = await injectAndSettle(h, api, zipFile());
  assert.strictEqual(outcome.ok, false);
  assert.strictEqual(outcome.reason, 'upload-input-unavailable');
  assert.deepStrictEqual(filesOf(picker), [], 'the settings picker stays untouched');
});

test('T-248-11: the topology probe names every candidate without leaking content', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  form.appendChild(attachButton(h));
  const upload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  form.appendChild(upload);
  main(h).appendChild(form);

  const topology = api.chatGPTUploadTopology();
  assert.strictEqual(topology.file_inputs, 1);
  assert.strictEqual(topology.composer_attach_control, 1);
  assert.strictEqual(topology.candidates[0].id, 'upload-files');
  assert.strictEqual(topology.candidates[0].inComposerRoot, true);
  assert.strictEqual(topology.candidates[0].boundToComposer, true);
  assert.strictEqual(topology.verdict, 'exact-match');
  const serialized = JSON.stringify(topology);
  assert.ok(!/prompt-textarea|Chat with ChatGPT/.test(serialized),
    'the probe carries structure only, never composer text or labels');
});

test('T-248-12: structural codes are the ones a retry can never fix', () => {
  const { api } = boot();
  for (const code of ['composer-root-unavailable', 'upload-input-unavailable', 'upload-input-ambiguous']) {
    assert.strictEqual(api.chatGPTUploadCodeIsStructural(code), true, code);
  }
  for (const code of ['upload-input-detached', 'file-injection-rejected', 'attachment-registration-timeout']) {
    assert.strictEqual(api.chatGPTUploadCodeIsStructural(code), false, code);
  }
});

test('T-248-13: the pre-fix selector is the red control for this ticket', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  form.appendChild(attachButton(h, { 'aria-controls': 'composer-file-portal' }));
  main(h).appendChild(form);
  const portal = h.el('div', { id: 'composer-file-portal', role: 'menu' });
  const upload = fileInput(h, { id: 'upload-files', multiple: 'true' });
  portal.appendChild(upload);
  h.dom.body.appendChild(portal);

  // Exactly what shipped until T-248: one root-scoped selector, and every
  // caller demanding `root.contains(input)` on top of it.
  const preFix = root => root.querySelector('#upload-files[type="file"], input[type="file"][multiple]');
  assert.strictEqual(preFix(form), null,
    'the old contract cannot see the input ChatGPT actually renders');
  assert.strictEqual(rootContains(form, upload), false,
    'and root.contains() rejected it a second time, as file-injection-rejected');
  assert.strictEqual(api.chatGPTUploadInput(), upload, 'the shared engine finds it');
});

// --- The real 2026-09 ChatGPT build, captured live via CDP against the ---
// --- managed profile. Editor lost #prompt-textarea and now carries       ---
// --- aria-label="Ask ChatGPT" + class ProseMirror; the <form> lost        ---
// --- data-type="unified-composer" and only the hashed Composer* classes   ---
// --- remain; three media file inputs sit beside one general input.        ---

function liveComposer2026(h, options = {}) {
  // <form class="... group/composer ..."> with a ProseMirror editor labelled
  // "Ask ChatGPT" and no data-type -- exactly the observed shape.
  const shell = h.el('div', { class: 'ComposerModeSurface-lVLm7j' });
  const form = h.el('form', { class: 'group/composer w-full relative flex flex-col gap-2' });
  const editor = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  editor.isContentEditable = true;
  form.appendChild(editor);
  form.appendChild(h.el('button', { 'aria-label': 'Add files and more' }));
  // Three media-only inputs the composer mounts for the "+" menu.
  form.appendChild(fileInput(h, { id: '_r_c_', accept: 'image/*,video/*', multiple: 'true' }));
  form.appendChild(fileInput(h, { id: '_r_b_', accept: 'image/*', multiple: 'true' }));
  const general = fileInput(h, { id: '_r_a_', multiple: 'true' });
  form.appendChild(general);
  shell.appendChild(form);
  main(h).appendChild(shell);
  if (options.withSend) {
    form.appendChild(h.el('button', { 'aria-label': 'Send', type: 'submit' }));
  }
  // A stray page-level JSON input, like the widget's own import control.
  const stray = fileInput(h, { accept: '.json,application/json' });
  h.dom.body.appendChild(stray);
  return { form, editor, general, stray };
}

test('T-248-14: the 2026-09 build (Ask ChatGPT, no data-type) still finds the composer', () => {
  const { h, api } = boot();
  const { form, editor } = liveComposer2026(h);
  assert.strictEqual(api.rawChatGPTComposerInput(), editor, 'the ProseMirror editor is the composer input');
  assert.strictEqual(api.chatGPTComposerRoot(), form, 'the form is the composer root via its hashed Composer class');
});

test('T-248-15: three media inputs + one general input resolves to the general input', () => {
  const { h, api } = boot();
  const { general } = liveComposer2026(h);
  const surface = api.chatGPTUploadSurface();
  assert.strictEqual(surface.ok, true, `reason was ${surface.reason}`);
  assert.strictEqual(surface.input, general, 'a project ZIP goes into the general input, never an image/video picker');
});

test('T-248-16: a project ZIP is never injected into an image/video picker', async () => {
  const { h, api } = boot();
  const { form, general } = liveComposer2026(h);
  const media = form.children.filter(child =>
    child.tagName === 'INPUT' && /image|video/.test(String(child.getAttribute('accept') || '')));
  assert.ok(media.length >= 2, 'fixture models the real media inputs');

  const outcome = await injectAndSettle(h, api, zipFile());
  assert.strictEqual(outcome.ok, true, `reason was ${outcome.reason}`);
  assert.deepStrictEqual(filesOf(general), [zipFile().name]);
  for (const input of media) {
    assert.deepStrictEqual(filesOf(input), [], `media input ${input.getAttribute('accept')} stays empty`);
  }
});

test('T-248-17: the media filter classifies accept lists correctly', () => {
  const { h, api } = boot();
  const media = fileInput(h, { accept: 'image/jpeg,.jpg,.jpeg,.mpo,image/png,.png,image/webp,.webp,image/gif,.gif' });
  const mixedMedia = fileInput(h, { accept: 'image/*,video/*' });
  const general = fileInput(h, { accept: '' });
  const zips = fileInput(h, { accept: '.zip,.md,application/zip' });
  assert.strictEqual(api.chatGPTUploadInputIsMediaOnly(media), true);
  assert.strictEqual(api.chatGPTUploadInputIsMediaOnly(mixedMedia), true);
  assert.strictEqual(api.chatGPTUploadInputIsMediaOnly(general), false, 'no accept = general input');
  assert.strictEqual(api.chatGPTUploadInputIsMediaOnly(zips), false, 'a zip/md accept is not media');
});

test('T-248-18: the 2026-09 tile shape (filename on the Remove button) is detected and named', () => {
  const { h, api } = boot();
  const { form } = liveComposer2026(h);
  // Live shape: an unlabelled composer-attachment wrapper whose only filename
  // is on the "Remove <name>" control, not on a [role=group][aria-label].
  const attachments = h.el('div', { class: 'ComposerLayoutAttachments-bG080e' });
  const row = h.el('div', { class: 'flex flex-wrap items-end gap-3 p-1' });
  const wrapper = h.el('span', { class: 'group/composer-attachment relative block' });
  wrapper.appendChild(h.el('button', { 'aria-label': 'Remove _AUDAPACK_27.09.26-T18-14-14.zip', class: 'absolute flex size-4' }));
  row.appendChild(wrapper);
  attachments.appendChild(row);
  form.appendChild(attachments);

  const tiles = api.chatGPTComposerAttachmentTiles(form);
  assert.strictEqual(tiles.length, 1, 'the unlabelled wrapper is still a tile');
  assert.strictEqual(api.chatGPTAttachmentTileName(tiles[0]), '_AUDAPACK_27.09.26-T18-14-14.zip',
    'the filename is read off the Remove control');
});

test('T-248-19: legacy [role=group][aria-label] tiles are still detected', () => {
  const { h, api } = boot();
  const form = h.el('form', { 'data-type': 'unified-composer' });
  form.appendChild(composerTextInput(h));
  const tile = h.el('div', { role: 'group', 'aria-label': 'LEGACY_01.01.26.zip' });
  tile.appendChild(h.el('button', { 'aria-label': 'Remove file LEGACY_01.01.26.zip' }));
  form.appendChild(tile);
  main(h).appendChild(form);

  const tiles = api.chatGPTComposerAttachmentTiles(form);
  assert.strictEqual(tiles.length, 1);
  assert.strictEqual(api.chatGPTAttachmentTileName(tiles[0]), 'LEGACY_01.01.26.zip');
});
