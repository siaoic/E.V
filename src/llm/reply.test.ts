import { describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, ToolCall, ToolDefinition } from './client.ts';
import { replyToWindow } from './reply.ts';

const event = (uname: string, text: string) => ({ type: 'danmaku' as const, uid: 1, uname, text, timestamp: 0 });
const say = (content: string | null): AssistantReply => ({ content, toolCalls: [] });
const readCall = (id = 'call_1'): ToolCall => ({ id, type: 'function', function: { name: 'read_danmaku', arguments: '{}' } });
const callOf = (complete: { mock: { calls: unknown[][] } }, index: number) =>
  complete.mock.calls[index] as unknown as [ChatMessage[], ToolDefinition[] | undefined];

describe('replyToWindow', () => {
  it('去掉模型回复首尾的空白与换行', async () => {
    const complete = vi.fn(async () => say('\n\n  刚吃完凉面  \n'));
    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply).toEqual({ text: '刚吃完凉面', replyTo: [] });
  });

  it('默认窗口提供 read_danmaku 工具，用户消息为讲故事而不是弹幕列表', async () => {
    const complete = vi.fn(async () => say('好'));
    await replyToWindow({ complete }, () => [event('a', 'b')], ['前一句']);

    const [messages, tools] = callOf(complete, 0);
    expect(tools?.map((tool) => tool.function.name)).toEqual(['read_danmaku']);
    expect(messages).toEqual([
      { role: 'system', content: expect.any(String) },
      { role: 'assistant', content: '前一句' },
      { role: 'user', content: '（接着讲你的故事）' },
    ]);
  });

  it('需要换话题时用户消息提示换个新话题', async () => {
    const complete = vi.fn(async () => say('新话题来了'));
    await replyToWindow({ complete }, () => [], [], { changeTopic: true });

    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '（你已经聊这个话题有一阵子了，自然地换一个新话题吧）',
    });
  });

  it('模型调用 read_danmaku 后拿到弹幕作为工具结果，再据此开口', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('谢谢你的弹幕'));
    const batch = [event('测试观众', '今天唱什么歌'), event('路人', '晚安')];

    const reply = await replyToWindow({ complete }, () => batch, []);

    expect(reply).toEqual({ text: '谢谢你的弹幕', replyTo: batch });
    const [secondMessages] = callOf(complete, 1);
    expect(secondMessages).toContainEqual({
      role: 'tool',
      tool_call_id: 'c1',
      content: '[#1] 测试观众: 今天唱什么歌\n[#1] 路人: 晚安',
    });
    expect(secondMessages.at(-1)).toEqual({ role: 'user', content: '（工具结果已返回，接着说）' });
  });

  it('读取弹幕为空时工具结果说明没有新弹幕', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('那我接着讲'));

    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply).toEqual({ text: '那我接着讲', replyTo: [] });
    expect(callOf(complete, 1)[0]).toContainEqual({ role: 'tool', tool_call_id: 'c1', content: '（当前没有新弹幕）' });
  });

  it('没读过弹幕时不允许沉默：空回复后追加一次开口提示再生成', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce(say('（空）'))
      .mockResolvedValueOnce(say('我接着讲'));

    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply).toEqual({ text: '我接着讲', replyTo: [] });
    expect(callOf(complete, 1)[0].at(-1)).toEqual({ role: 'user', content: '（这一句必须开口，说一两句接着讲）' });
  });

  it('提示后仍然沉默则返回空，不再无限重试', async () => {
    const complete = vi.fn(async () => say(''));

    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply).toEqual({ text: '', replyTo: [] });
    expect(complete).toHaveBeenCalledTimes(2);
  });

  it('读过弹幕后，模型可以以（空）保持沉默，不追加开口提示', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall()] })
      .mockResolvedValueOnce(say('（空）'));

    const reply = await replyToWindow({ complete }, () => [event('a', 'b')], []);

    expect(reply).toEqual({ text: '', replyTo: [event('a', 'b')] });
    expect(complete).toHaveBeenCalledTimes(2);
  });

  it('未知工具名返回未知工具结果，不读取弹幕', async () => {
    const readDanmaku = vi.fn(() => [event('a', 'b')]);
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({
        content: null,
        toolCalls: [{ id: 'x', type: 'function', function: { name: 'dance', arguments: '{}' } }],
      })
      .mockResolvedValueOnce(say('好'));

    await replyToWindow({ complete }, readDanmaku, []);

    expect(readDanmaku).not.toHaveBeenCalled();
    expect(callOf(complete, 1)[0]).toContainEqual({ role: 'tool', tool_call_id: 'x', content: '（未知工具）' });
  });

  it('工具调用轮次有上限，超过后不再提供工具，只能输出文字', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValue({ content: null, toolCalls: [readCall()] });
    complete.mockResolvedValueOnce({ content: null, toolCalls: [readCall('c0')] });
    complete.mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] });
    complete.mockResolvedValueOnce({ content: null, toolCalls: [readCall('c2')] });
    complete.mockResolvedValueOnce(say('结束了'));

    const reply = await replyToWindow({ complete }, () => [event('a', 'b')], []);

    expect(reply.text).toBe('结束了');
    expect(callOf(complete, 3)[1]).toBeUndefined();
  });

  it('读到 SC 时以 SC 格式呈现，并提示优先回应', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('谢谢富哥'));
    const superChat = { type: 'super-chat' as const, uid: 9, uname: '富哥', text: '唱一首', price: 30, timestamp: 0 };

    const reply = await replyToWindow({ complete }, () => [superChat], []);

    expect(reply).toEqual({ text: '谢谢富哥', replyTo: [superChat] });
    expect(callOf(complete, 1)[0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'c1',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（有 SC，请优先回应）',
    });
  });

  it('读弹幕后附上记得的观众档案首行（机械唤起）', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('哦，是你'));
    const viewerLines = vi.fn(async (uids: number[]) =>
      uids.includes(1) ? '[#1] 常客，爱发猫图' : '',
    );

    await replyToWindow({ complete }, () => [event('猫奴', '我家猫又上桌了')], [], {
      memory: { tools: [], run: async () => '', viewerLines },
    });

    expect(viewerLines).toHaveBeenCalledWith([1]);
    const toolMessage = callOf(complete, 1)[0].find((m) => m.role === 'tool');
    expect(toolMessage?.content).toBe(
      '[#1] 猫奴: 我家猫又上桌了\n（你记得的观众：\n[#1] 常客，爱发猫图）',
    );
  });

  it('带工具调用的那一轮里的文字也会保留，和之后的话按顺序拼接', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: '哎哟，我看看弹幕', toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('谢谢大家'));

    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply.text).toBe('哎哟，我看看弹幕谢谢大家');
  });

  it('有游戏时提供游戏工具，工具调用交给游戏执行，并允许更多轮次', async () => {
    const run = vi.fn(() => '已填入第 1 行第 1 列 = 5');
    const game = {
      prompt: '（游戏提示词）',
      tools: [{ type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } }],
      run,
      describe: () => '（数独进行中）',
      isActive: () => true,
    };
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>();
    for (let i = 0; i < 5; i += 1) {
      complete.mockResolvedValueOnce({
        content: `第${i}步`,
        toolCalls: [
          { id: `g${i}`, type: 'function', function: { name: 'sudoku_place', arguments: '{"row":1,"col":1,"value":5}' } },
        ],
      });
    }
    complete.mockResolvedValueOnce(say('完成'));

    const reply = await replyToWindow({ complete }, () => [], [], { game });

    expect(run).toHaveBeenCalledTimes(5);
    expect(run).toHaveBeenCalledWith('sudoku_place', '{"row":1,"col":1,"value":5}');
    expect(reply.text).toBe('第0步第1步第2步第3步第4步完成');
    expect(callOf(complete, 0)[1]?.map((tool) => tool.function.name)).toEqual(['read_danmaku', 'sudoku_place']);
  });

  it('一轮里的多个游戏动作只执行第一个，下一轮恢复', async () => {
    const run = vi.fn<(name: string, rawArguments: string) => string>(() => '已填入');
    const place = { type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } };
    const game = { prompt: '（游戏提示词）', tools: [place], run, describe: () => '（数独进行中）', isActive: () => true };
    const placeCall = (id: string) => ({
      id,
      type: 'function' as const,
      function: { name: 'sudoku_place', arguments: `{"row":1,"col":${id},"value":5}` },
    });
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: '想想', toolCalls: [placeCall('c0'), placeCall('c1')] })
      .mockResolvedValueOnce({ content: null, toolCalls: [placeCall('c2')] })
      .mockResolvedValueOnce(say('填好了'));

    await replyToWindow({ complete }, () => [], [], { game });

    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls.map((call) => call[1])).toEqual(['{"row":1,"col":c0,"value":5}', '{"row":1,"col":c2,"value":5}']);
    expect(callOf(complete, 1)[0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'c1',
      content: '（一次只能做一个游戏动作，先说说你的想法）',
    });
  });

  it('每产出一句话立即通过 say 交出，顺序为先说后做', async () => {
    const order: string[] = [];
    const run = vi.fn<(name: string, rawArguments: string) => string>((name) => {
      order.push(`do:${name}`);
      return 'ok';
    });
    const game = {
      prompt: '（游戏提示词）',
      tools: [{ type: 'function' as const, function: { name: 'sudoku_look', description: '看', parameters: {} } }],
      run,
      describe: () => '（数独进行中）',
      isActive: () => true,
    };
    const onSpeech = vi.fn((text: string) => order.push(`say:${text}`));
    const waitQuiet = vi.fn(async () => {
      order.push('quiet');
    });
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({
        content: '先看看',
        toolCalls: [{ id: 'g0', type: 'function', function: { name: 'sudoku_look', arguments: '{}' } }],
      })
      .mockResolvedValueOnce(say('填这里'));

    const reply = await replyToWindow({ complete }, () => [], [], { game, say: onSpeech, waitQuiet });

    expect(onSpeech.mock.calls.map((call) => call[0])).toEqual(['先看看', '填这里']);
    expect(order).toEqual(['say:先看看', 'quiet', 'do:sudoku_look', 'say:填这里']);
    expect(reply.text).toBe('先看看填这里');
  });

  it('没有游戏时不等待语音背压', async () => {
    const waitQuiet = vi.fn(async () => {});
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall()] })
      .mockResolvedValueOnce(say('回应了'));

    await replyToWindow({ complete }, () => [], [], { waitQuiet });

    expect(waitQuiet).not.toHaveBeenCalled();
  });

  it('话题文案：开场带话题、换话题带话题', async () => {
    const complete = vi.fn(async () => say('好'));
    await replyToWindow({ complete }, () => [], [], { topic: '天气' });
    expect(callOf(complete, 0)[0].at(-1)).toEqual({ role: 'user', content: '（开始直播了，开个头，可以聊聊：天气）' });

    await replyToWindow({ complete }, () => [], [], { changeTopic: true, topic: '午饭' });
    expect(callOf(complete, 1)[0].at(-1)).toEqual({
      role: 'user',
      content: '（你已经聊这个话题有一阵子了，自然地换个新话题吧，可以聊聊：午饭）',
    });
  });

  it('每窗口注入记忆上下文：开场带摘要，普通窗口带话题与上一句', async () => {
    const complete = vi.fn(async () => say('好'));
    const context = vi.fn(
      async (input: { topic?: string; lastSpoken?: string; opening: boolean }) =>
        input.opening ? '（你之前讲过：打印机）' : `（相关：${input.topic ?? ''}|${input.lastSpoken ?? ''}）`,
    );
    const memory = { tools: [], run: async () => '', context };

    await replyToWindow({ complete }, () => [], [], { opening: true, memory });
    expect(context).toHaveBeenCalledWith({ topic: undefined, lastSpoken: undefined, opening: true });
    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '（你之前讲过：打印机）\n（接着讲你的故事）',
    });

    await replyToWindow({ complete }, () => [], ['上一句'], { topic: '天气', memory });
    expect(context).toHaveBeenCalledWith({ topic: '天气', lastSpoken: '上一句', opening: false });
    expect(callOf(complete, 1)[0].at(-1)).toEqual({
      role: 'user',
      content: '（相关：天气|上一句）\n（开始直播了，开个头，可以聊聊：天气）',
    });
  });

  it('人格前缀进系统提示词头部', async () => {
    const complete = vi.fn(async () => say('好'));
    const memory = { tools: [], run: async () => '', prompt: () => '（宪法种子）' };

    await replyToWindow({ complete }, () => [], [], { memory });

    expect(callOf(complete, 0)[0][0]).toEqual({
      role: 'system',
      content: expect.stringContaining('（宪法种子）\n\n你正在直播'),
    });
  });

  it('memory 工具分发给记忆执行，不受游戏动作限制', async () => {
    const run = vi.fn<(name: string, rawArguments: string) => Promise<string>>(async () => '（已记住）');
    const memory = {
      tools: [{ type: 'function' as const, function: { name: 'memory_save', description: '存', parameters: {} } }],
      run,
    };
    const place = { type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } };
    const game = { prompt: '', tools: [place], run: () => '已填入', describe: () => '', isActive: () => true };
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({
        content: null,
        toolCalls: [
          { id: 'g0', type: 'function', function: { name: 'sudoku_place', arguments: '{}' } },
          { id: 'm0', type: 'function', function: { name: 'memory_save', arguments: '{"text":"x"}' } },
        ],
      })
      .mockResolvedValueOnce(say('好'));

    await replyToWindow({ complete }, () => [], [], { game, memory });

    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith('memory_save', '{"text":"x"}');
  });

  it('有游戏时游戏提示词进入系统消息，盘面描述注入用户消息', async () => {
    const complete = vi.fn(async () => say('好'));
    const game = {
      prompt: '你正在玩数独：sudoku_place 落子。',
      tools: [],
      run: () => '',
      describe: () => '（数独进行中）',
      isActive: () => true,
    };

    await replyToWindow({ complete }, () => [], [], { game });

    const [messages] = callOf(complete, 0);
    expect(messages.at(-1)).toEqual({ role: 'user', content: '（数独进行中）\n（接着讲你的故事）' });
    expect(messages[0].content).toContain('sudoku_place');
  });

  it('礼物在工具结果中以礼物格式呈现', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [readCall('c1')] })
      .mockResolvedValueOnce(say('谢谢火箭'));
    const gift = { type: 'gift' as const, uid: 2, uname: '土豪', giftName: '火箭', num: 1, price: 1000, coinType: 'gold', timestamp: 0 };

    await replyToWindow({ complete }, () => [gift], []);

    expect(callOf(complete, 1)[0]).toContainEqual({
      role: 'tool',
      tool_call_id: 'c1',
      content: '[#2] 土豪: [礼物] 火箭×1',
    });
  });
});
