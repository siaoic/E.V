// .env 文件加载：启动时把一份 KEY=VALUE 文本补进 process.env 缺的那部分。
// 优先级与 Node 的 --env-file 一致——已存在且非空的环境变量优先，run-live.bat 里 set 的值不会被文件盖掉；
// 空串一律当作未配置（bat 的 set /p 空回车会留下空串，这时仍应吃 .env）。
import { readFileSync } from 'node:fs';

export class EnvFileError extends Error {}

const LINE = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/;

const ESCAPES: Record<string, string> = { n: '\n', r: '\r', t: '\t', '"': '"', '\\': '\\' };

function unescapeDoubleQuoted(value: string): string {
  return value.replace(/\\([nrt"\\])/g, (_, ch: string) => ESCAPES[ch]);
}

// 双引号里允许 \" 转义，找结束引号时要跳过它
function findClosingQuote(value: string, quote: string): number {
  for (let i = 1; i < value.length; i += 1) {
    if (quote === '"' && value[i] === '\\') {
      i += 1;
      continue;
    }
    if (value[i] === quote) return i;
  }
  return -1;
}

function parseValue(tail: string): string {
  const value = tail.trim();
  const quote = value[0];
  if (quote === '"' || quote === "'") {
    const end = findClosingQuote(value, quote);
    if (end > 0) {
      const inner = value.slice(1, end);
      return quote === '"' ? unescapeDoubleQuoted(inner) : inner;
    }
  }
  // 未加引号：只有在 # 前面出现过空白时才算行内注释（dotenv 口径），SESSDATA 里的 %2C/* 原样保留
  const comment = /\s#/.exec(value);
  return (comment ? value.slice(0, comment.index) : value).trim();
}

/** 解析 .env 文本；同名的键按先出现的算（与 dotenv 一致）。格式错误带行号报错。 */
export function parseEnvFile(text: string): Map<string, string> {
  const result = new Map<string, string>();
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const raw = lines[i].trim();
    if (!raw || raw.startsWith('#')) continue;
    const match = LINE.exec(raw);
    if (match === null) throw new EnvFileError(`第 ${i + 1} 行不是 KEY=VALUE：${raw}`);
    const [, key, tail] = match;
    if (!result.has(key)) result.set(key, parseValue(tail));
  }
  return result;
}

export type LoadEnvFileResult = {
  path: string;
  /** 文件是否存在；不存在不是错误（.env 本来就该可选） */
  found: boolean;
  /** 从文件补进去的键 */
  applied: string[];
  /** 环境里已有非空值、因此没动的键 */
  kept: string[];
};

/** 读 .env 并补进 env。文件不存在返回 found: false，不抛错。 */
export function loadEnvFile(
  filePath: string,
  env: Record<string, string | undefined> = process.env,
): LoadEnvFileResult {
  let text: string;
  try {
    text = readFileSync(filePath, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return { path: filePath, found: false, applied: [], kept: [] };
    }
    throw error;
  }
  const applied: string[] = [];
  const kept: string[] = [];
  for (const [key, value] of parseEnvFile(text)) {
    const current = env[key];
    if (current === undefined || current === '') {
      env[key] = value;
      applied.push(key);
    } else {
      kept.push(key);
    }
  }
  return { path: filePath, found: true, applied, kept };
}
