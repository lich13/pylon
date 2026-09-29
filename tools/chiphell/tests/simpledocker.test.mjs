import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildImportCurlArgs,
  buildTerminalUrl,
  parseExpectedSha256,
  SimpleDockerClient,
  terminalCommandWithMarker,
} from '../lib/simpledocker.mjs';


test('image digest parser accepts standard sha256 files only', () => {
  const digest = 'b'.repeat(64);
  assert.equal(parseExpectedSha256(`${digest}  image.tar\n`), digest);
  assert.throws(() => parseExpectedSha256('not-a-digest'), /invalid SHA-256/);
});


test('image import keeps bearer credentials out of curl argv', () => {
  const args = buildImportCurlArgs({base: 'http://192.168.3.1:9001/', tarPath: '/tmp/frpc.tar'});
  assert.ok(args.includes('--config'));
  assert.ok(args.includes('file=@/tmp/frpc.tar'));
  assert.equal(args.at(-1), 'http://192.168.3.1:9001/api/image/import');
  assert.equal(args.join(' ').includes('Bearer'), false);
});


test('terminal websocket URL encodes identifiers and token', () => {
  assert.equal(
    buildTerminalUrl('http://192.168.3.1:9001', 'exec id', 'container/id', 'Bearer secret token'),
    'ws://192.168.3.1:9001/ws/api/container/terminal/exec%20id?containerId=container%2Fid&token=Bearer%20secret%20token',
  );
});


test('terminal wrapper reads secret payload from stdin instead of command text', () => {
  const wrapped = terminalCommandWithMarker('admin-entrypoint apply', 'marker-1', {stdinBytes: 128});
  assert.match(wrapped, /stty -echo/);
  assert.match(wrapped, /head -c 128/);
  assert.match(wrapped, /admin-entrypoint apply/);
  assert.match(wrapped, /marker-1:%s/);
  assert.equal(wrapped.includes('secret-token'), false);
});


test('complex container creation posts an exact JSON payload', async () => {
  const client = new SimpleDockerClient();
  const calls = [];
  client.api = async (route, options) => {
    calls.push({route, options});
    return {Code: 'OK', Data: 'container-id'};
  };
  const payload = {containerName: 'chiphell-proxy-admin', imageTag: 'admin:test'};
  assert.equal(await client.createComplexContainer(payload), 'container-id');
  assert.equal(calls[0].route, '/api/container/run/complex');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
});


test('dedicated removal methods never call a global prune endpoint', async () => {
  const client = new SimpleDockerClient();
  const routes = [];
  client.api = async route => {
    routes.push(route);
    return {Code: 'OK'};
  };
  await client.removeContainer('container/id', {volume: false, link: false, force: true});
  await client.removeImage('sha256:image/id', false);
  await client.removeVolume('chiphell-frp-config', false);
  assert.deepEqual(routes, [
    '/api/container/container%2Fid/delete?volume=false&link=false&force=true',
    '/api/image/image%2Fid/remove/false',
    '/api/volume/chiphell-frp-config/delete/false',
  ]);
  assert.equal(routes.some(route => route.includes('prune')), false);
});
