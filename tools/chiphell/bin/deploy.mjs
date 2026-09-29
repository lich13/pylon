#!/usr/bin/env node
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {
  buildFrpcEnvFile,
  buildFrpsConfig,
  buildSimpleDockerAdminPayload,
} from '../lib/config.mjs';
import {
  ADMIN_IMAGE_TAG,
  findContainerByName,
  findImageByTag,
  FRPC_IMAGE_TAG,
  redactSecrets,
  removeRouterArtifacts,
} from '../lib/deploy-logic.mjs';
import {runProcess} from '../lib/process.mjs';
import {SimpleDockerClient} from '../lib/simpledocker.mjs';


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CLOUD_HOST = process.env.CHIPHELL_CLOUD_HOST || 'tencent-cloud';
const CLOUD_ADDR = process.env.CHIPHELL_CLOUD_ADDR || '203.0.113.20';
const REMOTE_SOURCE = '/tmp/chiphell-frp-proxy-build';
const REMOTE_ARTIFACTS = '/tmp/chiphell-frp-proxy-artifacts';
const REMOTE_INSTALL = '/tmp/chiphell-install-frps.sh';


async function assertCronPaused() {
  const code = [
    'import subprocess',
    "units=['pylon-job-63c6ab6c4af3.timer','chiphell-fid26-poller.service','pylon-task@63c6ab6c4af3.service','pylon-manual@63c6ab6c4af3.service']",
    "states=[subprocess.check_output(['systemctl','show',u,'-p','ActiveState','--value'],text=True).strip() for u in units]",
    "assert all(s not in ('active','activating') for s in states), 'Pause pylon Chiphell and wait for active tasks'",
    "print('CHIPHELL_PAUSED')",
  ].join(';');
  await runProcess('ssh', [CLOUD_HOST, 'sudo', 'python3', '-'], {input: code});
}


async function buildRouterImages(localArtifacts) {
  await runProcess('ssh', [CLOUD_HOST, `rm -rf -- ${REMOTE_SOURCE} ${REMOTE_ARTIFACTS} && mkdir -p ${REMOTE_SOURCE}/router`]);
  await runProcess('rsync', ['-a', `${ROOT}/router/`, `${CLOUD_HOST}:${REMOTE_SOURCE}/router/`]);
  await runProcess('rsync', ['-a', `${ROOT}/cloud/build-router-images.sh`, `${CLOUD_HOST}:${REMOTE_SOURCE}/build-router-images.sh`]);
  await runProcess('ssh', [CLOUD_HOST, 'sudo', 'bash', `${REMOTE_SOURCE}/build-router-images.sh`, REMOTE_SOURCE, REMOTE_ARTIFACTS]);
  for (const name of [
    'chiphell-frpc-20260713-arm64.tar',
    'chiphell-frpc-20260713-arm64.tar.sha256',
    'chiphell-proxy-admin-20260713-arm64.tar',
    'chiphell-proxy-admin-20260713-arm64.tar.sha256',
  ]) {
    await runProcess('scp', [`${CLOUD_HOST}:${REMOTE_ARTIFACTS}/${name}`, path.join(localArtifacts, name)]);
  }
}


async function installCloudFrps(frpsConfig, token) {
  await runProcess('rsync', ['-a', `${ROOT}/cloud/install-frps.sh`, `${CLOUD_HOST}:${REMOTE_INSTALL}`]);
  await runProcess('ssh', [CLOUD_HOST, 'sudo', 'bash', REMOTE_INSTALL], {
    input: frpsConfig,
    secrets: [token],
  });
  try {
    await runProcess('nc', ['-zv', '-w', '5', CLOUD_ADDR, '7000']);
  } catch (error) {
    throw new Error(`腾讯云安全组或公网链路未放行 ${CLOUD_ADDR}:7000：${error.message}`);
  }
}


