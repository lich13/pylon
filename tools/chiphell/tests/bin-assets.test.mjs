import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';


const ROOT = new URL('../', import.meta.url);


async function read(relative) {
  return readFile(new URL(relative, ROOT), 'utf8');
}


test('deploy orchestrates secure config delivery and scoped rollback', async () => {
  const script = await read('bin/deploy.mjs');
  assert.match(script, /openssl.*rand.*-hex.*32/s);
  assert.match(script, /buildFrpsConfig/);
  assert.match(script, /buildFrpcEnvFile/);
  assert.match(script, /install-frps\.sh/);
  assert.match(script, /input:\s*frpsConfig/);
  assert.match(script, /runTerminal\([\s\S]*stdin:\s*Buffer\.from\(frpcEnvFile\)/);
  assert.match(script, /redactSecrets\(result\.output, \[token\]\)/);
  assert.match(script, /\/api\/container\/\$\{frpc\.Id\}\/info/);
  assert.match(script, /\/api\/container\/\$\{frpc\.Id\}\/log/);
  assert.match(script, /redactSecrets\([\s\S]*diagnostic[\s\S]*\[token\]\)/);
  assert.match(script, /removeRouterArtifacts/);
  assert.match(script, /uninstall-frps\.sh/);
  assert.doesNotMatch(script, /writeFile[\s\S]{0,120}(token|frpsConfig|frpcEnvFile)/);
});


test('deploy sends the cron assertion through ssh stdin', async () => {
  const script = await read('bin/deploy.mjs');
  assert.match(
    script,
    /runProcess\('ssh', \[CLOUD_HOST, 'sudo', 'python3', '-'\], \{input: code\}\)/,
  );
  assert.doesNotMatch(script, /\[CLOUD_HOST, 'sudo', 'python3', '-c', code\]/);
  assert.match(script, /pylon-job-63c6ab6c4af3\.timer/);
  assert.match(script, /ActiveState/);
});


test('status reads dedicated router and cloud state without mutation', async () => {
  const script = await read('bin/status.mjs');
  assert.match(script, /frpc-chiphell/);
  assert.match(script, /frps-chiphell/);
  assert.match(script, /127\.0\.0\.1:18080/);
  assert.match(script, /ip\.3322\.net/);
  assert.match(script, /checkip\.amazonaws\.com/);
  assert.doesNotMatch(script, /api\.ipify\.org/);
  assert.match(script, /ReadonlyRootfs/);
  assert.match(script, /CapDrop/);
  assert.match(script, /SecurityOpt/);
  assert.match(script, /PidsLimit/);
  assert.doesNotMatch(script, /removeRouterArtifacts|uninstall-frps|docker rm|ufw delete/);
});


test('uninstall uses only the scoped router and cloud removers', async () => {
  const script = await read('bin/uninstall.mjs');
  assert.match(script, /removeRouterArtifacts/);
  assert.match(script, /uninstall-frps\.sh/);
  assert.doesNotMatch(script, /system prune|volume prune|image prune|ufw reset/);
});


test('cookie sync reads only Chiphell Brave rows and sends payload through ssh stdin', async () => {
  const script = await read('bin/sync-brave-cookies.mjs');
  const entrypoint = await read('sync-brave-cookies.command');
  assert.match(script, /Brave Safe Storage/);
  assert.match(script, /\.timeout 5000/);
  assert.match(script, /host_key LIKE '%\.chiphell\.com'/);
  assert.match(script, /CAST\(\(SELECT value FROM meta WHERE key = 'version'/);
  assert.match(script, /decryptBraveCookie/);
  assert.match(script, /toPlaywrightCookies/);
  assert.match(script, /captureStableCookies/);
  assert.match(script, /maxAttempts:\s*3/);
  assert.match(script, /serializeCookiePayload/);
  assert.match(script, /chiphell-proxyctl.*import-cookies/s);
  assert.match(script, /input:\s*payload/);
  assert.match(script, /quiet:\s*true/);
  assert.match(script, /summarizeCookies/);
  for (const code of [
    'LOCAL_COOKIE_UNSTABLE',
    'LOCAL_COOKIE_EXPORT_FAILED',
    'REMOTE_LOGIN_REQUIRED',
    'REMOTE_VALIDATION_FAILED',
    'COOKIE_IMPORT_OK',
    'COOKIE_IMPORT_OK_RECOVERY_PENDING',
  ]) assert.ok(script.includes(code), `missing result code ${code}`);
  assert.match(entrypoint, /if \/usr\/bin\/env node "\$ROOT\/bin\/sync-brave-cookies\.mjs"/);
  assert.match(entrypoint, /登录态同步失败/);
  assert.match(entrypoint, /read -r -p/);
  assert.doesNotMatch(script, /mkdtemp|writeFile|\.backup/);
  assert.doesNotMatch(script, /launchd|clipboard|pbcopy/);
});
