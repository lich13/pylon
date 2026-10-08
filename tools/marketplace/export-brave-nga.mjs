#!/usr/bin/env node
// Export only the requested site's cookies. Secrets never enter arguments/logs.
import {spawnSync} from 'node:child_process';
import {realpathSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {decryptBraveCookie, captureStableCookies} from '../chiphell/lib/brave-cookies.mjs';

function run(command,args,input) {
  const result=spawnSync(command,args,{input,encoding:'utf8',maxBuffer:1024*1024});
  if(result.status!==0) throw new Error(command+' failed');
  return result.stdout;
}

try {
  const destination=process.argv[2];
  if(!/^\/tmp\/pylon-marketplace\.[A-Za-z0-9]+\/nga-cookies\.json$/.test(destination||''))
    throw new Error('expected isolated analysis destination');
  const database=realpathSync(process.env.BRAVE_COOKIE_DB||path.join(os.homedir(),
    'Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies'));
  const password=run('security',['find-generic-password','-s','Brave Safe Storage','-w']).trim();
  const snapshot=async()=>{
    const rows=JSON.parse(run('sqlite3',['-readonly','-json',database],`.timeout 5000
      SELECT host_key,name,path,CAST(expires_utc AS TEXT) expires_utc,is_secure,is_httponly,samesite,value,
      hex(encrypted_value) encrypted_hex,(SELECT value FROM meta WHERE key='version') db_version
      FROM cookies WHERE host_key IN ('nga.cn','.nga.cn','bbs.nga.cn','.bbs.nga.cn') ORDER BY host_key,path,name;
    `)||'[]');
    return rows.map(row=>({name:row.name,
      value:row.value||decryptBraveCookie({encryptedHex:row.encrypted_hex,hostKey:row.host_key,dbVersion:row.db_version,password}),
      domain:row.host_key,path:row.path||'/',
      expires:row.expires_utc==='0'?-1:Number(BigInt(row.expires_utc)/1000000n-11644473600n),
      httpOnly:!!row.is_httponly,secure:!!row.is_secure,sameSite:row.samesite===1?'Lax':row.samesite===2?'Strict':'None',
    })).filter(c=>c.expires===-1||c.expires>Date.now()/1000);
  };
  const {cookies}=await captureStableCookies(snapshot);
  if(!cookies.length) throw new Error('no valid NGA cookies');
  run('ssh',['-o','BatchMode=yes',process.env.PYLON_CLOUD_HOST||'tencent-cloud',
             `umask 077; cat > '${destination}'`],JSON.stringify({cookies}));
  console.log(`NGA_COOKIE_EXPORT_OK count=${cookies.length}`);
} catch(error) {
  console.error('NGA_COOKIE_EXPORT_FAILED '+error.message);
  process.exitCode=1;
}
