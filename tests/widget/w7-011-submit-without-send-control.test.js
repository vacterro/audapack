'use strict';

// T-260: the widget attached the archive, authored the audit text, and never
// sent. The operator had to press Send by hand.
//
// The cause is structural, not cosmetic. Both TRUSTED submit paths -- clicking
// the discovered control and form.requestSubmit() on the composer's own form --
// lived inside clickChatGPTSendVerified(), which starts by requiring a
// DISCOVERED send control. When discovery finds nothing, the only code left to
// run is a dispatched KeyboardEvent, which is untrusted: a site binding send to
// its own keydown handler ignores it. So a build that renames or drops the submit
// control does not degrade to a weaker send -- it degrades to no send at all,
// with the payload sitting visibly in the composer.
//
// This file proves the fallback exists, that it is the form's own trusted
// submit, and that it is still gated on real acceptance.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, composerFixture, addComposerAttachmentTile, installAcceptedSend } = require('./helpers');

const PAYLOAD = 'AUDIT CORE — wave 1/3 of Quick 3 Waves.\nCAMPAIGN_RUN_ID: run-submit-fallback';

// A composer on a build whose submit control the widget cannot identify: no
// `#composer-submit-button`, no `data-testid="send-button"`, no recognised
// aria-label, and a form that has no `button[type="submit"]` to fall back on.
function composerWithUnidentifiedSubmit(h) {
  const main = mainEl(h);
  const form = h.el('form', { 'data-type': 'unified-composer', class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  input.textContent = PAYLOAD;
  form.appendChild(input);
  const slot = h.el('div', { class: 'flex items-center' });
  // The real control on the live build, carrying an identity the widget's list
  // does not know. It is deliberately NOT named so the fallback is exercised.
  slot.appendChild(h.el('button', { type: 'button', 'aria-label': 'Submit message' }));
  form.appendChild(slot);
  main.appendChild(form);
  return { form, input, slot };
}

function fixture(h) {
  return composerFixture(h);
}

// The RED precondition, kept as an assertion so the fallback can never be
// "passing" for the wrong reason. Pre-fix this fixture IS the whole defect:
// discovery returns nothing, and the only code left to run is an untrusted
// synthetic keydown, so the composer keeps the payload and nothing is sent.
test('W7-011 RED precondition: this build has no discoverable send control', () => {
  const { h, api } = setup();
  composerWithUnidentifiedSubmit(h);
  assert.strictEqual(api.getChatGPTSend(), null,
    'if discovery found a control here, this suite would prove nothing');
});

test('W7-011: the composer form is submitted when no send control is found', async () => {
  const { h, api } = setup();
  const composer = composerWithUnidentifiedSubmit(h);
  // A real site accepts a submit by consuming the payload and adding the user
  // turn carrying it. That transition -- not the submit -- is acceptance.
  composer.form.addEventListener('submit', () => {
    const text = String(composer.input.textContent || '');
    if (!text.trim()) return;
    composer.input.textContent = '';
    const index = (Number(composer.form._acceptedTurns) || 0) + 1;
    composer.form._acceptedTurns = index;
    const turn = h.el('article', {
      'data-message-author-role': 'user',
      'data-testid': `conversation-turn-${index}`,
      'data-message-id': `accepted-${index}`
    });
    turn._text = text;
    mainEl(h).appendChild(turn);
  });

  const promise = api.triggerSend(api.detectSite(), composer.input, {});
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true, `send failed in mode ${result.mode}`);
  assert.strictEqual(result.mode, 'form-submit',
    'the trusted path is the form, not a synthetic key event');
  assert.strictEqual(Number(composer.form._submitCount) || 0, 1,
    'exactly one form submission');
  const turns = mainEl(h).querySelectorAll('[data-message-author-role="user"]');
  assert.strictEqual(turns.length, 1, 'the payload went out exactly once');
});

test('W7-011: an unaccepted form submit is never reported as sent', async () => {
  const { h, api } = setup();
  const composer = composerWithUnidentifiedSubmit(h);
  // A site that ignores the submit: the payload stays in the composer.
  composer.form.addEventListener('submit', event => event.preventDefault());
  const promise = api.triggerSend(api.detectSite(), composer.input, {});
  await h.settle();
  const result = await promise;
  assert.notStrictEqual(result.ok, true,
    'a submit nobody accepted is not a send, however trusted it was');
  assert.strictEqual(String(composer.input.textContent || ''), PAYLOAD,
    'the payload is still in the composer');
});

test('W7-011: an empty composer is never submitted', async () => {
  const { h, api } = setup();
  const composer = composerWithUnidentifiedSubmit(h);
  composer.input.textContent = '';
  const promise = api.triggerSend(api.detectSite(), composer.input, {});
  await h.settle();
  const result = await promise;
  assert.notStrictEqual(result.ok, true);
  assert.strictEqual(Number(composer.form._submitCount) || 0, 0,
    'an empty composer must never be submitted');
});

test('W7-011: a discoverable send control still wins over the form fallback', async () => {
  const { h, api } = setup();
  const known = installAcceptedSend(h, composerFixture(h));
  known.input.textContent = PAYLOAD;
  let submitted = 0;
  known.form.addEventListener('submit', () => { submitted += 1; });
  const promise = api.triggerSend(api.detectSite(), known.input, {});
  await h.settle();
  const result = await promise;
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.mode, 'button',
    'the click path is unchanged when the control is known');
  assert.strictEqual(submitted, 0, 'and it does not also submit the form');
});

test('W7-011: an attached archive alone is a payload worth submitting', async () => {
  const { h, api } = setup();
  const composer = composerWithUnidentifiedSubmit(h);
  composer.input.textContent = '';
  addComposerAttachmentTile(h, 'AUDAPACK.zip', { busy: false });
  let submitted = 0;
  composer.form.addEventListener('submit', () => { submitted += 1; });
  const promise = api.triggerSend(api.detectSite(), composer.input, {});
  await h.settle();
  const result = await promise;
  assert.strictEqual(submitted, 1,
    'an attachment is a payload: the form submit fires even with no text');
});

// A control the widget CAN name but that the platform has disabled is the
// platform saying this composer is not ready -- an attachment still uploading,
// a model still switching. The form fallback must not override that: it exists
// for a control the widget cannot identify, not for one it chose to bypass.
test('W7-011: a DISABLED discovered control blocks the form fallback', async () => {
  const { h, api } = setup();
  const known = installAcceptedSend(h, composerFixture(h));
  known.input.textContent = PAYLOAD;
  known.send.setAttribute('aria-disabled', 'true');
  let submitted = 0;
  known.form.addEventListener('submit', () => { submitted += 1; });

  const promise = api.triggerSend(api.detectSite(), known.input, {});
  await h.settle();
  const result = await promise;

  assert.strictEqual(submitted, 0,
    'the form is never submitted behind a control the platform disabled');
  assert.strictEqual(known.send._clickCount, undefined,
    'and the disabled control is never clicked');
  assert.notStrictEqual(result.ok, true);
});
