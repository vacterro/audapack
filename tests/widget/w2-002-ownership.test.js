'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture, installAcceptedSend, leaseFor } = require('./helpers');

const KEY = 'c:abc123';

async function withToken(h, api, fn) {
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token, 'expected a valid lease token');
  return fn(token);
}

test('W2-002: guard verifies empty composer before any write', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    assert.strictEqual(await guard.verify(), true);
    assert.strictEqual(api.composerPlainText(input), '');
  });
});

test('W2-002: manual draft appearing before write aborts the transaction', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    input.textContent = 'a manual draft appeared';
    assert.strictEqual(await guard.verify(), false);
  });
});

test('W2-002: manual attachment appearing before write aborts the transaction', async () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const tile = h.el('div', { role: 'group', 'aria-label': 'report.pdf' });
    tile.appendChild(h.el('button', { 'aria-label': 'Remove file' }));
    form.appendChild(tile);
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    assert.strictEqual(await guard.verify(), false);
  });
});

test('W2-002: after captureWrite the auto-owned content stays valid', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    input.textContent = 'AUDIT SECOND WAVE — the automatic prompt';
    guard.captureWrite();
    assert.strictEqual(await guard.verify(), true);
  });
});

test('W2-002: manual edit after captureWrite invalidates the guard', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    input.textContent = 'AUDIT SECOND WAVE — the automatic prompt';
    guard.captureWrite();
    input.textContent = 'AUDIT SECOND WAVE — the automatic prompt\nmanual addition';
    assert.strictEqual(await guard.verify(), false);
  });
});

test('W2-002: lost lease invalidates the guard even with matching composer', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    input.textContent = 'AUDIT SECOND WAVE — the automatic prompt';
    guard.captureWrite();
    leaseFor(h, KEY, 'other-tab:abc', 'other-nonce', Date.now() + 60000);
    assert.strictEqual(await guard.verify(), false);
  });
});

test('W2-002: executePreset sends automatically when ownership holds', async () => {
  const { h, api } = setup();
  const fixture = installAcceptedSend(h, composerFixture(h));
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    const promise = api.executePreset(
      { name: 'Audit Second Wave', text: 'AUDIT SECOND WAVE — automatic', forceTextDelivery: true, machineReceipt: 'r1' },
      'run',
      { autoOwnership: guard, beforeSend: async () => true }
    );
    await h.settle();
    const result = await promise;
    assert.deepStrictEqual({ ok: result.ok, sent: result.sent }, { ok: true, sent: true });
    assert.strictEqual(
      fixture._submitted.includes('ACB_CHAIN_RECEIPT: r1'),
      true,
      'the submitted payload carried the machine receipt'
    );
    assert.strictEqual(fixture.send._clickCount, 1, 'exactly one Send click');
  });
});

test('W2-002: executePreset submits the receipt with the composed payload', async () => {
  const { h, api } = setup();
  const fixture = installAcceptedSend(h, composerFixture(h));
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    const promise = api.executePreset(
      { name: 'Audit Second Wave', text: 'AUDIT SECOND WAVE — automatic', forceTextDelivery: true, machineReceipt: 'r2' },
      'run',
      { autoOwnership: guard, beforeSend: async () => true }
    );
    await h.settle();
    const result = await promise;
    assert.strictEqual(result.sent, true);
    // The accepted submission consumes the composer; what proves the receipt
    // was written is the payload ChatGPT received, not what remains behind.
    assert.ok(fixture._submitted.includes('AUDIT SECOND WAVE'));
    assert.ok(fixture._submitted.includes('ACB_CHAIN_RECEIPT: r2'));
    assert.strictEqual(api.composerPlainText(fixture.input), '', 'the accepted submission consumed the composer');
  });
});

