const TOKEN_PATTERN = /^[a-f0-9]{64}$/i;


export function validateToken(token) {
  const value = String(token || '').trim();
  if (!TOKEN_PATTERN.test(value)) {
    throw new Error('FRP token must contain exactly 64 hexadecimal characters');
  }
  return value.toLowerCase();
}


function validateServerAddr(serverAddr) {
  const value = String(serverAddr || '').trim();
  if (!/^[A-Za-z0-9.-]+$/.test(value)) {
    throw new Error('server address contains unsupported characters');
  }
  return value;
}


export function buildFrpsConfig(token) {
  const secret = validateToken(token);
  return [
    'bindAddr = "0.0.0.0"',
    'bindPort = 7000',
    'proxyBindAddr = "127.0.0.1"',
    'transport.tls.force = true',
    'auth.method = "token"',
    `auth.token = "${secret}"`,
    'allowPorts = [{ single = 18080 }]',
    '',
  ].join('\n');
}


export function buildFrpcEnvFile({serverAddr, token}) {
  const address = validateServerAddr(serverAddr);
  const secret = validateToken(token);
  return [
    `FRP_SERVER_ADDR=${address}`,
    `FRP_TOKEN=${secret}`,
    '',
  ].join('\n');
}


export function buildRouterContainerSpec(image) {
  const imageName = String(image || '').trim();
  if (!imageName) throw new Error('router image is required');
  return {
    name: 'frpc-chiphell',
    Image: imageName,
    Cmd: ['-c', '/config/frpc.toml'],
    HostConfig: {
      NetworkMode: 'host',
      Memory: 64 * 1024 * 1024,
      CpusetCpus: '0',
      PidsLimit: 64,
      ReadonlyRootfs: true,
      CapDrop: ['ALL'],
      SecurityOpt: ['no-new-privileges:true'],
      RestartPolicy: {Name: 'unless-stopped', MaximumRetryCount: 0},
      Binds: [],
      Tmpfs: {'/tmp': 'rw,noexec,nosuid,size=4194304'},
    },
  };
}


export function buildSimpleDockerAdminPayload({imageId, imageTag}) {
  const id = String(imageId || '').trim();
  const tag = String(imageTag || '').trim();
  if (!id || !tag) throw new Error('admin image id and tag are required');
  return {
    imageId: id,
    imageTag: tag,
    containerName: 'chiphell-proxy-admin',
    hostname: 'chiphell-proxy-admin',
    maxRestartCount: 1,
    cpuCoreLimit: 1,
    memoryLimit: 64,
    envList: [],
    mountDirList: [{
      type: 'dir',
      hostDir: '/var/run/docker.sock',
      containerDir: '/var/run/docker.sock',
    }],
    readonly: false,
    netMode: 'bridge',
    portMapping: [],
  };
}
