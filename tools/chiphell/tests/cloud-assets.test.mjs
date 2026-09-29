import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';


const ROOT = new URL('../', import.meta.url);


async function read(relative) {
  return readFile(new URL(relative, ROOT), 'utf8');
}


test('cloud installer pins frps and applies the network and resource boundary', async () => {
  const script = await read('cloud/install-frps.sh');
  assert.match(script, /fatedier\/frps@sha256:03d64743945bb5fe439624ba0d057aeb43469dfed54f8df21849d0bc2c677f27/);
  assert.match(script, /FRPS_INSTALL_FAILED step=/);
  assert.match(script, /for attempt in \{1\.\.20\}/);
  assert.match(script, /test "\$listener_ready" -eq 1/);
  for (const required of [
    '--network host',
    '--memory 64m',
    '--pids-limit 64',
    '--read-only',
    '--cap-drop ALL',
    '--security-opt no-new-privileges:true',
    '/srv/frp-chiphell/frps.toml:/etc/frp/frps.toml:ro',
    'ufw allow 7000/tcp',
  ]) assert.ok(script.includes(required), `missing ${required}`);
  assert.doesNotMatch(script, /ufw allow 18080|--publish|^\s*webServer\./m);
});


test('cloud uninstaller removes only the dedicated service and firewall rule', async () => {
  const script = await read('cloud/uninstall-frps.sh');
  assert.match(script, /docker rm -f frps-chiphell/);
  assert.match(script, /ufw --force delete allow 7000\/tcp/);
  assert.doesNotMatch(script, /docker system prune|ufw reset/);
});


test('router build script targets arm64, tests images, and emits digests', async () => {
  const script = await read('cloud/build-router-images.sh');
  assert.match(script, /--platform linux\/arm64/);
  assert.match(script, /chiphell-frpc:20260713/);
  assert.match(script, /chiphell-proxy-admin:20260713/);
  assert.match(script, /--version/);
  assert.match(script, /verify -c \/config\/frpc\.toml/);
  assert.match(script, /FRP_SERVER_ADDR/);
  assert.match(script, /FRP_TOKEN/);
  assert.match(script, /--read-only/);
  assert.match(script, /--cap-drop ALL/);
  assert.match(script, /--security-opt no-new-privileges:true/);
  assert.match(script, /healthcheck/);
  assert.match(script, /sha256sum/);
});


for (const [entry, target] of [
  ['deploy.command', 'bin/deploy.mjs'],
  ['status.command', 'bin/status.mjs'],
  ['sync-brave-cookies.command', 'bin/sync-brave-cookies.mjs'],
  ['uninstall.command', 'bin/uninstall.mjs'],
]) {
  test(`${entry} is a strict double-click wrapper for ${target}`, async () => {
    const script = await read(entry);
    assert.match(script, /^#!\/bin\/bash/);
    assert.match(script, /set -euo pipefail/);
    assert.ok(script.includes(target));
    assert.doesNotMatch(script, /FRP_TOKEN|auth\.token|Bearer /);
  });
}
