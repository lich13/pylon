import {spawn} from 'node:child_process';
import {createHash, randomBytes} from 'node:crypto';
import {createReadStream} from 'node:fs';
import {readFile, stat} from 'node:fs/promises';
import path from 'node:path';


export function parseExpectedSha256(line) {
  const match = String(line || '').trim().match(/^([a-fA-F0-9]{64})(?:\s+\*?.+)?$/);
  if (!match) throw new Error('invalid SHA-256 file');
  return match[1].toLowerCase();
}


export function buildImportCurlArgs({base, tarPath}) {
  const url = `${String(base).replace(/\/$/, '')}/api/image/import`;
  return [
    '--fail-with-body',
    '--silent',
    '--show-error',
    '--max-time',
    '1800',
    '--config',
    '-',
    '-X',
    'POST',
    '-F',
    `file=@${tarPath}`,
    url,
  ];
}


export function buildTerminalUrl(base, execId, containerId, token) {
  const wsBase = String(base).replace(/\/$/, '').replace(/^http/, 'ws');
  return `${wsBase}/ws/api/container/terminal/${encodeURIComponent(execId)}` +
    `?containerId=${encodeURIComponent(containerId)}&token=${encodeURIComponent(token)}`;
}


export function terminalCommandWithMarker(command, marker, {stdinBytes = 0} = {}) {
  if (!Number.isSafeInteger(stdinBytes) || stdinBytes < 0) throw new Error('invalid stdin byte count');
  if (stdinBytes === 0) return `${command}; printf '\\n${marker}:%s\\n' $?`;
  return [
    'stty -echo',
    '__chiphell_payload=$(mktemp)',
    `head -c ${stdinBytes} >"$__chiphell_payload"`,
    'stty echo',
    `${command} <"$__chiphell_payload"`,
    '__chiphell_status=$?',
    'rm -f "$__chiphell_payload"',
    `printf '\\n${marker}:%s\\n' "$__chiphell_status"`,
  ].join('; ');
}


