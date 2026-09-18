import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const SCRIPT = fileURLToPath(new URL('../../../src/worlds/minecraft/client-window.ps1', import.meta.url));

function runScript(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT, ...args],
      { windowsHide: true },
    );
    let out = '';
    proc.stdout.on('data', (c: Buffer) => { out += c.toString(); });
    proc.on('error', reject);
    proc.on('close', () => resolve(out));
  });
}

describe.skipIf(process.platform !== 'win32')('client-window.ps1', () => {
  it('Find-OwnerWindow 不占用 PowerShell 自动变量 $PID', async () => {
    const raw = await runScript(['-OwnerPid', '1', '-SetTitle', 'cortico-title-probe']);
    const line = raw.trim().split(/\r?\n/).filter(Boolean).pop() ?? '';
    const parsed = JSON.parse(line) as { ok?: boolean; error?: string };
    expect(parsed.error ?? '').not.toMatch(/overwrite variable pid/i);
    expect(parsed.ok === true || parsed.error === 'no-window').toBe(true);
  });
});
