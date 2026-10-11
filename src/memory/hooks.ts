import type { ToolDefinition } from '../llm/client.ts';
import type { NoteWorkspace } from './notes.ts';
import type { PersonaStore } from './persona.ts';
import type { ViewerBook } from './viewers.ts';
import type { MemoryWorkspace } from './workspace.ts';

export type MemoryToolkit = {
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): Promise<string>;
  recentDigest(limit: number): string;
  /** 人格常驻前缀：宪法种子 + PERSONA.md，进系统提示词头部；人格层缺席时返回空串 */
  prompt(): string;
  /** 每窗口注入：按话题/上一句召回相关记忆，开场附「别重复」摘要 */
  context(input: { topic?: string; lastSpoken?: string; opening: boolean }): Promise<string>;
  /** 机械唤起：批量取观众档案首行，无档案的观众不返回 */
  viewerLines(uids: number[]): Promise<string>;
};

/** 看图能力：由 llm 服务的 describeImage 注入；缺省时带图记忆必须自带文字说明 */
export type MemoryToolkitHelpers = {
  describeImage?: (input: { image: string; prompt?: string }) => Promise<string>;
  persona?: PersonaStore;
  notes?: NoteWorkspace;
  viewers?: ViewerBook;
  /** dream 后台整理的最新摘要，有内容时随窗口注入 */
  dreamSummary?: () => string;
};

