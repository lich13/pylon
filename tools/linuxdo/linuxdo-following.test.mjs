import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const script = await readFile(new URL('./linuxdo-following.user.js', import.meta.url), 'utf8');

function harness({owner = 'fixture_owner', users = [], currentPath, failPath = '', sessionText, usersText} = {}) {
  const requests = [];
  const clipboard = [];
  const downloads = [];
  const objectUrls = new Map();
  const callbacks = [];
  const button = {
    id: '', type: '', className: '', textContent: '复制关注名单', disabled: false,
    addEventListener(name, callback) { callbacks.push([name, callback]); },
  };
  const target = { prepend(value) { this.button = value; } };
  const document = {
    body: {},
    getElementById(id) { return id === button.id ? button : null; },
    querySelector() { return target; },
    createElement(name) {
      if (name === 'button') return button;
      return {
        href: '', download: '',
        click() { downloads.push({href: this.href, download: this.download, blob: objectUrls.get(this.href)}); },
      };
    },
  };
  const response = text => ({
    ok: true,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'application/json; charset=utf-8' : null},
    async text() { return text; },
  });
  const fetch = async path => {
    requests.push(path);
    if (path === failPath) throw new Error('fixture interruption');
    if (path === '/session/current.json') {
      return response(sessionText ?? JSON.stringify({current_user: {username: owner}}));
    }
    if (path.endsWith('/following.json')) {
      return response(usersText ?? JSON.stringify(users));
    }
    throw new Error(`unexpected fixture path: ${path}`);
  };
  const URL = {
    createObjectURL(blob) {
      const url = `blob:fixture/${objectUrls.size + 1}`;
      objectUrls.set(url, blob);
      return url;
    },
    revokeObjectURL() {},
  };
  const context = {
    AbortSignal, Blob, Date, JSON, URL, document, fetch,
    location: {pathname: currentPath ?? `/u/${owner}/follow/following`},
    GM_setClipboard(value) { clipboard.push(value); },
    MutationObserver: class { observe() {} },
    setTimeout(callback) { callbacks.push(['timeout', callback]); return 1; },
  };
  vm.runInNewContext(script, context, {filename: 'linuxdo-following.user.js'});
  return {
    button, clipboard, downloads, requests,
    async click() {
      const handler = callbacks.find(([name]) => name === 'click')?.[1];
      assert.ok(handler, 'export button should be mounted');
      await handler();
    },
  };
}

test('complete empty and populated arrays export owner-bound v2 snapshots', async () => {
  for (const users of [[], [{username: 'Fixture_B'}, {username: 'fixture_a'}, {username: 'fixture_b'}]]) {
    const page = harness({users});
    await page.click();

    assert.equal(page.clipboard.length, 1);
    assert.deepEqual(page.requests, ['/session/current.json', '/u/fixture_owner/follow/following.json']);
    const prefix = '/linuxdo_following_import ';
    assert.ok(page.clipboard[0].startsWith(prefix));
    const snapshot = JSON.parse(page.clipboard[0].slice(prefix.length));
    assert.equal(snapshot.version, 2);
    assert.equal(snapshot.complete, true);
    assert.equal(snapshot.owner, 'fixture_owner');
    assert.deepEqual(snapshot.usernames, users.length ? ['fixture_a', 'fixture_b'] : []);
    assert.equal(page.downloads.length, 0);
    assert.equal(page.button.disabled, false);
  }
});

test('long complete lists download JSON instead of copying a truncated command', async () => {
  const users = Array.from({length: 500}, (_, index) => ({username: `fixture_user_${String(index).padStart(4, '0')}`}));
  const page = harness({users});
  await page.click();

  assert.equal(page.clipboard.length, 0);
  assert.equal(page.downloads.length, 1);
  assert.equal(page.downloads[0].download, 'linuxdo-following.json');
  const snapshot = JSON.parse(await page.downloads[0].blob.text());
  assert.equal(snapshot.version, 2);
  assert.equal(snapshot.complete, true);
  assert.equal(snapshot.owner, 'fixture_owner');
  assert.equal(snapshot.usernames.length, 500);
  assert.equal(snapshot.usernames[0], 'fixture_user_0000');
});

test('wrong owner, paged response, oversized response, and interrupted download fail closed', async t => {
  const invalid = [
    ['wrong owner', {sessionText: JSON.stringify({current_user: {username: 'fixture_someone_else'}})}],
    ['paged object', {usersText: JSON.stringify({users: [{username: 'fixture_user'}], has_more: true})}],
    ['oversized response', {usersText: ' '.repeat(2_000_001)}],
    ['interrupted download', {failPath: '/u/fixture_owner/follow/following.json'}],
  ];
  for (const [name, options] of invalid) {
    await t.test(name, async () => {
      const page = harness(options);
      await page.click();
      assert.deepEqual(page.clipboard, []);
      assert.deepEqual(page.downloads, []);
      assert.equal(page.button.disabled, false);
      assert.notEqual(page.button.textContent, '复制关注名单');
    });
  }
});
