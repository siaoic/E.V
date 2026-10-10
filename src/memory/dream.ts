import type { ChatMessage, LlmPort } from '../llm/client.ts';
import type { NoteWorkspace } from './notes.ts';
import type { PersonaStore } from './persona.ts';

export type Dream = {
  /** 喂一次心跳；每 every 次触发一轮整理 */
  tick(): void;
  /** 上一次整理的一句摘要；还没整理过返回空串 */
  summary(): string;
  /** 场次交接（退出前）：跑最后一轮整理并等它结束 */
  flush(): Promise<void>;
};

export type DreamOptions = {
  llm: LlmPort;
  notes: NoteWorkspace;
  persona: PersonaStore;
  every?: number;
  logger?: (message: string) => void;
};

// 整理输入长、要求输出多条，窗口对话的 7 秒上限不够用（用户为 dream 定的例外：20 秒）
const DREAM_TIMEOUT_MS = 20_000;

// dream fork：后台整理人格笔记（合并重复、蒸馏稳定自我认知、指出矛盾）。
// 单实例排队不并发；LLM 失败就放弃本轮，不落盘不清空旧认知。
export function createDream({ llm, notes, persona, every = 30, logger }: DreamOptions): Dream {
  let beats = 0;
  let running = false;
  let pending = false;
  let lastSummary = '';
  let chain: Promise<void> = Promise.resolve();

  const consolidate = async (): Promise<void> => {
    const personaMd = (await notes.read('PERSONA.md').catch(() => '')).trim();
    const noteFiles = (await notes.list('notes')).slice(0, 20);
    if (!personaMd && noteFiles.length === 0) return;
    const noteBrief = (
      await Promise.all(
        noteFiles.map(async (file) => `${file}\n${(await notes.read(file).catch(() => '')).slice(0, 300)}`),
      )
    ).join('\n---\n');
    const messages: ChatMessage[] = [
      {
        role: 'system',
        content:
          '你是人格笔记的整理程序。任务：合并重复的条目、把反复出现的模式蒸馏成稳定的自我认知、指出并调和矛盾。不发明新事实，只整理已有内容。',
      },
      {
        role: 'user',
        content: [
          `PERSONA.md 当前内容：\n${personaMd || '（空）'}`,
          noteBrief ? `她的笔记：\n${noteBrief}` : '',
          '输出要求：每行一条整理后的自我认知（中文，一句一条，保留「- [日期] 」或直接给句子都可以）；最后一行以 SUMMARY: 开头，给一句本次整理的摘要。不要输出其他内容。',
        ]
          .filter(Boolean)
          .join('\n\n'),
      },
    ];
    const reply = await llm.complete(messages, undefined, { timeoutMs: DREAM_TIMEOUT_MS });
    const lines = (reply.content ?? '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    const summary = lines.find((line) => line.startsWith('SUMMARY:'))?.slice('SUMMARY:'.length).trim() ?? '';
    const entries = lines.filter((line) => !line.startsWith('SUMMARY:') && !line.startsWith('#'));
    if (entries.length === 0 && !summary) return;
    if (entries.length > 0) await persona.replace(entries);
    lastSummary = summary || lastSummary;
    logger?.(summary || '（没有摘要）');
  };

  const schedule = (): Promise<void> => {
    if (running) {
      pending = true;
      return chain;
    }
    running = true;
    chain = (async () => {
      do {
        pending = false;
        try {
          await consolidate();
        } catch (error) {
          logger?.(`整理失败：${error instanceof Error ? error.message : String(error)}`);
        }
      } while (pending);
      running = false;
    })();
    return chain;
  };

  return {
    tick(): void {
      beats += 1;
      if (beats >= every) {
        beats = 0;
        void schedule();
      }
    },
    summary(): string {
      return lastSummary;
    },
    flush(): Promise<void> {
      return schedule();
    },
  };
}
