// SQLite DATETIME 工具：与 Python SQLAlchemy sqlite 方言的存储格式对齐
// （"YYYY-MM-DD HH:MM:SS.ffffff"，本地时区，恒定 6 位微秒）。
// 字符串比较 = 时间比较的前提是写入格式一致，所有 TS 侧写库统一走这里。

export function sqliteDatetime(date: Date = new Date()): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds() * 1000, 6)}`
  );
}

/** epoch 秒 → SQLite DATETIME 字符串（对应 Python datetime.fromtimestamp）。 */
export function sqliteDatetimeFromEpoch(epochSeconds: number): string {
  return sqliteDatetime(new Date(epochSeconds * 1000));
}

/** SQLite DATETIME 字符串 → Date（本地时区）；解析失败返回 null。 */
export function parseSqliteDatetime(value: string | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
