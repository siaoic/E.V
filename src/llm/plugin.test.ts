import { Context } from '@deepseek-ai/cordis';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, LlmPort, ToolDefinition } from './client.ts';
import { createPuzzle, createRandom } from '../games/sudoku/engine.ts';
import { createSudokuToolkit } from '../games/sudoku/tools.ts';
import { apply } from './plugin.ts';

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

const readCall = { id: 'call_1', type: 'function' as const, function: { name: 'read_danmaku', arguments: '{}' } };
const reading: AssistantReply = { content: null, toolCalls: [readCall] };
const saying = (content: string): AssistantReply => ({ content, toolCalls: [] });

// 插件改为服务注入：先 provide，再启动
async function startLlm(
  root: Context,
  options: { llm: LlmPort; topics?: string[]; game?: unknown; memory?: unknown },
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

  it('播报中的弹幕只进缓冲不开窗，下一句播报开始时窗口读取它们', async () => {
    const llm = createLlm(saying('第一句续'), reading, saying('回应大家'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    const first = danmaku('甲', 'a', 1);
    const second = danmaku('乙', 'b', 2);
    root.emit('world/live-event', first);
    root.emit('world/live-event', second);
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);

    root.emit('playback/started', { sentence: '第一句续', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(3);
    expect(llm.complete.mock.calls[2][0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'call_1',
      content: '[#1] a: 甲\n[#2] b: 乙',
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

    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({ role: 'user', content: '（接着讲你的故事）' });
    expect(listener).toHaveBeenCalledWith({ text: '接着讲', replyTo: [] });
  });

  it('与最近说过的话重复的回复不发出', async () => {
    const llm = createLlm(saying('哈哈哈'), saying('哈哈哈'), saying('换个说法'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '一', waiting: 0 });
    await flush();
    root.emit('playback/started', { sentence: '二', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(3);
    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenLastCalledWith({ text: '换个说法', replyTo: [] });
  });

  it('链条窗口没有产出话时自动重试，保持自言自语不断', async () => {
    const llm = createLlm(saying(''), saying(''), saying('接着讲'));
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();

    expect(llm.complete).toHaveBeenCalledTimes(3);
    expect(listener).toHaveBeenCalledWith({ text: '接着讲', replyTo: [] });
  });

  it('连续空窗口达到上限后停止，等新弹幕重新唤醒', async () => {
    const llm = createLlm(...Array.from({ length: 8 }, () => saying('')));
    const root = new Context();
    const windows = vi.fn();
    const listener = vi.fn();
    root.on('llm/window', windows);
    root.on('agent/reply', listener);
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    expect(windows.mock.calls.map((call) => call[0].source)).toEqual(['playback', 'playback', 'playback', 'playback']);
    expect(listener).not.toHaveBeenCalled();

    root.emit('playback/completed', { sentence: '第一句' });
    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();
    expect(windows.mock.calls.at(-1)).toEqual([{ source: 'start' }]);

    root.emit('world/live-event', danmaku('还在吗'));
    await flush();
    expect(windows.mock.calls.at(-1)).toEqual([{ source: 'danmaku' }]);
    expect(listener).toHaveBeenCalledWith({ text: '好', replyTo: [] });
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

  it('播出 8 句后的下一个窗口提示换话题，之后重新计数', async () => {
    const replies = ['一', '二', '三', '四', '五', '六', '七', '八', '九'].map((text) => saying(text));
    const llm = createLlm(...replies);
    const root = new Context();
    await startLlm(root, { llm });

    for (let i = 0; i < 9; i += 1) {
      root.emit('playback/started', { sentence: `第${i}句`, waiting: 0 });
      await flush();
    }

    expect(llm.complete.mock.calls[7][0].at(-1)).toEqual({ role: 'user', content: '（接着讲你的故事）' });
    expect(llm.complete.mock.calls[8][0].at(-1)).toEqual({
      role: 'user',
      content: '（你已经聊这个话题有一阵子了，自然地换一个新话题吧）',
    });
  });

  it('读取只返回未读过的弹幕，已回应过的不会再次出现', async () => {
    const llm = createLlm(reading, saying('好'), reading, saying('再来一句'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('world/live-event', danmaku('第一条', 'a', 1));
    await flush();
    root.emit('playback/completed', { sentence: '好' });
    root.emit('playback/started', { sentence: '再来', waiting: 0 });
    await flush();

    expect(llm.complete.mock.calls[3][0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'call_1',
      content: '（当前没有新弹幕）',
    });
  });

  it('缓冲只保留最近 20 条，读取时取最新 10 条', async () => {
    const llm = createLlm(saying('先说'), reading, saying('好的'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('playback/started', { sentence: '第一句', waiting: 0 });
    await flush();
    for (let i = 0; i <= 20; i += 1) root.emit('world/live-event', danmaku(`d${i}`, 'u'));
    root.emit('playback/started', { sentence: '第二句', waiting: 0 });
    await flush();

    const toolMessage = llm.complete.mock.calls[2][0].find((message) => message.role === 'tool');
    const lines = (toolMessage as { content: string }).content.split('\n');
    expect(lines).toHaveLength(10);
    expect(lines[0]).toBe('[#123456] u: d11');
    expect(lines[9]).toBe('[#123456] u: d20');
  });

  it('银瓜子礼物忽略，金瓜子 ≥1000 的礼物进缓冲并唤醒窗口', async () => {
    const llm = createLlm(saying('谢谢火箭'));
    const root = new Context();
    await startLlm(root, { llm });

    root.emit('world/live-event', gift({ price: 100, coinType: 'silver' }));
    await flush();
    expect(llm.complete).not.toHaveBeenCalled();

    root.emit('world/live-event', gift({ giftName: '火箭', num: 1 }));
    await flush();
    expect(llm.complete).toHaveBeenCalledTimes(1);
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

  it('话题池：开场窗口取话题，池取尽后回退默认文案', async () => {
    // 每个空窗口会先空回一次、再被"必须开口"提示重调一次，即 2 次调用；4 个空窗口后链条停摆。
    // messages 会被原地修改，必须在调用时快照最后一条消息
    const replies = [...Array.from({ length: 8 }, () => saying('')), saying('终于说话了')];
    const seen: string[] = [];
    let index = 0;
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async (messages) => {
      const last = messages.at(-1)!.content;
      seen.push(typeof last === 'string' ? last : '');
      const reply = replies[index] ?? saying('好');
      index += 1;
      return reply;
    });
    const root = new Context();
    const listener = vi.fn();
    root.on('agent/reply', listener);
    await startLlm(root, { llm: { complete }, topics: ['天气', '午饭'] });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();
    expect(complete).toHaveBeenCalledTimes(8);
    expect(listener).not.toHaveBeenCalled();

    root.emit('world/live-event', { type: 'heartbeat', count: 2, timestamp: 1 });
    await flush();
    expect(listener).toHaveBeenCalledWith({ text: '终于说话了', replyTo: [] });

    const openings = seen.filter((content) => content.includes('可以聊聊：'));
    expect(openings).toHaveLength(2);
    expect(openings[0]).not.toBe(openings[1]);
    expect(openings[0]).toMatch(/^（开始直播了，开个头，可以聊聊：(天气|午饭)）$/);
    expect(seen.some((content) => content === '（接着讲你的故事）')).toBe(true);
  });

  it('开场窗口注入记忆上下文', async () => {
    const llm = createLlm(saying('好'));
    const root = new Context();
    await startLlm(root, {
      llm,
      memory: {
        tools: [],
        run: async () => '',
        context: async () => '（你之前讲过：打印机）',
      },
    });

    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await flush();

    expect(llm.complete.mock.calls[0][0].at(-1)).toEqual({
      role: 'user',
      content: '（你之前讲过：打印机）\n（接着讲你的故事）',
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
      if (!tools) return saying(`第${(texts += 1)}句`);
      if (!opened) {
        opened = true;
        return toolCall('sudoku_new', '{}');
      }
      const cell = empties[nextEmpty];
      if (!cell) return saying(`第${(texts += 1)}句`);
      nextEmpty += 1;
      return toolCall('sudoku_place', JSON.stringify({ row: cell.row + 1, col: cell.col + 1, value: solution[cell.row][cell.col] }));
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

  it('播报中到达的 SC 不开窗，下一个窗口读取时带上并优先回应', async () => {
    const llm = createLlm(saying('先说'), reading, saying('谢谢富哥'));
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

    expect(llm.complete).toHaveBeenCalledTimes(3);
    expect(llm.complete.mock.calls[2][0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'call_1',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（有 SC，请优先回应）',
    });
    expect(listener).toHaveBeenLastCalledWith({ text: '谢谢富哥', replyTo: [] });
  });

  it('空闲时到达的 SC 唤醒一次窗口', async () => {
    const llm = createLlm(reading, saying('谢谢富哥'));
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
    expect(llm.complete.mock.calls[1][0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'call_1',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（有 SC，请优先回应）',
    });
  });
});
