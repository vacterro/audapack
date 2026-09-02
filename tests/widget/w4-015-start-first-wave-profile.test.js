'use strict';

// A10 was dead on arrival. startHandoffComposerStillPrepared() demanded the
// composer classify as literally 'core', but START prepares the ACTIVE
// PROFILE's first wave -- AUDIT ARCHITECTURE under super10, COMPRESS AUDIT
// under compress, and only AUDIT CORE under quick3. So its own freshly written
// prompt read as a foreign human draft in beforeIrreversibleSend, which
// answered BLOCKED clean-state-lost and never pressed Send. Six A10 lanes
// pressed at 21:04, five BLOCKED PRE-START within one second, every composer
// left loaded and unsent; the abandoned drafts then poisoned the next A3 pass
// with worker-has-draft.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, composerFixture } = require('./helpers');

const RECEIPT = 'startcore-mtkeolpc-e947969b5e4e';
const RECEIPT_PREFIX = 'ACB_CHAIN_RECEIPT';

function prepare(api, input, { profile, body }) {
  // The dispatch applies the job's profile to state and the armed engine
  // carries it on the runtime, so a live worker has both set. getActiveProfile
  // reads the runtime first.
  api.state.auditProfile = profile;
  if (api.autoRuntime) api.autoRuntime.profileId = profile;
  input.textContent = `${body}\n${RECEIPT_PREFIX}: ${RECEIPT}`;
  return { phase: 'armed', receipt: RECEIPT };
}

const ARCHITECTURE = 'AUDIT ARCHITECTURE / SYSTEM INVARIANTS\n'
  + 'The complete command is attached as "AUDIT_ARCHITECTURE_176jhkn.md".\n'
  + 'Treat that attached file as my full instruction for this turn and execute it exactly; do not merely summarize the file.';

const CORE = 'AUDIT CORE\n'
  + 'The complete command is attached as "AUDIT_CORE_176jhkn.md".\n'
  + 'Treat that attached file as my full instruction for this turn and execute it exactly; do not merely summarize the file.';

test('W15: a super10 START is its own prepared composer, not a foreign draft', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const handoff = prepare(api, input, { profile: 'super10', body: ARCHITECTURE });

  assert.strictEqual(api.getActiveProfile().waves[0].id, 'architecture');
  assert.strictEqual(api.classifyAuditMessage(api.composerPlainText(input)), 'architecture');
  assert.strictEqual(api.startHandoffComposerStillPrepared(handoff), true);
});

test('W15: quick3 CORE still passes', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const handoff = prepare(api, input, { profile: 'quick3', body: CORE });

  assert.strictEqual(api.startHandoffComposerStillPrepared(handoff), true);
});

test('W15: compress passes although no classifier marker names its wave', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  const body = 'COMPRESS AUDIT\nThe complete command is attached as "AUDIT_COMPRESS_176jhkn.md".';
  const handoff = prepare(api, input, { profile: 'compress', body });

  assert.strictEqual(api.getActiveProfile().waves[0].id, 'compress');
  assert.strictEqual(api.classifyAuditMessage(api.composerPlainText(input)), '');
  assert.strictEqual(api.startHandoffComposerStillPrepared(handoff), true);
});

test('W15: a composer holding some OTHER wave is still refused', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  // quick3 opens on CORE, so an ARCHITECTURE prompt carrying this receipt is
  // not what START prepared -- the guard must still bite.
  const handoff = prepare(api, input, { profile: 'quick3', body: ARCHITECTURE });

  assert.strictEqual(api.startHandoffComposerStillPrepared(handoff), false);
});

test('W15: the receipt is still the identity proof', () => {
  const { h, api } = setup();
  const { input } = composerFixture(h);
  prepare(api, input, { profile: 'super10', body: ARCHITECTURE });

  assert.strictEqual(
    api.startHandoffComposerStillPrepared({ phase: 'armed', receipt: 'startcore-someone-else' }),
    false
  );
  assert.strictEqual(api.startHandoffComposerStillPrepared({ phase: 'sent', receipt: RECEIPT }), false);
});


test('W15: a handoff left armed by a pre-start BLOCK wedges the window shut', () => {
  // browserWorkerClearAbandonedDraft refuses to touch the composer while a
  // prepared handoff exists, so a BLOCK that keeps its handoff strands the
  // worker: it stays DIRTY, every later job is released with worker-has-draft,
  // and RESET ALL cannot reach it because the draft lives in the browser and
  // not in the Bridge. That is why "W 6/6" never came back and Clear cleared
  // nothing. A pre-start BLOCK sent nothing, so it must release its handoff.
  const { h, api } = setup({
    location: {
      href: 'https://chatgpt.com/?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1',
      pathname: '/',
      search: '?audapack_worker=1&audapack_worker_slot=1&audapack_worker_generation=1'
    }
  });
  const { input } = composerFixture(h);
  api.state.bridgeEnabled = true;
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false });
  api.state.auditProfile = 'super10';

  const handoff = api.armStartAuditHandoffForSend(api.beginStartAuditHandoff());
  assert.ok(handoff, 'the handoff must be armed for this to mean anything');
  assert.strictEqual(api.startHandoffIsPrepared(api.readStartAuditHandoff()), true);
  input._text = `${ARCHITECTURE}\n${RECEIPT_PREFIX}: ${handoff.receipt}`;

  assert.strictEqual(api.browserWorkerClearAbandonedDraft(), false, 'wedged while armed');

  api.clearStartAuditHandoff();
  assert.strictEqual(api.browserWorkerClearAbandonedDraft(), true, 'released once the handoff is gone');
  assert.strictEqual(String(api.composerPlainText(input)).trim(), '');
});
