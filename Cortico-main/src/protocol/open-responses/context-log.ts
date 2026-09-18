import { appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { ContextRecord } from './context.ts';

function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

/** Immutable standard Items with runtime metadata stored in a separate field. */
export class ContextLog {
  private entries: readonly ContextRecord[] = Object.freeze([]);
  constructor(readonly file: string, private readonly stamp?: () => string) {
    mkdirSync(dirname(file), { recursive: true });
  }

  get records(): readonly ContextRecord[] { return this.entries; }

  load(): void {
    if (!existsSync(this.file)) { this.entries = Object.freeze([]); return; }
    const rows = readFileSync(this.file, 'utf8').split(/\r?\n/).filter(line => line.trim()).map((line, index) => {
      let row: ContextRecord;
      try { row = JSON.parse(line) as ContextRecord; }
      catch { throw new Error(`Invalid session JSON at ${this.file}:${index + 1}`); }
      if (!row || row.version !== 2 || !row.item || typeof row.item !== 'object' || Array.isArray(row.item)
        || !row.context || typeof row.context !== 'object' || Array.isArray(row.context))
        throw new Error(`Invalid context record at ${this.file}:${index + 1}`);
      return freeze(row);
    });
    this.entries = Object.freeze(rows);
  }

  append(entry: ContextRecord): ContextRecord {
    const next = structuredClone(entry);
    if (this.stamp && next.context.ts === undefined) next.context.ts = this.stamp();
    appendFileSync(this.file, JSON.stringify(next) + '\n');
    this.entries = Object.freeze([...this.entries, freeze(next)]);
    return next;
  }

  reset(records: readonly ContextRecord[]): void {
    const next = records.map(entry => freeze(structuredClone(entry)));
    atomicContextWrite(this.file, next);
    this.entries = Object.freeze(next);
  }
}

function atomicContextWrite(path: string, records: readonly ContextRecord[]): void {
  const temporary = `${path}.${process.pid}.tmp`;
  const fd = openSync(temporary, 'w');
  try {
    writeFileSync(fd, records.map(row => JSON.stringify(row)).join('\n') + (records.length ? '\n' : ''));
    fsyncSync(fd);
  } finally { closeSync(fd); }
  renameSync(temporary, path);
}