const MEMORY_TOOLS: ToolDefinition[] = [
  {
    type: 'function',
    function: {
      name: 'memory_save',
      description:
        '把一件事记成长期记忆（讲过的完整故事、重要的观众、关于自己的事实），一句简短概括。带图片路径时可以不写说明，会自动看图生成。',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: '一句简短概括；带图片时可省略（自动看图描述）' },
          kind: { type: 'string', enum: ['story', 'fact', 'viewer'], description: 'story=讲过的故事，fact=关于自己的事，viewer=观众相关' },
          image: { type: 'string', description: '可选：图片的本地路径' },
          viewer: { type: 'number', description: '可选：观众 uid（弹幕行首的 [#编号]）；给这位观众记档案时填，配 kind=viewer' },
        },
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'memory_recall',
      description: '查长期记忆，返回最相关的几条。',
      parameters: {
        type: 'object',
        properties: { query: { type: 'string', description: '想查的关键词' } },
        required: ['query'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'persona_update',
      description:
        '把对自己的新认知沉淀为人格笔记：偏好变化、对某个观众形成的稳定认知、让你改变想法的时刻。只追加不改写；写下的内容之后每个窗口都跟着你。',
      parameters: {
        type: 'object',
        properties: { note: { type: 'string', description: '一句自我认知，如「我发现自己讲解失误时喜欢先嘴硬再承认」' } },
        required: ['note'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'persona_revise',
      description: '修订人格笔记里的一条旧认知（系统提示词「自我认知」小节的某一行，原样复制来匹配）。replace 留空即收回那句话。',
      parameters: {
        type: 'object',
        properties: {
          match: { type: 'string', description: '要改的那一行原文（一字不差）' },
          replace: { type: 'string', description: '改成的内容；留空表示删除这一条' },
        },
        required: ['match'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'memory_forget',
      description: '删掉一条记错的或过时的长期记忆（id 见 memory_recall 结果行首的 [#编号]）。',
      parameters: {
        type: 'object',
        properties: { id: { type: 'number', description: '要删除的记忆编号' } },
        required: ['id'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'recall_viewer',
      description: '调出一位观众的完整档案（uid 见弹幕行首的 [#编号]）。',
      parameters: {
        type: 'object',
        properties: { uid: { type: 'number', description: '观众的 uid' } },
        required: ['uid'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_write',
      description:
        '写你自己的笔记文件（notes/ 下，如 notes/剪片心得.md）。纪律：一件事实只存一处；时间锚真实日期；写成生动记叙而不是流水账。覆盖整个文件。',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'notes/ 或 viewers/ 下的路径' },
          content: { type: 'string', description: '完整的 Markdown 内容' },
        },
        required: ['path', 'content'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_read',
      description: '读一篇笔记或观众档案。',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string', description: 'notes/ 或 viewers/ 下的路径' } },
        required: ['path'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_append',
      description: '往笔记或观众档案末尾追加一段。',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'notes/ 或 viewers/ 下的路径' },
          text: { type: 'string', description: '追加的内容' },
        },
        required: ['path', 'text'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_edit',
      description: '改笔记里的一处原文（第一处匹配被替换）。',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'notes/ 或 viewers/ 下的路径' },
          match: { type: 'string', description: '要改的原文' },
          replace: { type: 'string', description: '改后的内容' },
        },
        required: ['path', 'match', 'replace'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_delete',
      description: '删掉一篇笔记或观众档案。',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string', description: 'notes/ 或 viewers/ 下的路径' } },
        required: ['path'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'note_list',
      description: '列出你写过的笔记与观众档案。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
    },
  },
  {
    type: 'function',
    function: {
      name: 'git_log',
      description: '看某个文件（或全部）的版本历史——笔记比记忆中短了、或想找回旧版时先看这个。',
      parameters: {
        type: 'object',
        properties: { path: { type: 'string', description: '可选：只看这个文件的改动' } },
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'git_show',
      description: '看某个历史版本的完整内容（hash 来自 git_log）。',
      parameters: {
        type: 'object',
        properties: { hash: { type: 'string', description: '提交哈希' } },
        required: ['hash'],
        additionalProperties: false,
      },
    },
  },
];

function parseArguments(raw: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(raw || '{}');
    return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export function createMemoryToolkit(workspace: MemoryWorkspace, helpers: MemoryToolkitHelpers = {}): MemoryToolkit {
  const formatRecord = (record: { id: number; text: string; image?: string }): string =>
    `[#${record.id}] ${record.image ? `[图] ${record.image} — ` : ''}${record.text}`;
  return {
    tools: MEMORY_TOOLS,
    prompt: () => helpers.persona?.prompt() ?? '',
    async run(name, rawArguments) {
      const args = parseArguments(rawArguments);
      if (name === 'memory_save') {
        const image = typeof args.image === 'string' && args.image.trim() ? args.image.trim() : undefined;
        const hasText = typeof args.text === 'string' && args.text.trim();
        if (!hasText && !image) return '（text 和 image 至少给一个）';
        let caption = hasText ? (args.text as string).trim() : '';
        if (image && !caption) {
          // 带图没写说明：看图自动生成描述（glm-5.3-flash 自带视觉能力）
          if (!helpers.describeImage) return '（当前看不了图，请自己补一句描述再存）';
          try {
            caption = await helpers.describeImage({ image, prompt: '用一句中文概括这张图片，作为直播记忆的说明' });
          } catch (error) {
            return `（看图失败：${error instanceof Error ? error.message : String(error)}）`;
          }
          if (!caption) return '（看图没有结果，请自己补一句描述）';
        }
        const kind = typeof args.kind === 'string' ? args.kind : 'note';
        const uid = typeof args.viewer === 'number' ? args.viewer : Number(args.viewer);
        if (Number.isInteger(uid) && uid > 0) {
          if (!helpers.viewers) return '（当前没有观众档案）';
          if (kind !== 'viewer') return '（记观众档案要把 kind 设为 viewer）';
          await helpers.viewers.note(uid, caption);
          return `（已记进 #${uid} 的观众档案：${caption}）`;
        }
        workspace.add(caption, kind, image);
        return `（已记住：${caption}）`;
      }
      if (name === 'recall_viewer') {
        const uid = typeof args.uid === 'number' ? args.uid : Number(args.uid);
        if (!Number.isInteger(uid) || uid <= 0) return '（uid 必须是弹幕行首的编号）';
        if (!helpers.viewers) return '（当前没有观众档案）';
        const profile = (await helpers.viewers.load(uid)).trim();
        return profile || `（还没有 #${uid} 的档案）`;
      }
      if (name === 'memory_recall') {
        if (typeof args.query !== 'string' || !args.query.trim()) return '（query 必须是关键词）';
        const found = await workspace.recall(args.query.trim());
        if (found.length === 0) return '（没有相关记忆）';
        return found.map(formatRecord).join('\n');
      }
      if (name === 'persona_update') {
        if (typeof args.note !== 'string' || !args.note.trim()) return '（note 必须是一句自我认知）';
        if (!helpers.persona) return '（当前没有人格工作区）';
        await helpers.persona.append(args.note.trim());
        return `（已沉淀：${args.note.trim()}）`;
      }
      if (name === 'persona_revise') {
        if (typeof args.match !== 'string' || !args.match.trim()) return '（match 必须是要改的那一行的原文）';
        if (!helpers.persona) return '（当前没有人格工作区）';
        const replace = typeof args.replace === 'string' ? args.replace.trim() : '';
        const done = await helpers.persona.revise(args.match, replace);
        return done ? (replace ? '（已修订）' : '（已收回这条认知）') : '（没找到这一行，要从「自我认知」小节原样复制）';
      }
      if (name === 'memory_forget') {
        const id = typeof args.id === 'number' ? args.id : Number(args.id);
        if (!Number.isInteger(id) || id <= 0) return '（id 必须是 memory_recall 结果里的编号）';
        return workspace.forget(id) ? `（已忘掉 #${id}）` : `（没有 #${id} 这条记忆）`;
      }
      if (name.startsWith('note_') || name.startsWith('git_')) {
        if (!helpers.notes) return '（当前没有笔记工作区）';
        const notePath = (value: unknown): string | null => {
          if (typeof value !== 'string') return null;
          const p = value.trim().replace(/\\/g, '/');
          return p.startsWith('notes/') || p.startsWith('viewers/') ? p : null;
        };
        try {
          if (name === 'note_write') {
            const p = notePath(args.path);
            if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
            if (typeof args.content !== 'string' || !args.content.trim()) return '（content 不能是空的）';
            const { shrinkNote: note } = await helpers.notes.write(p, args.content);
            return note === undefined ? `（已写入 ${p}）` : `（已写入 ${p}；${note}）`;
          }
          if (name === 'note_read') {
            const p = notePath(args.path);
            if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
            const content = (await helpers.notes.read(p)).trim();
            return content || '（空笔记）';
          }
          if (name === 'note_append') {
            const p = notePath(args.path);
            if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
            if (typeof args.text !== 'string' || !args.text.trim()) return '（text 不能是空的）';
            await helpers.notes.append(p, args.text);
            return `（已追加进 ${p}）`;
          }
          if (name === 'note_edit') {
            const p = notePath(args.path);
            if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
            if (typeof args.match !== 'string' || !args.match) return '（match 必须是要改的原文）';
            const replace = typeof args.replace === 'string' ? args.replace : '';
            return (await helpers.notes.edit(p, args.match, replace)) ? `（已改好 ${p}）` : '（没找到这段原文）';
          }
          if (name === 'note_delete') {
            const p = notePath(args.path);
            if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
            return (await helpers.notes.remove(p)) ? `（已删除 ${p}）` : `（没有 ${p} 这篇）`;
          }
          if (name === 'note_list') {
            const found = [
              ...(await helpers.notes.list('notes')),
              ...(await helpers.notes.list('viewers')),
            ];
            return found.length ? found.join('\n') : '（还没有写过笔记）';
          }
          if (name === 'git_log') {
            if (args.path !== undefined && args.path !== '') {
              const p = notePath(args.path);
              if (!p) return '（路径要在 notes/ 或 viewers/ 下）';
              return (await helpers.notes.gitLog(p)) || '（还没有版本历史）';
            }
            return (await helpers.notes.gitLog(undefined)) || '（还没有版本历史）';
          }
          if (name === 'git_show') {
            if (typeof args.hash !== 'string' || !args.hash.trim()) return '（hash 来自 git_log 的输出）';
            return helpers.notes.gitShow(args.hash.trim());
          }
        } catch (error) {
          return `（笔记操作失败：${error instanceof Error ? error.message : String(error)}）`;
        }
      }
      return '（未知工具）';
    },
    recentDigest(limit: number): string {
      if (workspace.size() === 0) return '';
      const records = workspace.recent(limit);
      return `（你之前讲过或记过这些，别重复：\n${records.map((record) => `- ${formatRecord(record)}`).join('\n')}）`;
    },
    async context(input) {
      const query = [input.topic, input.lastSpoken].filter(Boolean).join(' ').trim();
      const digestRecords = input.opening && workspace.size() > 0 ? workspace.recent(8) : [];
      const digestIds = new Set(digestRecords.map((record) => record.id));
      const recalled = query
        ? (await workspace.recall(query, 3)).filter((record) => !digestIds.has(record.id))
        : [];
      const sections: string[] = [];
      if (digestRecords.length > 0) {
        sections.push(`（你之前讲过或记过这些，别重复：\n${digestRecords.map((record) => `- ${formatRecord(record)}`).join('\n')}）`);
      }
      if (recalled.length > 0) {
        sections.push(`（与现在正相关的记忆：\n${recalled.map((record) => `- ${formatRecord(record)}`).join('\n')}）`);
      }
      const dream = helpers.dreamSummary?.() ?? '';
      if (dream) sections.push(`（梦里你整理出：${dream}）`);
      return sections.join('\n');
    },
    async viewerLines(uids: number[]): Promise<string> {
      if (!helpers.viewers) return '';
      const unique = [...new Set(uids.filter((uid) => Number.isInteger(uid) && uid > 0))];
      const lines = await Promise.all(
        unique.map(async (uid) => {
          const line = await helpers.viewers!.firstLine(uid);
          return line ? `[#${uid}] ${line}` : '';
        }),
      );
      return lines.filter(Boolean).join('\n');
    },
  };
}
