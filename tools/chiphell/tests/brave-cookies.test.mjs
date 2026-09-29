import assert from 'node:assert/strict';
import test from 'node:test';
import {createCipheriv, createHash, pbkdf2Sync} from 'node:crypto';

import {
  captureStableCookies,
  cookiePayloadFingerprint,
  decryptBraveCookie,
  serializeCookiePayload,
  summarizeCookies,
  toPlaywrightCookies,
} from '../lib/brave-cookies.mjs';


const PASSWORD = 'test-safe-storage';
const IV = Buffer.alloc(16, 0x20);


function encryptV10(hostKey, value, {dbVersion = 24} = {}) {
  const key = pbkdf2Sync(PASSWORD, 'saltysalt', 1003, 16, 'sha1');
  const prefix = dbVersion >= 24 ? createHash('sha256').update(hostKey).digest() : Buffer.alloc(0);
  const cipher = createCipheriv('aes-128-cbc', key, IV);
  return Buffer.concat([Buffer.from('v10'), cipher.update(Buffer.concat([prefix, Buffer.from(value)])), cipher.final()]).toString('hex');
}


test('v10 decryption validates the Chromium v24 host hash', () => {
  const encryptedHex = encryptV10('.chiphell.com', 'cookie-value');
  assert.equal(decryptBraveCookie({encryptedHex, hostKey: '.chiphell.com', dbVersion: 24, password: PASSWORD}), 'cookie-value');
  assert.throws(
    () => decryptBraveCookie({encryptedHex, hostKey: '.evil.example', dbVersion: 24, password: PASSWORD}),
    /domain hash mismatch/,
  );
});


test('pre-v24 v10 decryption does not remove a domain hash', () => {
  const encryptedHex = encryptV10('.chiphell.com', 'legacy-value', {dbVersion: 23});
  assert.equal(decryptBraveCookie({encryptedHex, hostKey: '.chiphell.com', dbVersion: 23, password: PASSWORD}), 'legacy-value');
});


test('conversion keeps only live chiphell cookies and preserves sessions', () => {
  const now = 1_800_000_000;
  const chromeEpoch = 11_644_473_600;
  const future = Math.trunc((now + 3600 + chromeEpoch) * 1_000_000);
  const expired = Math.trunc((now - 10 + chromeEpoch) * 1_000_000);
  const rows = [
    {host_key: '.chiphell.com', name: 'persist', path: '/', expires_utc: future, is_secure: 1, is_httponly: 1, samesite: 1, value: 'a'},
    {host_key: 'www.chiphell.com', name: 'session', path: '/', expires_utc: 0, is_secure: 1, is_httponly: 0, samesite: 0, value: 'b'},
    {host_key: '.chiphell.com', name: 'expired', path: '/', expires_utc: expired, is_secure: 1, is_httponly: 0, samesite: 0, value: 'c'},
    {host_key: '.example.com', name: 'foreign', path: '/', expires_utc: future, is_secure: 1, is_httponly: 0, samesite: 0, value: 'd'},
  ];
  const cookies = toPlaywrightCookies(rows, {now});
  assert.deepEqual(cookies.map(cookie => cookie.name), ['persist', 'session']);
  assert.equal(cookies[0].expires, now + 3600);
  assert.equal(cookies[0].sameSite, 'Lax');
  assert.equal(cookies[1].expires, -1);
});


test('cookie summary exposes counts and expiry only', () => {
  const cookies = [{name: 'secret-name', value: 'secret-value', expires: 1_900_000_000}];
  const summary = summarizeCookies(cookies);
  assert.match(summary, /count=1/);
  assert.equal(summary.includes('secret-name'), false);
  assert.equal(summary.includes('secret-value'), false);
});


function cookie(name, value = `${name}-value`) {
  return {
    name,
    value,
    domain: '.chiphell.com',
    path: '/',
    expires: -1,
    httpOnly: true,
    secure: true,
    sameSite: 'None',
  };
}


test('logical cookie payload hashing ignores source row order', () => {
  const left = [cookie('second'), cookie('first')];
  const right = [cookie('first'), cookie('second')];
  assert.equal(serializeCookiePayload(left), serializeCookiePayload(right));
  assert.equal(cookiePayloadFingerprint(left), cookiePayloadFingerprint(right));
});


test('stable capture requires two consecutive equal logical snapshots', async () => {
  const snapshots = [
    [cookie('second'), cookie('first')],
    [cookie('first'), cookie('second')],
  ];
  const result = await captureStableCookies(
    async () => snapshots.shift(),
    {maxAttempts: 3, wait: async () => {}},
  );
  assert.equal(result.attempts, 1);
  assert.equal(result.cookies.length, 2);
});


test('stable capture retries changing snapshots and returns a stable pair', async () => {
  const snapshots = [
    [cookie('first', 'old')],
    [cookie('first', 'new')],
    [cookie('first', 'new')],
    [cookie('first', 'new')],
  ];
  const result = await captureStableCookies(
    async () => snapshots.shift(),
    {maxAttempts: 3, wait: async () => {}},
  );
  assert.equal(result.attempts, 2);
  assert.equal(result.cookies[0].value, 'new');
});


test('stable capture emits LOCAL_COOKIE_UNSTABLE after three changing pairs', async () => {
  let sequence = 0;
  await assert.rejects(
    captureStableCookies(
      async () => [cookie('first', `value-${sequence += 1}`)],
      {maxAttempts: 3, wait: async () => {}},
    ),
    error => error?.code === 'LOCAL_COOKIE_UNSTABLE',
  );
});