async function installRouterFrpc(client, frpcEnvFile, token) {
  const images = await client.api('/api/image');
  const adminImage = findImageByTag(images, ADMIN_IMAGE_TAG);
  if (!adminImage.Id) throw new Error(`BE7000 缺少镜像 ${ADMIN_IMAGE_TAG}`);
  await client.createComplexContainer(buildSimpleDockerAdminPayload({
    imageId: adminImage.Id,
    imageTag: ADMIN_IMAGE_TAG,
  }));
  const result = await client.runTerminal({
    containerName: 'chiphell-proxy-admin',
    command: `admin-entrypoint apply ${FRPC_IMAGE_TAG}`,
    stdin: Buffer.from(frpcEnvFile),
    timeoutMs: 120000,
  });
  if (result.exitCode !== 0) {
    const detail = redactSecrets(result.output, [token]).trim().slice(-4000);
    throw new Error(`BE7000 admin apply failed with exit ${result.exitCode}: ${detail || 'no admin output'}`);
  }

  const containers = await client.api('/api/container');
  const adminContainer = findContainerByName(containers, 'chiphell-proxy-admin');
  if (adminContainer.Id) {
    await client.removeContainer(adminContainer.Id, {volume: false, link: false, force: true});
  }
  const refreshedImages = await client.api('/api/image');
  const refreshedAdmin = findImageByTag(refreshedImages, ADMIN_IMAGE_TAG);
  if (refreshedAdmin.Id) await client.removeImage(refreshedAdmin.Id, false);

  const refreshedContainers = await client.api('/api/container');
  const frpc = findContainerByName(refreshedContainers, 'frpc-chiphell');
  if (!frpc.Id || frpc.State !== 'running') {
    let diagnostic = `listState=${frpc.State || 'missing'} status=${frpc.Status || 'missing'}`;
    if (frpc.Id) {
      const infoResult = await client.api(`/api/container/${frpc.Id}/info`).catch(error => ({error: error.message}));
      const logResult = await client.api(`/api/container/${frpc.Id}/log`).catch(error => ({error: error.message}));
      const info = infoResult?.Data || infoResult;
      const logs = logResult?.Data || logResult;
      diagnostic += ` inspect=${JSON.stringify({
        Status: info?.State?.Status,
        ExitCode: info?.State?.ExitCode,
        Error: info?.State?.Error,
        OOMKilled: info?.State?.OOMKilled,
      })} logs=${typeof logs === 'string' ? logs : JSON.stringify(logs)}`;
    }
    diagnostic = redactSecrets(diagnostic, [token]).trim().slice(-4000);
    throw new Error(`BE7000 frpc-chiphell 未处于 running: ${diagnostic}`);
  }
}


async function cleanupBuildArtifacts(localArtifacts) {
  await rm(localArtifacts, {recursive: true, force: true});
  await runProcess('ssh', [CLOUD_HOST, `sudo docker image rm ${FRPC_IMAGE_TAG} ${ADMIN_IMAGE_TAG} >/dev/null 2>&1 || true; sudo rm -rf -- ${REMOTE_SOURCE} ${REMOTE_ARTIFACTS} ${REMOTE_INSTALL}`], {quiet: true});
}


async function rollback(client, localArtifacts) {
  await removeRouterArtifacts(client).catch(() => {});
  const uninstallScript = await readFile(path.join(ROOT, 'cloud/uninstall-frps.sh'));
  await runProcess('ssh', [CLOUD_HOST, 'sudo', 'bash', '-s'], {input: uninstallScript, quiet: true}).catch(() => {});
  await cleanupBuildArtifacts(localArtifacts).catch(() => {});
}


async function main() {
  const localArtifacts = await mkdtemp(path.join(os.tmpdir(), 'chiphell-frp-proxy-'));
  const client = new SimpleDockerClient();
  let token = '';
  try {
    console.log('确认 pylon Chiphell 已暂停');
    await assertCronPaused();
    await client.login();
    await removeRouterArtifacts(client);

    token = (await runProcess('openssl', ['rand', '-hex', '32'], {quiet: true})).stdout.trim();
    const frpsConfig = buildFrpsConfig(token);
    const frpcEnvFile = buildFrpcEnvFile({serverAddr: CLOUD_ADDR, token});

    console.log('在腾讯云交叉构建并测试 ARM64 镜像');
    await buildRouterImages(localArtifacts);
    console.log('校验摘要并导入 BE7000');
    for (const name of ['chiphell-frpc-20260713-arm64.tar', 'chiphell-proxy-admin-20260713-arm64.tar']) {
      const imported = await client.importImage(path.join(localArtifacts, name));
      console.log(`IMAGE_IMPORT_OK ${name} size=${imported.size} sha256=${imported.sha256}`);
    }

    console.log('安装腾讯云 frps 并验证公网 7000');
    await installCloudFrps(frpsConfig, token);
    console.log('通过临时 admin 创建加固 frpc');
    await installRouterFrpc(client, frpcEnvFile, token);
    await cleanupBuildArtifacts(localArtifacts);
    token = '';
    console.log('PROXY_DEPLOY_OK');
  } catch (error) {
    console.error(`PROXY_DEPLOY_FAILED: ${error.message}`);
    await rollback(client, localArtifacts);
    token = '';
    process.exitCode = 1;
  }
}


await main();
