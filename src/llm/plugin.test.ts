import { Context } from '@deepseek-ai/cordis';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, LlmPort, ToolDefinition } from './client.ts';
import { createPuzzle, createRandom } from '../games/sudoku/engine.ts';
import { createSudokuToolkit } from '../games/sudoku/tools.ts';
import { apply } from './plugin.ts';
import type { MemoryHooks } from './reply.ts';

const danmaku = (text: string, uname = '测试观众', uid = 123456) => ({
  type: 'danmaku' as const,
  uid,
  uname,
  text,
  timestamp: 0,
});

const gift = (overrides: Partial<{ price: number; coinType: string; giftName: string; num: number }> = {}) => ({
  type: 'gift' as const,
  uid: 2,
  uname: '土豪',
  giftName: '火箭',
  num: 1,
  price: 1000,
  coinType: 'gold',
  timestamp: 0,
  ...overrides,
});

const saying = (content: string): AssistantReply => ({ content, toolCalls: [] });

// 插件改为服务注入：先 provide，再启动
async function startLlm(
  root: Context,
  options: { llm: LlmPort; topics?: string[]; game?: unknown; memory?: MemoryHooks },
) {
  root.provide('llm', options.llm);
  if (options.game) root.provide('game', options.game);
  if (options.memory) root.provide('memory', options.memory);
  return root.plugin(apply, { topics: options.topics });
}

