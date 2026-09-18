/**
 * TOML「手术式」写入器（迁移风险 R12）。
 *
 * smol-toml / @iarna/toml 解析后重新 stringify 都会丢注释，而
 * `config/*.toml` 是用户数据红线（注释是用户留在配置里的备忘）。
 * 因此升级 / 写回采取文本级手术：只改目标键所在的那一行，
 * 保留行内 `#` 注释与其余所有行字节不变。
 *
 * 注意：`#` 出现在引号字符串内不属于注释（如 `name = "a#b" # 注释`），
 * 行内注释切分按引号状态扫描。
 */

export type TomlValue = string | number | boolean | TomlValue[];

const SECTION_PATTERN = /^\s*\[([^\]]+)\]\s*(#.*)?$/;

/** 把 JS 值序列化为 TOML 字面量（标量与一维数组）。 */
export function serializeTomlValue(value: TomlValue): string {
  if (typeof value === "string") {
    return JSON.stringify(value); // JSON 字符串转义与 TOML basic string 兼容（\u、\"、\\）
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => serializeTomlValue(item)).join(", ")}]`;
  }
  throw new Error(`不支持的 TOML 值类型: ${typeof value}`);
}

/**
 * 把一行切成「代码部分 + 行内注释部分」：引号外的第一个 `#` 起为注释。
 */
function splitInlineComment(line: string): { code: string; comment: string } {
  let inString: '"' | "'" | null = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (inString === '"') {
      if (ch === "\\") {
        i += 1; // 跳过转义字符
      } else if (ch === '"') {
        inString = null;
      }
      continue;
    }
    if (inString === "'") {
      if (ch === "'") {
        inString = null;
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      inString = ch;
      continue;
    }
    if (ch === "#") {
      return { code: line.slice(0, i).trimEnd(), comment: line.slice(i) };
    }
  }
  return { code: line.trimEnd(), comment: "" };
}

export interface TomlUpdateResult {
  /** 更新后的完整文件文本。 */
  text: string;
  /** 是否有实际变化（值原本就相同时为 false）。 */
  changed: boolean;
  /** 动作：updated 改键 / inserted 补键 / section_added 新建节 / unchanged 无变化。 */
  action: "updated" | "inserted" | "section_added" | "unchanged";
}

/**
 * 设置 `[section]` 下的 `key = value`，保留行内注释与文件其余部分。
 */
export function setTomlKey(source: string, section: string, key: string, value: TomlValue): TomlUpdateResult {
  const serialized = `${key} = ${serializeTomlValue(value)}`;
  const lines = source.split(/\r?\n/);
  let sectionIndex = -1;
  let keyLineIndex = -1;
  let originalLine = "";

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const sectionMatch = SECTION_PATTERN.exec(line);
    if (sectionMatch !== null) {
      if (sectionMatch[1].trim() === section) {
        sectionIndex = i;
        break;
      }
    }
  }

  if (sectionIndex >= 0) {
    for (let i = sectionIndex + 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (SECTION_PATTERN.test(line)) {
        break; // 进入下一个节，本节没有这个键
      }
      const keyMatch = new RegExp(`^\\s*${key}\\s*=\\s*(#.*)?$`).exec(line) ?? new RegExp(`^\\s*${key}\\s*=\\s*`).exec(line);
      if (keyMatch !== null) {
        keyLineIndex = i;
        originalLine = line;
        break;
      }
    }
  }

  // 情形一：键已存在 → 原位替换，保留行内注释
  if (keyLineIndex >= 0) {
    const { comment } = splitInlineComment(originalLine);
    const leadingWhitespace = /^\s*/.exec(originalLine)?.[0] ?? "";
    const newText = `${leadingWhitespace}${serialized}${comment ? ` ${comment}` : ""}`;
    if (newText === originalLine) {
      return { text: source, changed: false, action: "unchanged" };
    }
    lines[keyLineIndex] = newText;
    return { text: lines.join("\n"), changed: true, action: "updated" };
  }

  // 情形二：节存在但键不存在 → 插到节头之后（节内首行）
  if (sectionIndex >= 0) {
    lines.splice(sectionIndex + 1, 0, serialized);
    return { text: lines.join("\n"), changed: true, action: "inserted" };
  }

  // 情形三：节不存在 → 文件末尾新建节
  const trimmed = source.replace(/\s+$/, "");
  const text = `${trimmed}\n\n[${section}]\n${serialized}\n`;
  return { text, changed: true, action: "section_added" };
}
