import {
  createDecipheriv,
  createHash,
  pbkdf2Sync,
  timingSafeEqual,
} from 'node:crypto';


const CHROMIUM_EPOCH_SECONDS = 11_644_473_600n;
const V10_IV = Buffer.alloc(16, 0x20);


export function decryptBraveCookie({encryptedHex, hostKey, dbVersion, password}) {
  const encrypted = Buffer.from(String(encryptedHex || ''), 'hex');
  if (encrypted.subarray(0, 3).toString() !== 'v10') {
    throw new Error('unsupported Brave cookie encryption version');
  }
  const key = pbkdf2Sync(String(password), 'saltysalt', 1003, 16, 'sha1');
  const decipher = createDecipheriv('aes-128-cbc', key, V10_IV);
  const plaintext = Buffer.concat([decipher.update(encrypted.subarray(3)), decipher.final()]);
  if (Number(dbVersion) >= 24) {
    if (plaintext.length < 32) throw new Error('Brave cookie domain hash is missing');
    const expected = createHash('sha256').update(String(hostKey)).digest();
    const actual = plaintext.subarray(0, 32);
    if (!timingSafeEqual(actual, expected)) throw new Error('Brave cookie domain hash mismatch');
    return plaintext.subarray(32).toString('utf8');
  }
  return plaintext.toString('utf8');
}


function isChiphellDomain(hostKey) {
  const domain = String(hostKey || '').replace(/^\./, '').toLowerCase();
  return domain === 'chiphell.com' || domain.endsWith('.chiphell.com');
}


function chromiumExpiryToUnix(value) {
  if (value === 0 || value === '0' || value === null || value === undefined) return -1;
  const micros = typeof value === 'bigint' ? value : BigInt(String(Math.trunc(Number(value))));
  return Number(micros / 1_000_000n - CHROMIUM_EPOCH_SECONDS);
}


function sameSiteName(value) {
  if (Number(value) === 1) return 'Lax';
  if (Number(value) === 2) return 'Strict';
  return 'None';
}


export function toPlaywrightCookies(rows, {now = Math.floor(Date.now() / 1000)} = {}) {
  const cookies = [];
  for (const row of rows || []) {
    if (!isChiphellDomain(row.host_key)) continue;
    const expires = chromiumExpiryToUnix(row.expires_utc);
    if (expires !== -1 && expires <= now) continue;
    cookies.push({
      name: String(row.name),
      value: String(row.value),
      domain: String(row.host_key),
      path: String(row.path || '/'),
      expires,
      httpOnly: Boolean(row.is_httponly),
      secure: Boolean(row.is_secure),
      sameSite: sameSiteName(row.samesite),
    });
  }
  return cookies;
}


export function summarizeCookies(cookies) {
  const values = cookies || [];
  const persistent = values.filter(cookie => cookie.expires !== -1);
  const latest = persistent.reduce((max, cookie) => Math.max(max, Number(cookie.expires) || 0), 0);
  const expiry = latest ? new Date(latest * 1000).toISOString() : 'session-only';
  return `COOKIE_EXPORT_OK count=${values.length} persistent=${persistent.length} session=${values.length - persistent.length} latest_expiry=${expiry}`;
}


export function serializeCookiePayload(cookies) {
  const ordered = [...(cookies || [])]
    .map(cookie => ({
      name: String(cookie.name),
      value: String(cookie.value),
      domain: String(cookie.domain),
      path: String(cookie.path || '/'),
      expires: Number(cookie.expires),
      httpOnly: Boolean(cookie.httpOnly),
      secure: Boolean(cookie.secure),
      sameSite: String(cookie.sameSite),
    }))
    .sort((left, right) => {
      const leftKey = `${left.domain}\u0000${left.path}\u0000${left.name}`;
      const rightKey = `${right.domain}\u0000${right.path}\u0000${right.name}`;
      return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
    });
  return JSON.stringify({cookies: ordered});
}


export function cookiePayloadFingerprint(cookies) {
  return createHash('sha256').update(serializeCookiePayload(cookies)).digest('hex');
}


export async function captureStableCookies(readSnapshot, {
  maxAttempts = 3,
  waitMs = 750,
  wait = delay => new Promise(resolve => setTimeout(resolve, delay)),
} = {}) {
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1) {
    throw new TypeError('maxAttempts must be a positive integer');
  }
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const first = await readSnapshot();
    await wait(waitMs);
    const second = await readSnapshot();
    if (cookiePayloadFingerprint(first) === cookiePayloadFingerprint(second)) {
      return {cookies: second, attempts: attempt};
    }
  }
  const error = new Error('Brave Cookie DB did not stabilize');
  error.code = 'LOCAL_COOKIE_UNSTABLE';
  throw error;
}
