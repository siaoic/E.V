import { describe, expect, it } from 'vitest';
import { EnvError, substituteEnv } from './config.ts';

describe('substituteEnv', () => {
  it('替换已设置的 ${VAR}', () => {
    expect(substituteEnv({ token: '${T_TOKEN}' }, { T_TOKEN: 'abc' })).toEqual({ token: 'abc' });
  });

  it('${VAR:-default} 在未设或空串时取默认值，已设时取环境值', () => {
    expect(substituteEnv('${P_PORT:-8787}', {})).toBe('8787');
    expect(substituteEnv('${P_PORT:-8787}', { P_PORT: '' })).toBe('8787');
    expect(substituteEnv('${P_PORT:-8787}', { P_PORT: '9000' })).toBe('9000');
  });

  it('一串里混多个 token', () => {
    expect(substituteEnv('ws://${H}:${PORT}/sub', { H: '127.0.0.1', PORT: '80' })).toBe('ws://127.0.0.1:80/sub');
  });

  it('${VAR} 未设置时抛 EnvError，指明变量名', () => {
    expect(() => substituteEnv('${T_MISSING}', {})).toThrow(EnvError);
    expect(() => substituteEnv('${T_MISSING}', {})).toThrow(/T_MISSING/);
  });

  it('${VAR} 已设置为空串时不算未设，保留空串', () => {
    expect(substituteEnv('${T_EMPTY}', { T_EMPTY: '' })).toBe('');
  });

  it('不匹配的字面 $ 原样保留', () => {
    expect(substituteEnv('$100 and ${HOME:-/root}', {})).toBe('$100 and /root');
  });

  it('深遍历对象与数组，非字符串原样返回', () => {
    expect(substituteEnv({ a: ['${X:-1}', 2, { b: '${Y}' }] }, { X: 'x', Y: 'y' })).toEqual({ a: ['x', 2, { b: 'y' }] });
    expect(substituteEnv(5, {})).toBe(5);
    expect(substituteEnv(null, {})).toBeNull();
  });
});
