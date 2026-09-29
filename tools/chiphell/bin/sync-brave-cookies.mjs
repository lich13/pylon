#!/usr/bin/env node
import os from 'node:os';
import path from 'node:path';

import {
  captureStableCookies,
  decryptBraveCookie,
  serializeCookiePayload,
  summarizeCookies,
  toPlaywrightCookies,
} from '../lib/brave-cookies.mjs';
import {runProcess} from '../lib/process.mjs';


const CLOUD_HOST = process.env.CHIPHELL_CLOUD_HOST || 'tencent-cloud';
const COOKIE_DB = process.env.BRAVE_COOKIE_DB || path.join(
  os.homedir(),
  'Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies',
);
const REMOTE_RESULT_CODES = new Set([
  'COOKIE_IMPORT_OK',
  'COOKIE_IMPORT_OK_RECOVERY_PENDING',
  'REMOTE_LOGIN_REQUIRED',
  'REMOTE_VALIDATION_FAILED',
]);


class SyncFailure extends Error {
  constructor(code, stage, errorClass = 'Error', safeLine = '') {
    super(code);
    this.code = code;
    this.stage = stage;
    this.errorClass = errorClass;
    this.safeLine = safeLine;
  }
}


async function sqlite(sql) {
  return (await runProcess(
    'sqlite3',
    ['-json', COOKIE_DB],
    {input: `.timeout 5000\n${sql}\n`, quiet: true},
  )).stdout.trim();
}


function parseRemoteResult(output) {
  for (const rawLine of String(output || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    const code = line.split(/\s+/, 1)[0];
    if (REMOTE_RESULT_CODES.has(code)) return {code, line};
  }
  return null;
}


async function readCookieSnapshot({password}) {
  try {
    const rowsText = await sqlite(`
      SELECT host_key,
             name,
             path,
             CAST(expires_utc AS TEXT) AS expires_utc,
             is_secure,
             is_httponly,
             samesite,
             value,
             hex(encrypted_value) AS encrypted_hex,
             CAST((SELECT value FROM meta WHERE key = 'version' LIMIT 1) AS INTEGER) AS db_version
        FROM cookies
       WHERE host_key = 'chiphell.com'
          OR host_key = '.chiphell.com'
          OR host_key LIKE '%.chiphell.com'
       ORDER BY host_key, path, name;
    `);
    const rows = rowsText ? JSON.parse(rowsText) : [];
    const decrypted = rows.map(row => ({
      ...row,
      value: row.value || decryptBraveCookie({
        encryptedHex: row.encrypted_hex,
        hostKey: row.host_key,
        dbVersion: Number(row.db_version || 0),
        password,
      }),
    }));
    const cookies = toPlaywrightCookies(decrypted);
    if (!cookies.length) {
      throw new SyncFailure('LOCAL_COOKIE_EXPORT_FAILED', 'NO_USABLE_COOKIES');
    }
    return cookies;
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    throw new SyncFailure('LOCAL_COOKIE_EXPORT_FAILED', 'COOKIE_SNAPSHOT', error?.constructor?.name || 'Error');
  }
}


async function exportStableCookies() {
  let password;
  try {
    password = (await runProcess(
      'security',
      ['find-generic-password', '-s', 'Brave Safe Storage', '-w'],
      {quiet: true},
    )).stdout.trim();
  } catch (error) {
    throw new SyncFailure('LOCAL_COOKIE_EXPORT_FAILED', 'KEYCHAIN', error?.constructor?.name || 'Error');
  }
  if (!password) throw new SyncFailure('LOCAL_COOKIE_EXPORT_FAILED', 'KEYCHAIN_EMPTY');

  try {
    return await captureStableCookies(
      () => readCookieSnapshot({password}),
      {maxAttempts: 3, waitMs: 750},
    );
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    if (error?.code === 'LOCAL_COOKIE_UNSTABLE') {
      throw new SyncFailure('LOCAL_COOKIE_UNSTABLE', 'COOKIE_SNAPSHOT');
    }
    throw new SyncFailure('LOCAL_COOKIE_EXPORT_FAILED', 'COOKIE_SNAPSHOT', error?.constructor?.name || 'Error');
  }
}


async function importCookies(cookies) {
  const payload = Buffer.from(serializeCookiePayload(cookies));
  const secrets = cookies.flatMap(cookie => [cookie.name, cookie.value]);
  try {
    const result = await runProcess(
      'ssh',
      [CLOUD_HOST, 'sudo', '/usr/local/sbin/chiphell-proxyctl', 'import-cookies'],
      {input: payload, secrets, quiet: true},
    );
    const parsed = parseRemoteResult(`${result.stdout}\n${result.stderr}`);
    if (!parsed || !parsed.code.startsWith('COOKIE_IMPORT_OK')) {
      throw new SyncFailure('REMOTE_VALIDATION_FAILED', 'REMOTE_RESULT');
    }
    return parsed.line;
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    const parsed = parseRemoteResult(`${error?.stdout || ''}\n${error?.stderr || ''}`);
    if (parsed) throw new SyncFailure(parsed.code, 'REMOTE_IMPORT', 'Error', parsed.line);
    throw new SyncFailure('REMOTE_VALIDATION_FAILED', 'SSH', error?.constructor?.name || 'Error');
  }
}


async function main() {
  const {cookies, attempts} = await exportStableCookies();
  console.log(`${summarizeCookies(cookies)} stable_attempt=${attempts}`);
  console.log(await importCookies(cookies));
}


try {
  await main();
} catch (error) {
  if (error instanceof SyncFailure && error.safeLine) {
    console.error(error.safeLine);
  } else if (error instanceof SyncFailure) {
    console.error(`${error.code} stage=${error.stage} error_class=${error.errorClass}`);
  } else {
    console.error(`LOCAL_COOKIE_EXPORT_FAILED stage=UNEXPECTED error_class=${error?.constructor?.name || 'Error'}`);
  }
  process.exitCode = 1;
}
