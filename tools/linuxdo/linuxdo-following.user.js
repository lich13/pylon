// ==UserScript==
// @name         Linux.do 关注名单导出
// @namespace    pylon.linuxdo.following
// @version      1.0.0
// @description  将自己的完整关注名单复制给 Pylon
// @match        https://linux.do/*
// @grant        GM_setClipboard
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';
  const route = /^\/u\/([^/]+)\/follow\/following\/?$/;
  const validName = /^[\p{L}\p{N}_.-]{1,100}$/u;
  const buttonId = 'pylon-following-export';
  let busy = false;

  async function readJson(path) {
    const response = await fetch(path, {
      credentials: 'same-origin', headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(15000), redirect: 'error', cache: 'no-store',
    });
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('关注名单读取失败，请确认已登录后重试。');
    }
    const text = await response.text();
    if (text.length > 2_000_000) throw new Error('名单过大，未导出。');
    return JSON.parse(text);
  }

  async function exportFollowing(button) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    try {
      const match = location.pathname.match(route);
      if (!match) throw new Error('请打开自己的正在关注页面。');
      const owner = decodeURIComponent(match[1]);
      if (!validName.test(owner)) throw new Error('账号格式无效，未导出。');
      button.textContent = '正在读取完整名单…';
      const session = await readJson('/session/current.json');
      if (session.current_user?.username?.toLowerCase() !== owner.toLowerCase()) {
        throw new Error('只能导出当前登录账号自己的关注名单。');
      }
      // discourse-follow returns the complete BasicUserSerializer array, not
      // just the currently rendered cards. Unknown response shapes fail closed.
      const users = await readJson(`/u/${encodeURIComponent(owner)}/follow/following.json`);
      if (!Array.isArray(users) || users.length > 10000 || users.some(user =>
        !user || typeof user.username !== 'string' || !validName.test(user.username))) {
        throw new Error('无法确认名单完整，未导出。');
      }
      if (location.pathname !== match[0]) throw new Error('页面已切换，请重新导出。');
      const usernames = [...new Set(users.map(user => user.username.toLowerCase()))].sort();
      const snapshot = { version: 2, complete: true, owner: owner.toLowerCase(),
        imported_at: new Date().toISOString(), usernames };
      const json = JSON.stringify(snapshot);
      const command = `/linuxdo_following_import ${json}`;
      if (command.length <= 3800) {
        GM_setClipboard(command, 'text');
        button.textContent = `已复制 ${usernames.length} 人，粘贴给 Pylon`;
      } else {
        const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'linuxdo-following.json';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        button.textContent = `已导出 ${usernames.length} 人，将文件发给 Pylon`;
      }
    } catch (error) {
      button.textContent = error instanceof SyntaxError ? '返回内容无效，未导出。' :
        error.message.startsWith('关注') || error.message.endsWith('。') ? error.message : '读取失败，点击重试';
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  function mount() {
    const existing = document.getElementById(buttonId);
    if (!route.test(location.pathname)) { existing?.remove(); return; }
    if (existing) return;
    const target = document.querySelector('#user-content') || document.querySelector('.user-content');
    if (!target) return;
    const button = document.createElement('button');
    button.id = buttonId;
    button.type = 'button';
    button.className = 'btn btn-default';
    button.textContent = '复制关注名单';
    button.addEventListener('click', () => exportFollowing(button));
    target.prepend(button);
  }
  new MutationObserver(mount).observe(document.body, { childList: true, subtree: true });
  mount();
})();
