import assert from 'node:assert/strict';
import test from 'node:test';

import {runProcess} from '../lib/process.mjs';


test('process runner sends sensitive payload through stdin', async () => {
  const secret = 'payload-only-secret';
  const result = await runProcess(process.execPath, ['-e', "process.stdin.pipe(process.stdout)"], {
    input: secret,
    quiet: true,
  });
  assert.equal(result.stdout, secret);
  assert.equal(result.command.includes(secret), false);
});


test('process failure redacts registered secrets', async () => {
  const secret = 'sensitive-value';
  await assert.rejects(
    runProcess(process.execPath, ['-e', `process.stderr.write('${secret}'); process.exit(7)`], {
      quiet: true,
      secrets: [secret],
    }),
    error => error.code === 7 && !error.message.includes(secret) && error.message.includes('[REDACTED]'),
  );
});
