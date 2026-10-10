import type { WindowItem } from './plugin.ts';
import type { ToolDefinition } from './client.ts';
import { buildContext } from '../agent/context.ts';
import { runAgentLoop, type AgentTool } from '../agent/loop.ts';

const SYSTEM_PROMPT =
  '你正在直播。每次只说一句简短口语，不超过 30 个字，不分段，接着上一句自然往下讲，不要重复自己说过的话。你可以调用 read_danmaku 看看观众的弹幕：大多数时候接着讲自己的就好，只有弹幕真的值得接才回应一下；读到 SC（付费留言）必须回应。记忆方面：讲完的完整故事用 memory_save 记下，想不起以前聊过什么就 memory_recall 查，记错了就 memory_forget 删；对某位观众有新观察就 memory_save 带 viewer 记进对方档案，想翻档案用 recall_viewer；对自己的新认知用 persona_update 沉淀、说错了用 persona_revise 收回；值得长写的体会用 note_write 写成笔记。没看过弹幕时必须开口；看过弹幕后若都不想接，可以只回复（空）。';

const READ_DANMAKU: ToolDefinition = {
  type: 'function',
  function: {
    name: 'read_danmaku',
    description: '查看直播间最新的观众弹幕（大额礼物会一并返回）。只在想回应观众时调用。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
  },
};

const MAX_TOOL_ROUNDS = 3;
const GAME_TOOL_ROUNDS = 8;
const NUDGE = '（这一句必须开口，说一两句接着讲）';

export type GameHooks = {
  prompt: string;
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): string;
  describe(): string;
  isActive(): boolean;
};

export type MemoryHooks = {
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): Promise<string>;
  /** 人格前缀（宪法种子 + 自我认知），进系统提示词头部；缺省时无人格层 */
  prompt?(): string;
  /** 每窗口注入：按话题/上一句召回相关记忆，开场附「别重复」摘要；缺省时不注入 */
  context?(input: { topic?: string; lastSpoken?: string; opening: boolean }): Promise<string>;
  /** 机械唤起：批量取观众档案首行；缺省时不注入 */
  viewerLines?(uids: number[]): Promise<string>;
};

export type WindowReply = { text: string; replyTo: WindowItem[] };

// 直播适配层：把直播间特有的东西（弹幕读取、播报管线、话题提示）接到 Agent Loop 上。
// 循环与上下文本身在 src/agent/，与入口无关。
export async function replyToWindow(
  llm: Parameters<typeof runAgentLoop>[0]['llm'],
  readDanmaku: () => WindowItem[],
  spoken: string[] = [],
  options: {
    changeTopic?: boolean;
    topic?: string;
    opening?: boolean;
    game?: GameHooks;
    memory?: MemoryHooks;
    say?: (text: string) => void;
    waitQuiet?: () => Promise<void>;
  } = {},
): Promise<WindowReply> {
  const game = options.game;
  const memory = options.memory;
  const prompt = options.changeTopic
    ? options.topic
      ? `（你已经聊这个话题有一阵子了，自然地换个新话题吧，可以聊聊：${options.topic}）`
      : '（你已经聊这个话题有一阵子了，自然地换一个新话题吧）'
    : options.topic
      ? `（开始直播了，开个头，可以聊聊：${options.topic}）`
      : '（接着讲你的故事）';
  const personaPrefix = memory?.prompt?.() ?? '';
  const memoryContext = memory?.context
    ? await memory.context({ topic: options.topic, lastSpoken: spoken.at(-1), opening: options.opening ?? false })
    : '';

  const replyTo: WindowItem[] = [];
  const readExecutor = async (): Promise<string> => {
    const items = readDanmaku();
    for (const item of items) if (!replyTo.includes(item)) replyTo.push(item);
    if (items.length === 0) return '（当前没有新弹幕）';
    const lines = items.map(formatLine);
    if (items.some((item) => item.type === 'super-chat')) lines.push('（有 SC，请优先回应）');
    const known = memory?.viewerLines ? await memory.viewerLines(items.map((item) => item.uid)) : '';
    if (known) lines.push(`（你记得的观众：\n${known}）`);
    return lines.join('\n');
  };

  // 顺序与工具分发优先级一致：弹幕 → 游戏（动作）→ 记忆
  const tools: AgentTool[] = [{ name: READ_DANMAKU.function.name, definition: READ_DANMAKU, run: readExecutor }];
  if (game) {
    const hooks = game;
    for (const definition of hooks.tools) {
      tools.push({
        name: definition.function.name,
        definition,
        action: true,
        run: (args: string) => Promise.resolve(hooks.run(definition.function.name, args)),
      });
    }
  }
  if (memory) {
    const hooks = memory;
    for (const definition of hooks.tools) {
      tools.push({
        name: definition.function.name,
        definition,
        run: (args: string) => hooks.run(definition.function.name, args),
      });
    }
  }

  const messages = buildContext({
    personaPrompt: personaPrefix,
    disciplinePrompt: SYSTEM_PROMPT,
    gamePrompt: game?.prompt,
    spoken,
    gameDescribe: game?.describe(),
    memoryContext,
    windowPrompt: prompt,
  });

  const { sentences } = await runAgentLoop({
    llm,
    messages,
    tools,
    maxRounds: game ? GAME_TOOL_ROUNDS : MAX_TOOL_ROUNDS,
    say: options.say,
    waitQuiet: options.waitQuiet,
    nudge: NUDGE,
  });
  return { text: sentences.join(''), replyTo };
}

function formatLine(event: WindowItem): string {
  if (event.type === 'gift') return `[#${event.uid}] ${event.uname}: [礼物] ${event.giftName}×${event.num}`;
  if (event.type === 'super-chat') return `[#${event.uid}] [SC ¥${event.price}] ${event.uname}: ${event.text}`;
  return `[#${event.uid}] ${event.uname}: ${event.text}`;
}
