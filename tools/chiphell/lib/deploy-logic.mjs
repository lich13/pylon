export const FRPC_IMAGE_TAG = 'chiphell-frpc:20260713';
export const ADMIN_IMAGE_TAG = 'chiphell-proxy-admin:20260713';


export function findImageByTag(images, tag) {
  return (images || []).find(image => (image.RepoTags || []).includes(tag)) || {};
}


export function findContainerByName(containers, name) {
  return (containers || []).find(container => (container.Names || []).includes(`/${name}`)) || {};
}


export function redactSecrets(value, secrets = []) {
  let output = String(value || '');
  for (const secret of secrets) {
    if (secret) output = output.replaceAll(String(secret), '[REDACTED]');
  }
  output = output.replace(/(Bearer\s+)[A-Za-z0-9._~-]+/gi, '$1[REDACTED]');
  output = output.replace(/(auth\.token\s*=\s*")[^"]+("?)/gi, '$1[REDACTED]$2');
  return output;
}


async function ignoreFailure(action) {
  try {
    await action();
  } catch {
    // Rollback is best effort so later dedicated artifacts are still removed.
  }
}


export async function removeRouterArtifacts(client) {
  const containers = await client.api('/api/container');
  for (const name of ['frpc-chiphell', 'chiphell-proxy-admin']) {
    const container = findContainerByName(containers, name);
    if (container.Id) {
      await ignoreFailure(() => client.removeContainer(container.Id, {volume: false, link: false, force: true}));
    }
  }
  await ignoreFailure(() => client.removeVolume('chiphell-frp-config', true));
  const images = await client.api('/api/image');
  for (const tag of [FRPC_IMAGE_TAG, ADMIN_IMAGE_TAG]) {
    const image = findImageByTag(images, tag);
    if (image.Id) await ignoreFailure(() => client.removeImage(image.Id, false));
  }
}
