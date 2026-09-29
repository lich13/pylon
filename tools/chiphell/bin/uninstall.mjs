#!/usr/bin/env node
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {removeRouterArtifacts} from '../lib/deploy-logic.mjs';
import {runProcess} from '../lib/process.mjs';
import {SimpleDockerClient} from '../lib/simpledocker.mjs';


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CLOUD_HOST = process.env.CHIPHELL_CLOUD_HOST || 'tencent-cloud';


async function main() {
  const client = new SimpleDockerClient();
  await client.login();
  await removeRouterArtifacts(client);
  await runProcess('ssh', [CLOUD_HOST, "if sudo test -x /usr/local/sbin/chiphell-proxyctl; then sudo /usr/local/sbin/chiphell-proxyctl pause; fi"], {quiet: true});
  const uninstallScript = await readFile(path.join(ROOT, 'cloud/uninstall-frps.sh'));
  await runProcess('ssh', [CLOUD_HOST, 'sudo', 'bash', '-s'], {input: uninstallScript});
  await runProcess('ssh', [CLOUD_HOST, 'sudo rm -rf -- /tmp/chiphell-frp-proxy-build /tmp/chiphell-frp-proxy-artifacts /tmp/chiphell-install-frps.sh'], {quiet: true});
  console.log('PROXY_UNINSTALL_OK');
}


await main();
