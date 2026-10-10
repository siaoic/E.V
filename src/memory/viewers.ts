import type { NoteWorkspace } from './notes.ts';

// 观众档案：viewers/<uid>.md，第一行是给机械唤起用的摘要，往下是累积的观察。
export type ViewerBook = {
  firstLine(uid: number): Promise<string>;
  load(uid: number): Promise<string>;
  save(uid: number, content: string): Promise<void>;
  note(uid: number, text: string): Promise<void>;
};

export function createViewerBook(notes: NoteWorkspace): ViewerBook {
  const file = (uid: number) => `viewers/${uid}.md`;
  const date = () => new Date().toISOString().slice(0, 10);
  return {
    async firstLine(uid: number): Promise<string> {
      const content = await notes.read(file(uid)).catch(() => '');
      return content.split('\n').map((line) => line.trim()).find(Boolean) ?? '';
    },
    load(uid: number): Promise<string> {
      return notes.read(file(uid)).catch(() => '');
    },
    save(uid: number, content: string): Promise<void> {
      return notes.write(file(uid), content).then(() => undefined);
    },
    note(uid: number, text: string): Promise<void> {
      return notes.append(file(uid), `- [${date()}] ${text}`);
    },
  };
}
