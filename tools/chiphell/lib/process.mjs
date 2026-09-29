import {spawn} from 'node:child_process';

import {redactSecrets} from './deploy-logic.mjs';


export function runProcess(command, args = [], {
  cwd,
  input,
  quiet = false,
  secrets = [],
  env = process.env,
} = {}) {
  const renderedCommand = [command, ...args].join(' ');
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {cwd, env, stdio: ['pipe', 'pipe', 'pipe']});
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdout += chunk;
      if (!quiet) process.stdout.write(redactSecrets(chunk, secrets));
    });
    child.stderr.on('data', chunk => {
      stderr += chunk;
      if (!quiet) process.stderr.write(redactSecrets(chunk, secrets));
    });
    child.on('error', reject);
    if (input === undefined) child.stdin.end();
    else child.stdin.end(input);
    child.on('close', code => {
      const commandText = redactSecrets(renderedCommand, secrets);
      if (code === 0) {
        resolve({stdout, stderr, command: commandText, code});
        return;
      }
      const detail = redactSecrets(stderr.trim() || stdout.trim() || `exit ${code}`, secrets);
      const error = new Error(`${commandText} failed: ${detail}`);
      error.code = code;
      error.stdout = redactSecrets(stdout, secrets);
      error.stderr = redactSecrets(stderr, secrets);
      reject(error);
    });
  });
}
