import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { EnvFileError, loadEnvFile, parseEnvFile } from './env-file.ts';

const dir = mkdtempSync(path.join(tmpdir(), 'env-file-test-'));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

function writeEnv(name: string, text: string): string {
  const file = path.join(dir, name);
  writeFileSync(file, text, 'utf8');
  return file;
}

describe('parseEnvFile', () => {
  it('读 KEY=VALUE，跳过空行与整行注释', () => {
    const parsed = parseEnvFile(['# 注释', '', 'A=1', '  ', 'B=two'].join('\n'));
    expect([...parsed]).toEqual([
      ['A', '1'],
      ['B', 'two'],
    ]);
  });

  it('支持 export 前缀与 CRLF 换行', () => {
    expect([...parseEnvFile('export A=1\r\nB=2\r\n')]).toEqual([
      ['A', '1'],
      ['B', '2'],
    ]);
  });

  it('引号包裹的值去掉引号，双引号解转义，单引号原样', () => {
    const parsed = parseEnvFile('A="a\\nb"\nB=\'c\\nd\'\nC="含 空格"');
    expect(parsed.get('A')).toBe('a\nb');
    expect(parsed.get('B')).toBe('c\\nd');
    expect(parsed.get('C')).toBe('含 空格');
  });

  it('只有 # 前面有空白才算行内注释，# 本身保留在值里', () => {
    const parsed = parseEnvFile('A=1 # 注释\nB=ab#cd\nC="1 # 不是注释"');
    expect(parsed.get('A')).toBe('1');
    expect(parsed.get('B')).toBe('ab#cd');
    expect(parsed.get('C')).toBe('1 # 不是注释');
  });

  it('SESSDATA 这类值原样保留（%2C、*、-、_ 不做处理）', () => {
    // 占位值：SESSDATA 是登录态，不要写真实值进仓库
    const sessdata = 'FAKE-SESSDATA%2C1800000000%2Cabc12%2Adef-ghi_jkl';
    expect(parseEnvFile(`SESSDATA=${sessdata}`).get('SESSDATA')).toBe(sessdata);
  });

  it('未加引号的 JSON 值原样保留', () => {
    expect(parseEnvFile('LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}').get('LLM_EXTRA_BODY')).toBe(
      '{"thinking":{"type":"disabled"}}',
    );
  });

  it('同名键按先出现的算', () => {
    expect(parseEnvFile('A=first\nA=second').get('A')).toBe('first');
  });

  it('空值算已定义的空串', () => {
    expect(parseEnvFile('A=').get('A')).toBe('');
  });

  it('格式错误的行带行号抛 EnvFileError', () => {
    expect(() => parseEnvFile('A=1\n这行没有等号')).toThrow(EnvFileError);
    expect(() => parseEnvFile('A=1\n这行没有等号')).toThrow(/第 2 行/);
    expect(() => parseEnvFile('1A=1')).toThrow(EnvFileError);
  });
});

describe('loadEnvFile', () => {
  it('文件不存在时返回 found: false，不抛错', () => {
    const env: Record<string, string | undefined> = {};
    expect(loadEnvFile(path.join(dir, '没有这个文件.env'), env)).toEqual({
      path: path.join(dir, '没有这个文件.env'),
      found: false,
      applied: [],
      kept: [],
    });
    expect(env).toEqual({});
  });

  it('补齐未设置的键，并覆盖空串（bat 的 set /p 空回车）', () => {
    const file = writeEnv('fill.env', 'A=from-file\nB=from-file\nC=from-file');
    const env: Record<string, string | undefined> = { A: 'from-env', B: '' };

    const result = loadEnvFile(file, env);

    expect(env).toEqual({ A: 'from-env', B: 'from-file', C: 'from-file' });
    expect(result.applied).toEqual(['B', 'C']);
    expect(result.kept).toEqual(['A']);
    expect(result.found).toBe(true);
  });

  it('整份 .env 能一次补进新 env 对象', () => {
    const file = writeEnv(
      'full.env',
      ['# 直播配置', 'ROOM_ID=22748536', 'SESSDATA=a%2Cb', 'LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}'].join(
        '\n',
      ),
    );
    const env: Record<string, string | undefined> = {};

    loadEnvFile(file, env);

    expect(env).toEqual({
      LLM_EXTRA_BODY: '{"thinking":{"type":"disabled"}}',
      ROOM_ID: '22748536',
      SESSDATA: 'a%2Cb',
    });
  });
});
