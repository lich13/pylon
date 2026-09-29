import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findContainerByName,
  findImageByTag,
  redactSecrets,
  removeRouterArtifacts,
} from '../lib/deploy-logic.mjs';


test('image and container lookup require exact dedicated names', () => {
  const images = [
    {Id: 'safe', RepoTags: ['simple-docker:0.0.7.3']},
    {Id: 'frpc', RepoTags: ['chiphell-frpc:20260713']},
  ];
  const containers = [
    {Id: 'safe', Names: ['/simple-docker']},
    {Id: 'frpc', Names: ['/frpc-chiphell']},
  ];
  assert.equal(findImageByTag(images, 'chiphell-frpc:20260713').Id, 'frpc');
  assert.equal(findImageByTag(images, 'chiphell-frpc').Id, undefined);
  assert.equal(findContainerByName(containers, 'frpc-chiphell').Id, 'frpc');
  assert.equal(findContainerByName(containers, 'simple').Id, undefined);
});


test('secret redaction hides token values and bearer credentials', () => {
  const token = 'a'.repeat(64);
  const output = redactSecrets(`auth.token = "${token}"\nAuthorization: Bearer abc.def.ghi`, [token]);
  assert.equal(output.includes(token), false);
  assert.equal(output.includes('abc.def.ghi'), false);
  assert.match(output, /\[REDACTED\]/);
});


test('router rollback removes only dedicated artifacts', async () => {
  const calls = [];
  const client = {
    api: async route => {
      if (route === '/api/container') return [
        {Id: 'safe-container', Names: ['/simple-docker']},
        {Id: 'frpc-container', Names: ['/frpc-chiphell']},
        {Id: 'admin-container', Names: ['/chiphell-proxy-admin']},
      ];
      if (route === '/api/image') return [
        {Id: 'safe-image', RepoTags: ['simple-docker:0.0.7.3']},
        {Id: 'frpc-image', RepoTags: ['chiphell-frpc:20260713']},
        {Id: 'admin-image', RepoTags: ['chiphell-proxy-admin:20260713']},
      ];
      throw new Error(`unexpected ${route}`);
    },
    removeContainer: async (...args) => calls.push(['container', ...args]),
    removeImage: async (...args) => calls.push(['image', ...args]),
    removeVolume: async (...args) => calls.push(['volume', ...args]),
  };
  await removeRouterArtifacts(client);
  assert.deepEqual(calls, [
    ['container', 'frpc-container', {volume: false, link: false, force: true}],
    ['container', 'admin-container', {volume: false, link: false, force: true}],
    ['volume', 'chiphell-frp-config', true],
    ['image', 'frpc-image', false],
    ['image', 'admin-image', false],
  ]);
});
