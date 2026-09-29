import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';


const ROOT = new URL('../', import.meta.url);


async function read(relative) {
  return readFile(new URL(relative, ROOT), 'utf8');
}


test('frpc wrapper uses the approved digest and fixed config command', async () => {
  const dockerfile = await read('router/Dockerfile.frpc');
  const config = await read('router/frpc.toml');
  assert.match(dockerfile, /fatedier\/frpc@sha256:f4e8d7981ee3230b0bf2fd216316b4e7494111a7b85659feec3bd6e2d921131d/);
  assert.match(dockerfile, /RUN install -d -m 0755 \/config[\s\S]*COPY.*frpc\.toml.*\/config\/frpc\.toml/);
  assert.match(dockerfile, /COPY.*frpc\.toml.*\/config\/frpc\.toml/);
  assert.match(dockerfile, /CMD \["-c", "\/config\/frpc\.toml"\]/);
  assert.match(config, /serverAddr = "\{\{ \.Envs\.FRP_SERVER_ADDR \}\}"/);
  assert.match(config, /auth\.token = "\{\{ \.Envs\.FRP_TOKEN \}\}"/);
  assert.match(config, /remotePort = 18080/);
  assert.match(config, /type = "socks5"/);
  assert.doesNotMatch(config, /[a-f0-9]{64}/i);
});


test('admin image uses the approved docker cli digest', async () => {
  const dockerfile = await read('router/Dockerfile.admin');
  assert.match(dockerfile, /docker:20\.10\.24-cli@sha256:d1b70e7d1329eef4c9541c6cd36a0ad39cfaf1adc0119a6fb7f6668d93ae0475/);
  assert.match(dockerfile, /admin-entrypoint/);
});


test('admin apply creates a hardened frpc container and never publishes ports', async () => {
  const script = await read('router/admin-entrypoint.sh');
  for (const required of [
    '--network host',
    '--memory 64m',
    '--cpuset-cpus 0',
    '--pids-limit 64',
    '--read-only',
    '--cap-drop ALL',
    '--security-opt no-new-privileges:true',
    '--restart unless-stopped',
    '--env-file "$env_file"',
  ]) assert.ok(script.includes(required), `missing ${required}`);
  assert.equal(script.includes('--publish'), false);
  assert.doesNotMatch(script, /^\s+-p\s/m);
  assert.doesNotMatch(script, /docker volume create|chiphell-frp-config:\/config|-v\s/);
});


test('admin env writer validates stdin and removes the temporary file', async () => {
  const script = await read('router/admin-entrypoint.sh');
  assert.match(script, /umask 077/);
  assert.match(script, /cat >"?\$env_file"?/);
  assert.match(script, /chmod 600 "?\$env_file"?/);
  assert.match(script, /FRP_SERVER_ADDR/);
  assert.match(script, /FRP_TOKEN/);
  assert.match(script, /rm -f "?\$env_file"?/);
});