async function sha256File(filePath) {
  const hash = createHash('sha256');
  await new Promise((resolve, reject) => {
    const stream = createReadStream(filePath);
    stream.on('data', chunk => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', resolve);
  });
  return hash.digest('hex');
}


async function runCurl(args, config) {
  return new Promise((resolve, reject) => {
    const child = spawn('curl', args, {stdio: ['pipe', 'pipe', 'pipe']});
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.stdin.end(config);
    child.on('close', code => {
      if (code === 0) resolve(stdout);
      else reject(new Error(`image import failed: ${stderr.trim() || `curl exit ${code}`}`));
    });
  });
}


export class SimpleDockerClient {
  constructor({base = 'http://192.168.3.1:9001', username = 'admin', password = 'admin'} = {}) {
    this.base = String(base).replace(/\/$/, '');
    this.username = username;
    this.password = password;
    this.bearer = '';
  }

  async login() {
    const response = await fetch(`${this.base}/api/system/login`, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({username: this.username, password: this.password, verityCode: ''}),
    });
    if (!response.ok) throw new Error(`SimpleDocker login returned HTTP ${response.status}`);
    const result = await response.json();
    if (result.Code !== 'OK' || !result.Data) {
      throw new Error(`SimpleDocker login failed: ${result.Msg || 'unknown error'}`);
    }
    this.bearer = `Bearer ${result.Data}`;
    return this.bearer;
  }

  async api(route, options = {}) {
    if (!this.bearer) await this.login();
    const headers = {...options.headers, Authorization: this.bearer};
    const response = await fetch(`${this.base}${route}`, {...options, headers});
    if (!response.ok) throw new Error(`${route} returned HTTP ${response.status}`);
    return response.json();
  }

  async importImage(tarPath, sha256Path = `${tarPath}.sha256`) {
    const absoluteTar = path.resolve(tarPath);
    const fileInfo = await stat(absoluteTar);
    if (!fileInfo.isFile() || fileInfo.size === 0) throw new Error('image tar is missing or empty');
    const expected = parseExpectedSha256(await readFile(path.resolve(sha256Path), 'utf8'));
    const actual = await sha256File(absoluteTar);
    if (actual !== expected) throw new Error('image tar SHA-256 mismatch');
    if (!this.bearer) await this.login();
    const output = await runCurl(
      buildImportCurlArgs({base: this.base, tarPath: absoluteTar}),
      `header = "Authorization: ${this.bearer}"\n`,
    );
    const result = JSON.parse(output);
    if (result.Code !== 'OK') throw new Error(`SimpleDocker image import failed: ${result.Msg || 'unknown error'}`);
    return {sha256: actual, size: fileInfo.size, data: result.Data};
  }

  async createComplexContainer(payload) {
    const result = await this.api('/api/container/run/complex', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (result.Code !== 'OK' || !result.Data) {
      throw new Error(`SimpleDocker container creation failed: ${result.Msg || 'unknown error'}`);
    }
    return result.Data;
  }

  async removeContainer(id, {volume = false, link = false, force = false} = {}) {
    const query = new URLSearchParams({
      volume: String(Boolean(volume)),
      link: String(Boolean(link)),
      force: String(Boolean(force)),
    });
    const result = await this.api(`/api/container/${encodeURIComponent(id)}/delete?${query}`);
    if (result.Code !== 'OK') throw new Error(`SimpleDocker container removal failed: ${result.Msg || 'unknown error'}`);
  }

  async removeImage(id, force = false) {
    const normalized = String(id).replace(/^sha256:/, '');
    const result = await this.api(`/api/image/${encodeURIComponent(normalized)}/remove/${Boolean(force)}`);
    if (result.Code !== 'OK') throw new Error(`SimpleDocker image removal failed: ${result.Msg || 'unknown error'}`);
  }

  async removeVolume(name, force = false) {
    const result = await this.api(`/api/volume/${encodeURIComponent(name)}/delete/${Boolean(force)}`);
    if (result.Code !== 'OK') throw new Error(`SimpleDocker volume removal failed: ${result.Msg || 'unknown error'}`);
  }

  async runTerminal({containerName = 'simple-docker', command, stdin = Buffer.alloc(0), timeoutMs = 30000}) {
    if (!this.bearer) await this.login();
    const containers = await this.api('/api/container');
    const container = containers.find(item => (item.Names || []).includes(`/${containerName}`));
    if (!container) throw new Error(`SimpleDocker container not found: ${containerName}`);
    const execResult = await this.api(`/api/container/${container.Id}/command/exec`);
    if (execResult.Code !== 'OK') throw new Error(`container exec failed: ${execResult.Msg}`);
    const marker = `__CHIPHELL_EXIT_${randomBytes(8).toString('hex')}`;
    const payload = Buffer.isBuffer(stdin) ? stdin : Buffer.from(stdin);
    const wrapped = terminalCommandWithMarker(command, marker, {stdinBytes: payload.length});
    const url = buildTerminalUrl(this.base, execResult.Data, container.Id, this.bearer);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        socket.close();
        reject(new Error('SimpleDocker terminal timed out'));
      }, timeoutMs);
      const socket = new WebSocket(url);
      let output = '';
      socket.binaryType = 'arraybuffer';
      socket.onopen = () => {
        socket.send(`${wrapped}\n`);
        if (payload.length) socket.send(payload);
      };
      socket.onmessage = event => {
        output += typeof event.data === 'string' ? event.data : Buffer.from(event.data).toString('utf8');
        const match = output.match(new RegExp(`${marker}:(\\d+)`));
        if (!match) return;
        clearTimeout(timer);
        socket.close();
        resolve({exitCode: Number(match[1]), output: output.replace(new RegExp(`${marker}:\\d+`), '')});
      };
      socket.onerror = () => {
        clearTimeout(timer);
        reject(new Error('SimpleDocker terminal websocket failed'));
      };
    });
  }
}
