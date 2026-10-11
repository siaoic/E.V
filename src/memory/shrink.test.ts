import { describe, expect, it } from 'vitest';
import { shrinkNote } from './shrink.ts';

const body = (sections: string[], filler = '内容') =>
  sections.map((title) => `## ${title}\n${filler.repeat(40)}`).join('\n\n');

describe('shrinkNote', () => {
  it('上一版不足 200 字就不提示（小文件改动不算缩水）', () => {
    expect(shrinkNote('短的一版', '更短')).toBeNull();
  });

  it('新内容不到上一版一半才提示，并点名将要消失的小节', () => {
    const prev = body(['身份与原则', '弹幕回应', '换话题', '沉默的合法性']);
    const next = body(['身份与原则']);

    const note = shrinkNote(prev, next);
    expect(note).toContain('[缩水提示]');
    expect(note).toContain('将要消失的小节');
    expect(note).toContain('## 弹幕回应');
    expect(note).toContain('## 沉默的合法性');
    expect(note).not.toContain('## 身份与原则');
    expect(note).toContain('git_show');
  });

  it('长度没缩水过半就不提示', () => {
    const prev = body(['一', '二']);
    const next = body(['一', '二', '三']);
    expect(shrinkNote(prev, next)).toBeNull();
  });

  it('没有小节标题时也给提示，只是不点名小节', () => {
    const prev = '长'.repeat(400);
    const note = shrinkNote(prev, '短');
    expect(note).toContain('[缩水提示]');
    expect(note).not.toContain('将要消失的小节');
  });

  it('消失的小节最多点 8 个，其余只报数量', () => {
    const prev = body(Array.from({ length: 12 }, (_, i) => `第${i}节`));
    const note = shrinkNote(prev, '短');
    expect(note).toContain('另有 4 节');
  });
});
