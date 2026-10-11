import type { WindowItem } from './plugin.ts';
import type { ToolDefinition } from './client.ts';
import { buildContext } from '../agent/context.ts';
import { runAgentLoop, type AgentTool } from '../agent/loop.ts';
import { classifyWindowItems } from './danmaku.ts';

const SYSTEM_PROMPT =
  '你正在直播。每次只说一句简短口语，不超过 30 个字，不分段，接着上一句自然往下讲，不要重复自己说过的话。每句话之前，直播间最新的一批弹幕都已经摆在你的视野里（值得接的逐条列出，其余聚合成一行计数）：大多数时候接着讲自己的就好，只有真的值得接才回应一下；SC、礼物、上舰都只是弹幕里的一类消息，接不接、怎么接由你自己决定。没有值得说的话就不说，沉默是你的合法态。记忆方面：讲完的完整故事用 memory_save 记下，想不起以前聊过什么就 memory_recall 查，记错了就 memory_forget 删；对某位观众有新观察就 memory_save 带 viewer 记进对方档案，想翻档案用 recall_viewer；对自己的新认知用 persona_update 沉淀、说错了用 persona_revise 收回；值得长写的体会用 note_write 写成笔记。';

const MAX_TOOL_ROUNDS = 3;
const GAME_TOOL_ROUNDS = 8;

export type GameHooks = {
  prompt: string;
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): string;
  describe(): string;
  isActive(): boolean;
  /** 未开局时注入的那一行"有得玩"的菜单；不给就什么都不提 */
  menu?: string;
  /** 未开局时**只**暴露这些工具（如 ['sudoku_new']）。不给就不暴露任何游戏工具 */
  openers?: string[];
};

export type MemoryHooks = {
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): Promise<string>;
  /** 人格前缀（宪法种子 + 自我认知），进系统提示词头部。记忆服务必须提供这个出口：缺了就是人格层断线，不做静默降级 */
  prompt(): string;
  /** 每窗口注入：按话题/上一句召回相关记忆，开场附「别重复」摘要；缺省时不注入 */
  context?(input: { topic?: string; lastSpoken?: string; opening: boolean }): Promise<string>;
  /** 机械唤起：批量取观众档案首行；缺省时不注入 */
  viewerLines?(uids: number[]): Promise<string>;
};

export type WindowReply = { text: string; replyTo: WindowItem[] };

// 直播适配层：把直播间特有的东西（弹幕注入、播报管线、话题提示、机械唤起）接到 Agent Loop 上。
// 循环与上下文本身在 src/agent/，与入口无关。
// 弹幕不再是"她要执行的动作"：窗口组装时直接把最新一批分层注入上下文（机制只提供看见的机会）。
export async function replyToWindow(
  llm: Parameters<typeof runAgentLoop>[0]['llm'],
  takeItems: () => WindowItem[],
  spoken: string[] = [],
  options: {
    topic?: string;
    opening?: boolean;
    game?: GameHooks;
    memory?: MemoryHooks;
    /** 机械唤起：本窗要注入档案首行的观众 uid（plugin 按「本场首次出现」筛过）。不经模型决定 */
    viewerUids?: number[];
    /** 节奏水位一行数据（plugin 拼好）：只作背景数据进 user 段，机制不规定说什么 */
    statusContext?: string;
    say?: (text: string) => void;
    waitQuiet?: () => Promise<void>;
  } = {},
): Promise<WindowReply> {
  const game = options.game;
  const memory = options.memory;
  // 只有开场窗口带话题池素材；中段不提示换题（按句数强制换话题已删除，见 ADR-0009）
  const prompt = options.topic
    ? `（开始直播了，开个头，可以聊聊：${options.topic}）`
    : '（接着讲你的故事）';
  const personaPrefix = memory?.prompt() ?? '';
  const memoryContext = memory?.context
    ? await memory.context({ topic: options.topic, lastSpoken: spoken.at(-1), opening: options.opening ?? false })
    : '';

  // 弹幕注入：本窗的条目一次性取走（plugin 侧同时标记「已呈现」），分相关 / 气氛两层
  const { listed, hidden } = classifyWindowItems(takeItems(), spoken);
  const danmakuLines = listed.map(formatLine);
  if (hidden > 0) danmakuLines.push(`（另有 ${hidden} 条闲聊）`);
  // 机械唤起：观众档案首行进上下文，与模型是否"看弹幕"无关（不经模型决定的规则）
  const known = options.viewerUids?.length ? await memory?.viewerLines?.(options.viewerUids) : '';
  if (known) danmakuLines.push(`（你记得的观众：\n${known}）`);

  // 游戏：**没开局就不推她玩**。未开局只给一行可选菜单、只暴露开局工具；
  // 开局之后才注入规则书与盘面描述、并放宽工具轮次（否则模型一看规则书就自己开一局）。
  const playing = game?.isActive() ?? false;
  const tools: AgentTool[] = [];
  if (game) {
    const hooks = game;
    const exposed = playing ? hooks.tools : hooks.tools.filter((definition) => (hooks.openers ?? []).includes(definition.function.name));
    for (const definition of exposed) {
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
    gamePrompt: playing ? game?.prompt : game?.menu,
    spoken,
    gameDescribe: playing ? game?.describe() : undefined,
    memoryContext,
    danmakuContext: danmakuLines.join('\n'),
    statusContext: options.statusContext,
    windowPrompt: prompt,
  });

  const { sentences } = await runAgentLoop({
    llm,
    messages,
    tools,
    maxRounds: playing ? GAME_TOOL_ROUNDS : MAX_TOOL_ROUNDS,
    say: options.say,
    waitQuiet: options.waitQuiet,
    // 不给 nudge：沉默是合法裁决，追着要她开口是机制越界（ADR-0002、种子「沉默的合法性」）
  });
  return { text: sentences.join(''), replyTo: listed };
}

// B 站 guard_level 口径：1 总督 / 2 提督 / 3 舰长。未知值按最常见的舰长兜底
const GUARD_LABELS: Record<number, string> = { 1: '总督', 2: '提督', 3: '舰长' };

function formatLine(event: WindowItem): string {
  if (event.type === 'guard') {
    const label = GUARD_LABELS[event.guardLevel] ?? '舰长';
    return `[#${event.uid}] ${event.uname}: [上舰] ${label}${event.num > 1 ? `×${event.num}` : ''}`;
  }
  if (event.type === 'gift') return `[#${event.uid}] ${event.uname}: [礼物] ${event.giftName}×${event.num}`;
  if (event.type === 'super-chat') return `[#${event.uid}] [SC ¥${event.price}] ${event.uname}: ${event.text}`;
  return `[#${event.uid}] ${event.uname}: ${event.text}`;
}
