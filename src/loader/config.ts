// 插件配置的 env 替换：仅字符串值做 ${VAR} / ${VAR:-default} 替换，深遍历对象与数组。
// ${VAR} 未设 → EnvError（插件级失败，不拖垮其他插件）；${VAR:-default} 未设或空串 → default；
// 一串里可混多个 token；不匹配的字面 $ 原样保留；不做类型转换，插件自己 Number() 强转。
export class EnvError extends Error {}

const TOKEN = /\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g;

function substituteString(text: string, env: Record<string, string | undefined>): string {
  return text.replace(TOKEN, (raw, name: string, fallback?: string) => {
    if (fallback !== undefined) {
      const value = env[name];
      return value === undefined || value === '' ? fallback : value;
    }
    const value = env[name];
    if (value === undefined) throw new EnvError(`环境变量 ${name} 未设置`);
    return value;
  });
}

export function substituteEnv(value: unknown, env: Record<string, string | undefined>): unknown {
  if (typeof value === 'string') return substituteString(value, env);
  if (Array.isArray(value)) return value.map((item) => substituteEnv(item, env));
  if (value !== null && typeof value === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) result[key] = substituteEnv(item, env);
    return result;
  }
  return value;
}
