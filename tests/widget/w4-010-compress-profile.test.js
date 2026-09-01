'use strict';

// CM: the widget assumed exactly two profiles in several places --
// ['super10','quick3'] validation and a `super10 ? quick3 : super10` flip --
// which made every profile beyond the second unselectable and unpersistable.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

test('W10: the embedded registry carries compress alongside the other two', () => {
  const { api } = setup();
  const profiles = api.EMBEDDED_AUDIT_PROFILES?.profiles || {};
  assert.ok(profiles.quick3, 'quick3 must remain');
  assert.ok(profiles.super10, 'super10 must remain');
  assert.ok(profiles.compress, 'compress must be embedded');
  assert.strictEqual(profiles.compress.waves.length, 1);
  assert.strictEqual(profiles.compress.waves[0].ticket_prefix, 'CMP-');
  assert.strictEqual(profiles.compress.waves[0].status_line, 'STATUS: COMPRESS: COMPLETE');
});

test('W10: compact labels come from the manifest, not a binary guess', () => {
  const { api } = setup();
  assert.strictEqual(api.profileShortLabel('quick3'), 'A3');
  assert.strictEqual(api.profileShortLabel('super10'), 'A10');
  assert.strictEqual(api.profileShortLabel('compress'), 'CM');
  assert.strictEqual(api.profileShortLabel(''), 'A3');
});

test('W10: the compact toggle cycles every profile instead of flipping two', () => {
  const { api } = setup();
  const ids = api.auditProfileIds();
  assert.ok(ids.includes('compress'), JSON.stringify(ids));

  const seen = new Set();
  let current = ids[0];
  for (let i = 0; i < ids.length; i += 1) {
    seen.add(current);
    current = api.nextAuditProfileId(current);
  }
  assert.strictEqual(seen.size, ids.length, 'every profile must be reachable');
  assert.strictEqual(current, ids[0], 'the cycle must return to where it started');
});

function storedStateWithProfile(h, api, profileId) {
  // loadState() falls back to the default state when categories are missing,
  // so a persisted profile has to ride on a real state document.
  const base = api.loadState();
  base.auditProfile = profileId;
  h.gmStore.set('ai_chatbuttons_v6', JSON.stringify(base));
  return api.loadState();
}

test('W10: compress survives a state round trip', () => {
  const { h, api } = setup();
  assert.strictEqual(storedStateWithProfile(h, api, 'compress').auditProfile, 'compress');
});

test('W10: an unknown persisted profile still falls back', () => {
  const { h, api } = setup();
  const loaded = storedStateWithProfile(h, api, 'retired-profile').auditProfile;
  assert.notStrictEqual(loaded, 'retired-profile');
  assert.ok(api.auditProfileIds().includes(loaded), loaded);
});

test('W10: a compress terminal handoff passes the widget gate', () => {
  const { api } = setup();
  const wave = api.EMBEDDED_AUDIT_PROFILES.profiles.compress.waves[0];
  const fields = wave.ticket_fields.map(f => `${f}: sample ${f.toLowerCase()}.`).join(String.fromCharCode(10));
  const body = [
    'PROJECT_NAME: AUDAPACK',
    'CAMPAIGN_PROFILE: compress',
    'CAMPAIGN_RUN_ID: acb-cm-1',
    `WAVE_ID: ${wave.id}`,
    `WAVE: ${wave.wave_header}`,
    wave.status_line,
    'TICKETS: 1',
    'HANDOFF: IMPLEMENTATION_AGENT',
    '',
    '[P1] [CMP-001] DELETE audapack/dead.py',
    fields,
    '',
    `${wave.done_marker} the deletions land and the suite stays green.`,
  ].join(String.fromCharCode(10));

  assert.strictEqual(api.responseGate(`wait-${wave.id}`, body), 'complete');
  assert.strictEqual(api.auditHandoffIntegrity(`wait-${wave.id}`, body).valid, true);
});

test('W10: a one-wave campaign has no next wave to expect', () => {
  const { api } = setup();
  const compress = api.EMBEDDED_AUDIT_PROFILES.profiles.compress;
  assert.strictEqual(compress.waves.length, 1);
  assert.strictEqual(compress.finalizer_wave_id, 'compress');
  assert.strictEqual(compress.waves[0].finalizer, true);
});
