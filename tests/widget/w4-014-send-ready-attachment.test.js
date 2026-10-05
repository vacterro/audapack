'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

function busyAttachment(h, form, label = 'PROJECT.zip') {
  const tile = h.el('div', { role: 'group', 'aria-label': label });
  tile.appendChild(h.el('button', { 'aria-label': 'Remove file' }));
  const spinner = h.el('div', { class: 'animate-spin' });
  tile.appendChild(spinner);
  form.appendChild(tile);
  return { tile, spinner };
}

// The widget sleeps on the harness clock while its deadlines read the real
// one, so a driver has to move both.
async function drive(h, ms, step = 30) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    h.advance(step);
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

test('T126: an ingesting attachment keeps the Send wait alive past its nominal timeout', async () => {
  // ChatGPT keeps Send aria-disabled while it swallows an attachment, and a
  // few hundred megabytes take far longer than any fixed wait. Observed live
  // on four of six dispatches at once: button=found disabled=false aria=true
  // tiles=1 composerPrepared=true, the wait expiring, and the operator told
  // to press Send by hand on a run that was supposed to need nobody.
  const { h, api } = setup();
  const { form, send } = composerFixture(h);
  send.setAttribute('aria-disabled', 'true');
  const { spinner } = busyAttachment(h, form);

  let settled = false;
  const pending = api.waitForChatGPTSendReady(40, 4000).then(value => {
    settled = true;
    return value;
  });

  await drive(h, 500);
  assert.strictEqual(settled, false, 'the wait must not expire while the tile is busy');

  spinner.remove();
  send.setAttribute('aria-disabled', 'false');
  await drive(h, 300);
  assert.strictEqual(await pending, send, 'the wait must return the live Send control');
});

test('T126: with nothing uploading the short wait still fails fast', async () => {
  // The fast failure is the point: nothing is ingesting, so a Send that never
  // enables is a real problem rather than a slow one.
  const { h, api } = setup();
  const { send } = composerFixture(h);
  send.setAttribute('aria-disabled', 'true');

  let settled = false;
  const pending = api.waitForChatGPTSendReady(40, 4000).then(value => {
    settled = true;
    return value;
  });

  await drive(h, 400);
  assert.strictEqual(settled, true, 'no attachment means no extension');
  assert.strictEqual(await pending, null);
});

test('T129: a tile newly registering holds the wait briefly', async () => {
  const { h, api } = setup();
  const { form, send } = composerFixture(h);
  send.setAttribute('aria-disabled', 'true');
  const tile = h.el('div', { role: 'group', 'aria-label': 'PROJECT.zip' });
  tile.appendChild(h.el('button', { 'aria-label': 'Remove file' }));
  tile.appendChild(h.el('span', { class: 'animate-spin' }));
  form.appendChild(tile);

  let settled = false;
  const pending = api.waitForChatGPTSendReady(40, 4000).then(value => {
    settled = true;
    return value;
  });

  await drive(h, 200);
  assert.strictEqual(settled, false, 'a registering tile must hold the wait briefly');

  const spin = tile.querySelector('.animate-spin');
  if (spin) spin.remove();
  send.setAttribute('aria-disabled', 'false');
  await drive(h, 300);
  assert.strictEqual(await pending, send);
});
