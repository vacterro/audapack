'use strict';

// Generating is not submission when there was nothing to submit.
//
// Measured live on dsp-5d5df0f69b3244b8 and dsp-c6763cc27a1242a9, both on the
// same signature: `send 3.82s | ack 1.06s`, then turns=0(u0/a0) forever. A CDP
// session on the dedicated AUDAPACK profile showed why -- the page was the
// ChatGPT home with an EMPTY composer (one visible contenteditable, innerText
// length 1), zero Send controls and zero attachment tiles, while the page
// already reported generating. chatGPTSendOutcome() accepted that as
// submitted, the Bridge ACKed the irreversible START_PREPARED fence, the lane
// went STARTED, and ChatGPT never created a turn.
//
// The other half is kept as a control: generation WITH a payload in the composer
// is still accepted, and so is the w7-012 transport verdict.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl } = require('./helpers');

function currentComposer(h, body) {
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  input.textContent = body;
  form.appendChild(input);
  form.appendChild(h.el('div', { class: 'flex items-center' }));
  mainEl(h).appendChild(form);
  return { form, input };
}

function mountGeneratingStop(h, form) {
  form.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop' }));
  h.mutate(form);
}

test('an empty composer plus a generating page is NOT submission', async () => {
  const { h, api } = setup();
  const composer = currentComposer(h, '');
  mountGeneratingStop(h, composer.form);

  assert.strictEqual(api.chatGPTIsGenerating(), true, 'the fixture must really be generating');
  // The bounded acceptance window has to be walked on the harness clock, or the
  // refusal never arrives: awaiting it directly parks on a sleep() nothing fires.
  const pending = api.chatGPTSendAccepted('startcore-1', '', 900, false);
  await h.settle();
  assert.strictEqual(
    await pending, false,
    'nothing was in the composer, so a generating page cannot prove a submission'
  );
});

test('a payload in the composer plus a generating page is still accepted', async () => {
  const { h, api } = setup();
  const composer = currentComposer(h, 'AUDIT CORE\nACB_CHAIN_RECEIPT: startcore-1');
  mountGeneratingStop(h, composer.form);

  assert.strictEqual(
    await api.chatGPTSendAccepted('startcore-1', 'AUDIT CORE', 900, true), true,
    'generation with a payload stays the liveness signal it was meant to be'
  );
});