function createLlm(...replies: AssistantReply[]) {
  const complete = vi
    .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
    .mockResolvedValue(saying('好'));
  for (const reply of replies) complete.mockResolvedValueOnce(reply);
  return { complete };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

describe('llm plugin', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('播报中的弹幕只进缓冲不开窗，下一句播报开始时注入这批弹幕', async () => {
    const llm = createLlm(saying('第一句续'), saying('回应大家'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    const first = danmaku('在吗？', 'a', 1);
    const second = danmaku('吃了吗？', 'b', 2);
    root.emit('world/live-event', first);
    root.emit('world/live-event', second);
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);

    root.emit('playback/started', { sentence: '第一句续', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(2);
    expect(llm.complete.mock.calls[1][0].at(-1)).toEqual({
      role: 'user',
      content: '[#1] a: 在吗？\n[#2] b: 吃了吗？\n（直播状态：你已连着自聊 1 句，期间 2 条弹幕经过；待播队列空）\n（接着讲你的故事）',
    });
    expect(listener).toHaveBeenLastCalledWith({ text: '回应大家', replyTo: [] });
  });

  it('空闲时（无播报、无进行中的窗口）弹幕唤醒一次窗口', async () => {
    const llm = createLlm(saying('好的'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('world/live-event', danmaku('你好'));
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'danmaku' }]]);
    expect(llm.complete).toHaveBeenCalledTimes(1);
  });

  it('窗口进行中到达的弹幕不另开窗口', async () => {
    let resolveWindow: (reply: AssistantReply) => void = () => {};
    const complete = vi.fn(() => new Promise<AssistantReply>((resolve) => (resolveWindow = resolve)));
    const root = new Context();
    await startLlm(root, { llm: { complete } });

    root.emit('world/live-event', danmaku('甲'));
    root.emit('world/live-event', danmaku('乙'));
    await flush();
    expect(complete).toHaveBeenCalledTimes(1);

    resolveWindow(saying('好'));
    await flush();
  });

  it('播报结束后，新弹幕可以重新唤醒已停止的链条', async () => {
    const llm = createLlm(saying('最后一句'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '最后一句', waiting: 0 });
    await flush();
    root.emit('playback/completed', { sentence: '最后一句' });
    root.emit('world/live-event', danmaku('还在吗'));
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'playback' }], [{ source: 'danmaku' }]]);
  });

  it('首个心跳在空闲时开场一次，之后的心跳不再开窗', async () => {
    const llm = createLlm(saying('大家好'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('world/live-event', { type: 'heartbeat', count: 100, timestamp: 6000 });
    await flush();
    root.emit('world/live-event', { type: 'heartbeat', count: 100, timestamp: 60000 });
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'start' }]]);
    expect(llm.complete).toHaveBeenCalledTimes(1);
  });

  it('播报开始时开窗，窗口接着讲故事，没有弹幕时不带弹幕', async () => {
    const llm = createLlm(saying('接着讲'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();

    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '（直播状态：待播队列空）\n（接着讲你的故事）',
    });
    expect(listener).toHaveBeenCalledWith({ text: '接着讲', replyTo: [] });
  });

  it('与最近说过的话重复的回复不发出，也不再立刻重试（沉默合法）', async () => {
    const llm = createLlm(saying('哈哈哈'), saying('哈哈哈'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '一', waiting: 0 });
    await flush();
    root.emit('playback/started', { sentence: '二', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenLastCalledWith({ text: '哈哈哈', replyTo: [] });
  });

  it('空窗口不再立即重试：链条停下，攒够 5 拍心跳才给一次机会', async () => {
    const llm = createLlm(...Array.from({ length: 4 }, () => saying('')));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    expect(windows.mock.calls.map((call) => call[0].source)).toEqual(['playback']);

    root.emit('playback/completed', { sentence: '第一句' });
    for (let beat = 0; beat < 4; beat += 1) {
      root.emit('world/live-event', { type: 'heartbeat', count: beat, timestamp: beat });
      await flush();
    }
    expect(windows).toHaveBeenCalledTimes(1);

    root.emit('world/live-event', { type: 'heartbeat', count: 5, timestamp: 5 });
    await flush();
    expect(windows.mock.calls.at(-1)).toEqual([{ source: 'revive' }]);
  });

  it('停摆后心跳节拍给机会（不是开场），弹幕到达则立刻唤醒', async () => {
    const llm = createLlm(saying(''), saying('第一次拉起'), saying('弹幕唤醒'));
    const root = new Context();
    const windows = vi.fn();
    const listener = vi.fn();
    root.on('llm/window', windows);
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    expect(windows.mock.calls.map((call) => call[0].source)).toEqual(['playback']);

    root.emit('playback/completed', { sentence: '第一句' });
    for (let beat = 0; beat < 5; beat += 1) {
      root.emit('world/live-event', { type: 'heartbeat', count: beat, timestamp: beat });
      await flush();
    }
    // 中段恢复用 revive：不取话题池、也不说"开始直播了"
    expect(windows.mock.calls.at(-1)).toEqual([{ source: 'revive' }]);
    expect(llm.complete).toHaveBeenCalledTimes(2);

    root.emit('world/live-event', danmaku('还在吗？'));
    await flush();
    expect(windows.mock.calls.at(-1)).toEqual([{ source: 'danmaku' }]);
    expect(listener).toHaveBeenLastCalledWith({ text: '弹幕唤醒', replyTo: [] });
  });

  it('窗口运行中到达的多次播报开始合并为一个排队窗口，结束后执行', async () => {
    let resolveFirst: (reply: AssistantReply) => void = () => {};
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockImplementationOnce(() => new Promise<AssistantReply>((resolve) => (resolveFirst = resolve)))
      .mockResolvedValue(saying('第二句'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm: { complete } });

    root.emit('playback/started', { sentence: '一', waiting: 0 });
    root.emit('playback/started', { sentence: '二', waiting: 0 });
    root.emit('playback/started', { sentence: '三', waiting: 0 });
    await flush();
    expect(complete).toHaveBeenCalledTimes(1);

    resolveFirst(saying('第一句'));
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'playback' }], [{ source: 'playback' }]]);
    expect(complete).toHaveBeenCalledTimes(2);
  });

  it('已呈现过的弹幕不会重复进上下文', async () => {
    const llm = createLlm(saying('好'), saying('再来一句'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('world/live-event', danmaku('在吗？', 'a', 1));
    await flush();
    root.emit('playback/completed', { sentence: '好' });
    root.emit('playback/started', { sentence: '再来', waiting: 0 });
    await flush();

    const user = String(llm.complete.mock.calls[1][0].at(-1)?.content);
    expect(user).toBe('（直播状态：你已连着自聊 1 句，期间 1 条弹幕经过；待播队列空）\n（接着讲你的故事）');
  });

  it('缓冲只保留最近 20 条：被挤掉的旧弹幕不会进上下文，逐条上限 10 条', async () => {
    const llm = createLlm(saying('先说'), saying('接着说'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    // 21 条都在播报期间到达：只进缓冲、不开窗
    for (let i = 0; i <= 20; i += 1) root.emit('world/live-event', danmaku(`在吗${i}？`, 'u', 100 + i));
    root.emit('playback/started', { sentence: '第二句', waiting: 0 });
    await flush();

    const user = String(llm.complete.mock.calls[1][0].at(-1)?.content);
    const danmakuLines = user.split('\n').filter((line) => line.startsWith('[#'));
    expect(user).not.toContain('在吗0？'); // 第 21 条把最旧的挤出了缓冲
    expect(user).toContain('在吗20？');
    expect(danmakuLines).toHaveLength(10); // 「只呈现最新 10 条」由分层那一层负责
  });

  it('银瓜子礼物忽略，金瓜子总价 ≥100000（100 元）的礼物进缓冲并唤醒窗口', async () => {
    const llm = createLlm(saying('谢谢火箭'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('world/live-event', gift({ price: 100, coinType: 'silver' }));
    await flush();
    expect(llm.complete).not.toHaveBeenCalled();

    // 总价不到 100 元也进不了（1–99 元档她看不见）
    root.emit('world/live-event', gift({ price: 5000, num: 1 }));
    await flush();
    expect(llm.complete).not.toHaveBeenCalled();

    root.emit('world/live-event', gift({ price: 100_000, num: 1 }));
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);
  });

  it('礼物门槛按总价算：单价 10000 ×10 = 100000 够，单价 10000 ×9 = 90000 不够', async () => {
    const llm = createLlm(saying('谢谢火箭'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('world/live-event', gift({ price: 10_000, num: 9 }));
    await flush();
    expect(llm.complete).not.toHaveBeenCalled();

    root.emit('world/live-event', gift({ price: 10_000, num: 10 }));
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);
    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '[#2] 土豪: [礼物] 火箭×10\n（直播状态：待播队列空）\n（接着讲你的故事）',
    });
  });

  it('播报开始时仍有待播内容则不开窗，待播清空后的播报开始再开窗', async () => {
    const llm = createLlm(saying('接着讲'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 1 });
    await flush();
    expect(llm.complete).not.toHaveBeenCalled();

    root.emit('playback/started', { sentence: '第二句', waiting: 0 });
    await flush();
    expect(windows.mock.calls).toEqual([[{ source: 'playback' }]]);
  });

  it('话题池只在开场取一条，之后不再由机制换题', async () => {
    const seen: string[] = [];
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async (messages) => {
      const last = messages.at(-1)!.content;
      seen.push(typeof last === 'string' ? last : '');
      return saying(`第${seen.length}句`);
    });
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm: { complete }, topics: ['天气', '午饭'] });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();
    // 之后 12 句由播报开始驱动：按句数强制换话题已删除，任何窗口都不该再出现"换个新话题"
    for (let i = 0; i < 12; i += 1) {
      root.emit('playback/started', { sentence: `已播第${i}句`, waiting: 0 });
      await flush();
    }

    const openings = seen.filter((content) => content.includes('可以聊聊：'));
    expect(openings).toHaveLength(1);
    expect(openings[0]).toMatch(/（开始直播了，开个头，可以聊聊：(天气|午饭)）$/);
    // 状态行会排在这些提示之前，所以只看最后一行
    const prompts = seen.map((content) => content.split('\n').at(-1) ?? '');
    expect(prompts.filter((content) => content === '（接着讲你的故事）')).toHaveLength(12);
    expect(seen.some((content) => content.includes('换个新话题'))).toBe(false);
    expect(listener).toHaveBeenCalledTimes(13);
  });

  it('端点限流后按心跳降温：不再每个心跳都开窗，降温结束重新拉起', async () => {
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async () => {
      throw Object.assign(new Error('LLM request failed: 429 {"error":{"code":"1302"}}'), { rateLimited: true });
    });
    const root = new Context();
    await startLlm(root, { llm: { complete } });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();
    expect(complete).toHaveBeenCalledTimes(1);

    // 降温的 9 拍里一个窗口都不开。此前每拍都会重开窗口、每个窗口烧 4 次尝试、失败还立刻再连试 3 次，
    // 等于把限流配额越打越死，直播就长时间没声音
    for (let beat = 0; beat < 9; beat += 1) {
      root.emit('world/live-event', { type: 'heartbeat', count: 2 + beat, timestamp: beat });
      await flush();
    }
    expect(complete).toHaveBeenCalledTimes(1);

    // 第 10 拍降温结束，同一拍重新拉起
    root.emit('world/live-event', { type: 'heartbeat', count: 11, timestamp: 9 });
    await flush();
    expect(complete).toHaveBeenCalledTimes(2);
  });

  it('降温期弹幕只进缓冲不开窗，降温结束后随恢复窗口一起注入', async () => {
    let limited = true;
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async () => {
      if (limited) throw Object.assign(new Error('LLM request failed: 429 1302'), { rateLimited: true });
      return saying('回应攒下的弹幕');
    });
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm: { complete } });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();
    expect(complete).toHaveBeenCalledTimes(1);

    // 降温期来的弹幕：进缓冲但不开窗
    root.emit('world/live-event', danmaku('攒下的弹幕？', '观众', 7));
    await flush();
    expect(complete).toHaveBeenCalledTimes(1);

    limited = false;
    for (let beat = 0; beat < 10; beat += 1) {
      root.emit('world/live-event', { type: 'heartbeat', count: 2 + beat, timestamp: beat });
      await flush();
    }

    expect(listener).toHaveBeenCalledWith({ text: '回应攒下的弹幕', replyTo: [] });
    // 失败的那一轮没有"吃掉"弹幕：恢复后它随窗口一起注入
    const user = String(complete.mock.calls.at(-1)![0].at(-1)?.content);
    expect(user).toContain('[#7] 观众: 攒下的弹幕？');
  });

  it('机械唤起：只对首次出现的观众注入，限流失败的窗口不消费这次唤醒', async () => {
    const viewerLines = vi.fn(async (uids: number[]) => uids.map((uid) => `[#${uid}] 老朋友`).join('\n'));
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockRejectedValueOnce(Object.assign(new Error('LLM request failed: 429 1302'), { rateLimited: true }))
      .mockResolvedValue(saying('终于说话了'));
    const root = new Context();
    await startLlm(root, {
      llm: { complete },
      memory: { tools: [], run: async () => '', prompt: () => '', viewerLines },
    });

    // 第一次：弹幕唤醒窗口，档案注入过但这一轮调用失败 → 不算认过
    root.emit('world/live-event', danmaku('你好', '老朋友', 7));
    await flush();
    expect(viewerLines).toHaveBeenCalledTimes(1);
    expect(viewerLines).toHaveBeenLastCalledWith([7]);

    // 限流降温 10 拍后重新拉起：同一位观众仍是「首次」，要再注入一次
    for (let beat = 0; beat < 10; beat += 1) {
      root.emit('world/live-event', { type: 'heartbeat', count: beat, timestamp: beat });
      await flush();
    }
    expect(viewerLines).toHaveBeenCalledTimes(2);

    // 这一轮成功了 → 已认过，之后的窗口不再重复喂同一份档案
    root.emit('playback/started', { sentence: '已播一句', waiting: 0 });
    await flush();
    expect(viewerLines).toHaveBeenCalledTimes(2);
  });

  it('节奏水位：自聊句数与期间弹幕数进上下文，接住观众的话就清零', async () => {
    const heard: string[] = [];
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(
      async (messages) => {
        heard.push(String(messages.at(-1)!.content));
        return saying(heard.length === 1 ? '我家猫又上桌了' : '另外一个话题');
      },
    );
    const root = new Context();
    await startLlm(root, { llm: { complete } });

    // 第一窗：观众问猫，她接住了（句子与弹幕重合 ≥2 个二字组）→ 自聊清零
    root.emit('world/live-event', danmaku('猫又上桌了吗？', '甲', 1));
    await flush();
    expect(heard[0]).toContain('（直播状态：');
    expect(heard[0]).toContain('待播队列空');

    // 第二窗：播报开始，她讲别的 → 自聊 1 句
    root.emit('playback/started', { sentence: '我家猫又上桌了', waiting: 0 });
    await flush();
    expect(heard[1]).not.toContain('自聊');

    // 播报中来的弹幕只进缓冲，计入"期间经过"
    root.emit('world/live-event', danmaku('另一个无关话题', '乙', 2));
    await flush();

    // 第三窗的状态行：告诉她已经连着自聊 1 句、期间 1 条弹幕经过
    root.emit('playback/started', { sentence: '另外一个话题', waiting: 0 });
    await flush();
    expect(heard[2]).toContain('你已连着自聊 1 句，期间 1 条弹幕经过');
  });

  it('开场窗口注入记忆上下文', async () => {
    const llm = createLlm(saying('好'));
    const root = new Context();
    await startLlm(root, {
      llm,
      memory: {
        tools: [],
        run: async () => '',
        prompt: () => '',
        context: async () => '（你之前讲过：打印机）',
      },
    });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();

    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '（你之前讲过：打印机）\n（直播状态：待播队列空；热度 1）\n（接着讲你的故事）',
    });
  });

  it('离线模拟：假模型按答案落子，经多个窗口走完整局，游戏变为已完成', async () => {
    const { puzzle, solution } = createPuzzle(createRandom(7));
    const empties = puzzle.flatMap((line, row) =>
      line.flatMap((value, col) => (value === 0 ? [{ row, col }] : [])),
    );
    let nextEmpty = 0;
    let opened = false;
    let calls = 0;
    let texts = 0;
    const toolCall = (name: string, args: string): AssistantReply => ({
      content: null,
      toolCalls: [{ id: `call_${(calls += 1)}`, type: 'function', function: { name, arguments: args } }],
    });
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async (_messages, tools) => {
      // 按"这一轮实际暴露了哪些工具"行动：未开局只给 sudoku_new，开局后才给落子工具
      const names = tools?.map((tool) => tool.function.name) ?? [];
      if (names.includes('sudoku_place')) {
        const cell = empties[nextEmpty];
        if (!cell) return saying(`第${(texts += 1)}句`);
        nextEmpty += 1;
        return toolCall(
          'sudoku_place',
          JSON.stringify({ row: cell.row + 1, col: cell.col + 1, value: solution[cell.row][cell.col] }),
        );
      }
      if (names.includes('sudoku_new') && !opened) {
        opened = true;
        return toolCall('sudoku_new', '{}');
      }
      return saying(`第${(texts += 1)}句`);
    });
    const toolkit = createSudokuToolkit({ nextSeed: () => 7 });
    const root = new Context();
    await startLlm(root, { llm: { complete }, game: toolkit });

    for (let i = 0; i < 20; i += 1) {
      root.emit('playback/started', { sentence: `句${i}`, waiting: 0 });
      await flush();
    }

    expect(nextEmpty).toBe(empties.length);
    expect(toolkit.isActive()).toBe(false);
    expect(toolkit.describe()).toContain('上一局已完成');
  });

  it('播报中到达的 SC 不开窗，下一个窗口读取时带上（与普通弹幕同一套机制）', async () => {
    const llm = createLlm(saying('先说'), saying('谢谢富哥'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });
    const superChat = { type: 'super-chat' as const, uid: 9, uname: '富哥', text: '唱一首', price: 30, timestamp: 0 };

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    root.emit('world/live-event', superChat);
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);

    root.emit('playback/completed', { sentence: '第一句' });
    root.emit('playback/started', { sentence: '第二句', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(2);
    expect(llm.complete.mock.calls[1][0].at(-1)).toEqual({
      role: 'user',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（直播状态：你已连着自聊 1 句；待播队列空）\n（接着讲你的故事）',
    });
    expect(listener).toHaveBeenLastCalledWith({ text: '谢谢富哥', replyTo: [] });
  });

  it('空闲时到达的 SC 唤醒一次窗口', async () => {
    const llm = createLlm(saying('谢谢富哥'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('world/live-event', {
      type: 'super-chat',
      uid: 9,
      uname: '富哥',
      text: '唱一首',
      price: 30,
      timestamp: 0,
    });
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'danmaku' }]]);
    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（直播状态：待播队列空）\n（接着讲你的故事）',
    });
  });

  it('上舰进缓冲：空闲时开窗，窗口里按「[上舰] 舰长」呈现', async () => {
    const llm = createLlm(saying('哟，上舰了'));
    const root = new Context();
    const windows = vi.fn();
    root.on('llm/window', windows);
    await startLlm(root, { llm });

    root.emit('world/live-event', {
      type: 'guard',
      uid: 7,
      uname: '阿紫',
      guardLevel: 3,
      num: 1,
      price: 138000,
      timestamp: 0,
    });
    await flush();

    expect(windows.mock.calls).toEqual([[{ source: 'danmaku' }]]);
    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '[#7] 阿紫: [上舰] 舰长\n（直播状态：待播队列空）\n（接着讲你的故事）',
    });
  });
});