test('W2-002: executePreset aborts with ownership-lost when composer changed before insertion', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    input.textContent = 'manual draft typed by the user';
    const promise = api.executePreset(
      { name: 'Audit Second Wave', text: 'AUDIT SECOND WAVE — automatic', forceTextDelivery: true, machineReceipt: 'r3' },
      'run',
      { autoOwnership: guard, beforeSend: async () => true }
    );
    await h.settle();
    const result = await promise;
    assert.strictEqual(result.reason, 'ownership-lost');
    assert.strictEqual(result.sent, false);
    assert.strictEqual(api.composerPlainText(input), 'manual draft typed by the user');
  });
});

test('W2-002: executePreset aborts when lease is stolen before insertion', async () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  api.autoRuntime.enabled = true;
  await withToken(h, api, async token => {
    const guard = api.createAutoSendOwnershipGuard(token, api.chatGPTComposerStateSnapshot());
    const promise = api.executePreset(
      { name: 'Audit Second Wave', text: 'AUDIT SECOND WAVE — automatic', forceTextDelivery: true, machineReceipt: 'r4' },
      'run',
      {
        autoOwnership: guard,
        beforeSend: async () => {
          leaseFor(h, KEY, 'other-tab:abc', 'other-nonce', Date.now() + 60000);
          return true;
        }
      }
    );
    await h.settle();
    const result = await promise;
    assert.strictEqual(result.reason, 'ownership-lost');
    assert.strictEqual(result.sent, false);
  });
});

test('W2-002: triggerSend fence blocks the click without side effects', async () => {
  const { h, api } = setup();
  const { send } = composerFixture(h);
  const site = api.detectSite();
  const input = site.getInput();
  assert.ok(input);
  const result = await api.triggerSend(site, input, {
    fence: async () => false
  });
  assert.strictEqual(result.mode, 'ownership-lost');
  assert.strictEqual(result.ok, false);
  assert.strictEqual(send._clicked, undefined);
});

test('W2-002: triggerSend succeeds when ChatGPT verifies the accepted Send', async () => {
  const { h, api } = setup();
  const fixture = installAcceptedSend(h, composerFixture(h));
  fixture.input.textContent = 'manual send payload';

  const site = api.detectSite();
  const input = site.getInput();
  assert.ok(input);

  const promise = api.triggerSend(site, input, {});
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.mode, 'button');
  assert.strictEqual(fixture.send._clickCount, 1);
  assert.strictEqual(fixture._submitted, 'manual send payload');
});

test('W2-002: triggerSend reports click-unverified when the Send click is not accepted', async () => {
  const { h, api } = setup();
  const { send, input } = composerFixture(h);
  input.textContent = 'manual send payload';

  const site = api.detectSite();
  const promise = api.triggerSend(site, site.getInput(), {});
  await h.settle();
  const result = await promise;

  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.mode, 'click-unverified');
  assert.strictEqual(send._clickCount, 1);
  assert.strictEqual(api.composerPlainText(input), 'manual send payload');
});
test('T79: the ownership guard survives ChatGPT relabelling our own attachment', () => {
  const { h, api } = setup();
  const prepared = {
    rootId: 'composer-a',
    text: 'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    tiles: ['uploading project.zip'],
    generating: false
  };

  // ChatGPT finishes the upload and rewrites the tile label, and can swap the
  // composer subtree. Neither is foreign activity.
  const settled = {
    rootId: 'composer-b',
    text: 'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
    tiles: ['project.zip'],
    generating: false
  };
  assert.strictEqual(api.composerStateOwnedBySameWrite(prepared, settled), true);

  // A foreign draft is still refused.
  assert.strictEqual(api.composerStateOwnedBySameWrite(prepared, {
    ...settled,
    text: 'AUDIT CORE — wave 1/3 of Quick 3 Waves. and a human typed here'
  }), false);

  // A second attachment appearing is still refused.
  assert.strictEqual(api.composerStateOwnedBySameWrite(prepared, {
    ...settled,
    tiles: ['project.zip', 'someone-elses.pdf']
  }), false);

  // Generation starting is still refused.
  assert.strictEqual(api.composerStateOwnedBySameWrite(prepared, {
    ...settled,
    generating: true
  }), false);
});
