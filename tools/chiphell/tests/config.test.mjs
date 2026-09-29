import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildFrpcEnvFile,
  buildFrpsConfig,
  buildRouterContainerSpec,
  buildSimpleDockerAdminPayload,
  validateToken,
} from '../lib/config.mjs';


const TOKEN = 'a'.repeat(64);


test('validateToken accepts exactly 64 hexadecimal characters', () => {
  assert.equal(validateToken(TOKEN), TOKEN);
  for (const invalid of ['a'.repeat(63), 'a'.repeat(65), 'g'.repeat(64), '']) {
    assert.throws(() => validateToken(invalid), /64 hexadecimal/);
  }
});


test('frps config forces TLS and exposes only the approved proxy port', () => {
  const config = buildFrpsConfig(TOKEN);
  assert.match(config, /bindAddr = "0\.0\.0\.0"/);
  assert.match(config, /bindPort = 7000/);
  assert.match(config, /proxyBindAddr = "127\.0\.0\.1"/);
  assert.match(config, /transport\.tls\.force = true/);
  assert.match(config, /allowPorts = \[\{ single = 18080 \}\]/);
  assert.doesNotMatch(config, /webServer|dashboard|18081/);
  assert.match(config, new RegExp(`auth\\.token = "${TOKEN}"`));
});


test('frpc env file carries only the validated server and token', () => {
  const envFile = buildFrpcEnvFile({serverAddr: '203.0.113.20', token: TOKEN});
  assert.equal(envFile, `FRP_SERVER_ADDR=203.0.113.20\nFRP_TOKEN=${TOKEN}\n`);
  assert.doesNotMatch(envFile, /auth\.token|socks5|18080/);
});


test('router container spec is fail-closed and resource constrained', () => {
  const spec = buildRouterContainerSpec('chiphell-frpc:20260713');
  assert.equal(spec.name, 'frpc-chiphell');
  assert.equal(spec.Image, 'chiphell-frpc:20260713');
  assert.deepEqual(spec.Cmd, ['-c', '/config/frpc.toml']);
  assert.equal(spec.HostConfig.NetworkMode, 'host');
  assert.equal(spec.HostConfig.Memory, 64 * 1024 * 1024);
  assert.equal(spec.HostConfig.CpusetCpus, '0');
  assert.equal(spec.HostConfig.PidsLimit, 64);
  assert.equal(spec.HostConfig.ReadonlyRootfs, true);
  assert.deepEqual(spec.HostConfig.CapDrop, ['ALL']);
  assert.deepEqual(spec.HostConfig.SecurityOpt, ['no-new-privileges:true']);
  assert.deepEqual(spec.HostConfig.RestartPolicy, {Name: 'unless-stopped', MaximumRetryCount: 0});
  assert.deepEqual(spec.HostConfig.Binds, []);
  assert.equal(spec.HostConfig.PortBindings, undefined);
  assert.equal(JSON.stringify(spec).includes('/var/run/docker.sock'), false);
});


test('temporary admin payload is the only container allowed to mount docker socket', () => {
  const payload = buildSimpleDockerAdminPayload({
    imageId: 'sha256:1234567890abcdef',
    imageTag: 'chiphell-proxy-admin:20260713',
  });
  assert.equal(payload.containerName, 'chiphell-proxy-admin');
  assert.equal(payload.imageId, 'sha256:1234567890abcdef');
  assert.equal(payload.imageTag, 'chiphell-proxy-admin:20260713');
  assert.equal(payload.cpuCoreLimit, 1);
  assert.equal(payload.memoryLimit, 64);
  assert.equal(payload.netMode, 'bridge');
  assert.deepEqual(payload.portMapping, []);
  assert.deepEqual(payload.envList, []);
  assert.deepEqual(payload.mountDirList, [{
    type: 'dir',
    hostDir: '/var/run/docker.sock',
    containerDir: '/var/run/docker.sock',
  }]);
});
