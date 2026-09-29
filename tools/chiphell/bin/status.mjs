#!/usr/bin/env node
import {findContainerByName, findImageByTag, FRPC_IMAGE_TAG} from '../lib/deploy-logic.mjs';
import {runProcess} from '../lib/process.mjs';
import {SimpleDockerClient} from '../lib/simpledocker.mjs';


const CLOUD_HOST = process.env.CHIPHELL_CLOUD_HOST || 'tencent-cloud';


async function main() {
  const client = new SimpleDockerClient();
  await client.login();
  const [containers, images] = await Promise.all([
    client.api('/api/container'),
    client.api('/api/image'),
  ]);
  const frpc = findContainerByName(containers, 'frpc-chiphell');
  const frpcImage = findImageByTag(images, FRPC_IMAGE_TAG);
  console.log(`BE7000 frpc-chiphell=${frpc.State || 'missing'} image=${frpcImage.Id ? 'present' : 'missing'}`);
  let frpcHardened = false;
  if (frpc.Id) {
    const infoResult = await client.api(`/api/container/${frpc.Id}/info`);
    const info = infoResult.Data || infoResult;
    const host = info.HostConfig || {};
    const mounts = info.Mounts || [];
    const portCount = Object.keys(host.PortBindings || {}).length;
    const capDrop = host.CapDrop || [];
    const securityOpt = host.SecurityOpt || [];
    console.log([
      `BE7000 network=${host.NetworkMode}`,
      `memory=${host.Memory}`,
      `cpuset=${host.CpusetCpus}`,
      `pids=${host.PidsLimit}`,
      `readonly=${host.ReadonlyRootfs}`,
      `capDrop=${capDrop.join(',') || 'none'}`,
      `nnp=${securityOpt.includes('no-new-privileges:true')}`,
      `mounts=${mounts.length}`,
      `ports=${portCount}`,
      `restarts=${info.RestartCount}`,
    ].join(' '));
    frpcHardened = host.NetworkMode === 'host' &&
      host.Memory === 64 * 1024 * 1024 &&
      host.CpusetCpus === '0' &&
      host.PidsLimit === 64 &&
      host.ReadonlyRootfs === true &&
      capDrop.length === 1 && capDrop[0] === 'ALL' &&
      securityOpt.includes('no-new-privileges:true') &&
      mounts.length === 0 && portCount === 0 &&
      host.RestartPolicy?.Name === 'unless-stopped';
  }

  const remote = [
    "sudo docker inspect frps-chiphell --format 'frps={{.State.Status}} restarts={{.RestartCount}} memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}'",
    "ss -lnt | rg ':(7000|18080)\\b'",
    "sudo ufw status | rg '7000/tcp'",
    "proxy_ip=''; proxy_endpoint=''",
    "for endpoint in https://ip.3322.net https://checkip.amazonaws.com https://api-ipv4.ip.sb/ip; do candidate=$(curl -4 --fail --silent --show-error --socks5-hostname 127.0.0.1:18080 --max-time 15 \"$endpoint\") || continue; candidate=$(printf '%s' \"$candidate\" | tr -d '\\r\\n '); if printf '%s\\n' \"$candidate\" | rg -q '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$'; then proxy_ip=$candidate; proxy_endpoint=$endpoint; break; fi; done",
    "test -n \"$proxy_ip\"",
    "printf 'proxy_egress_ip=%s endpoint=%s\\n' \"$proxy_ip\" \"$proxy_endpoint\"",
  ].join('; ');
  await runProcess('ssh', [CLOUD_HOST, remote]);
  if (!frpc.Id || frpc.State !== 'running' || !frpcImage.Id || !frpcHardened) process.exitCode = 1;
}


await main();
