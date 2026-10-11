import { describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, ToolDefinition } from './client.ts';
import { replyToWindow } from './reply.ts';

const event = (uname: string, text: string) => ({ type: 'danmaku' as const, uid: 1, uname, text, timestamp: 0 });
const say = (content: string | null): AssistantReply => ({ content, toolCalls: [] });
const callTool = (name: string, args = '{}', id = 'c1') => ({
  id,
  type: 'function' as const,
  function: { name, arguments: args },
});
const callOf = (complete: { mock: { calls: unknown[][] } }, index: number) =>
  complete.mock.calls[index] as unknown as [ChatMessage[], ToolDefinition[] | undefined];

describe('replyToWindow', () => {
  it('去掉模型回复首尾的空白与换行', async () => {
    const complete = vi.fn(async () => say('\n\n  刚吃完凉面  \n'));
    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply).toEqual({ text: '刚吃完凉面', replyTo: [] });
  });

  it('弹幕直接进 user 消息：没有 read_danmaku 工具，一轮就说完', async () => {
    const complete = vi.fn(async () => say('好'));
    const reply = await replyToWindow({ complete }, () => [event('测试观众', '今天唱什么歌？')], ['前一句']);

    const [messages, tools] = callOf(complete, 0);
    expect(tools).toEqual([]);
    expect(messages).toEqual([
      { role: 'system', content: expect.any(String) },
      { role: 'assistant', content: '前一句' },
      { role: 'user', content: '[#1] 测试观众: 今天唱什么歌？\n（接着讲你的故事）' },
    ]);
    expect(reply).toEqual({ text: '好', replyTo: [event('测试观众', '今天唱什么歌？')] });
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('分层的呈现方式：相关逐条，气氛聚合成一行计数', async () => {
    const complete = vi.fn(async () => say('好'));
    const asked = event('测试观众', '今天唱什么歌？');
    await replyToWindow({ complete }, () => [asked, event('路人', '晚安')], []);

    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '[#1] 测试观众: 今天唱什么歌？\n（另有 1 条闲聊）\n（接着讲你的故事）',
    });
  });

  it('没有条目时不注入弹幕段', async () => {
    const complete = vi.fn(async () => say('那我接着讲'));
    await replyToWindow({ complete }, () => [], []);

    expect(callOf(complete, 0)[0].at(-1)).toEqual({ role: 'user', content: '（接着讲你的故事）' });
  });

  it('节奏水位进 user 段：排在弹幕之后、窗口提示之前', async () => {
    const complete = vi.fn(async () => say('好'));
    await replyToWindow({ complete }, () => [event('a', '在吗？')], [], {
      statusContext: '（直播状态：待播队列空）',
    });

    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '[#1] a: 在吗？\n（直播状态：待播队列空）\n（接着讲你的故事）',
    });
  });

  it('沉默是合法态：空回复直接结束，不再追问"必须开口"', async () => {
    const complete = vi.fn(async () => say(''));

    const reply = await replyToWindow({ complete }, () => [event('a', 'b')], []);

    expect(reply.text).toBe('');
    expect(complete).toHaveBeenCalledTimes(1);
    const system = String(callOf(complete, 0)[0][0].content);
    expect(system).toContain('沉默是你的合法态');
    expect(system).not.toContain('没看过弹幕');
    expect(system).not.toContain('read_danmaku');
  });

  it('回复（空）同样视为沉默，一次调用结束', async () => {
    const complete = vi.fn(async () => say('（空）'));
    const asked = event('a', '在吗？');

    const reply = await replyToWindow({ complete }, () => [asked], []);

    expect(reply).toEqual({ text: '', replyTo: [asked] });
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('模型调未知工具（比如已经删掉的 read_danmaku）只拿到未知工具结果', async () => {
    const takeItems = vi.fn(() => [event('a', 'b')]);
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: null, toolCalls: [callTool('read_danmaku', '{}', 'x')] })
      .mockResolvedValueOnce(say('好'));

    await replyToWindow({ complete }, takeItems, []);

    expect(callOf(complete, 1)[0]).toContainEqual({ role: 'tool', tool_call_id: 'x', content: '（未知工具）' });
  });

  it('工具调用轮次有上限，超过后不再提供工具，只能输出文字', async () => {
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>();
    for (let i = 0; i < 3; i += 1) complete.mockResolvedValueOnce({ content: null, toolCalls: [callTool('dance', '{}', `c${i}`)] });
    complete.mockResolvedValueOnce(say('结束了'));

    const reply = await replyToWindow({ complete }, () => [], []);

    expect(reply.text).toBe('结束了');
    expect(callOf(complete, 3)[1]).toBeUndefined();
  });

  it('SC 按普通弹幕呈现，不再附"优先回应"提示', async () => {
    const complete = vi.fn(async () => say('谢谢富哥'));
    const superChat = { type: 'super-chat' as const, uid: 9, uname: '富哥', text: '唱一首', price: 30, timestamp: 0 };

    const reply = await replyToWindow({ complete }, () => [superChat], []);

    expect(reply).toEqual({ text: '谢谢富哥', replyTo: [superChat] });
    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '[#9] [SC ¥30] 富哥: 唱一首\n（接着讲你的故事）',
    });
    // 钱不产生回复义务
    const system = String(callOf(complete, 0)[0][0].content);
    expect(system).not.toContain('读到 SC');
    expect(JSON.stringify(callOf(complete, 0)[0])).not.toContain('优先回应');
  });

  it('上舰按「[上舰] 舰长」呈现，等级决定称呼', async () => {
    const complete = vi.fn(async () => say('哟，上舰了'));
    const guard = { type: 'guard' as const, uid: 7, uname: '阿紫', guardLevel: 3, num: 2, price: 276000, timestamp: 0 };
    const admiral = { type: 'guard' as const, uid: 8, uname: '乙', guardLevel: 2, num: 1, price: 1998000, timestamp: 0 };

    await replyToWindow({ complete }, () => [guard, admiral], []);

    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '[#7] 阿紫: [上舰] 舰长×2\n[#8] 乙: [上舰] 提督\n（接着讲你的故事）',
    });
  });

  it('礼物按礼物格式呈现', async () => {
    const complete = vi.fn(async () => say('谢谢火箭'));
    const gift = { type: 'gift' as const, uid: 2, uname: '土豪', giftName: '火箭', num: 1, price: 1000, coinType: 'gold', timestamp: 0 };

    await replyToWindow({ complete }, () => [gift], []);

    expect(callOf(complete, 0)[0].at(-1)).toEqual({
      role: 'user',
      content: '[#2] 土豪: [礼物] 火箭×1\n（接着讲你的故事）',
    });
  });

  it('机械唤起：档案首行与弹幕同一段进 user 消息，一轮就能看到', async () => {
    const complete = vi.fn(async () => say('哦，是你'));
    const viewerLines = vi.fn(async (uids: number[]) => (uids.includes(1) ? '[#1] 常客，爱发猫图' : ''));

    await replyToWindow({ complete }, () => [event('猫奴', '我家猫又上桌了')], [], {
      viewerUids: [1],
      memory: { tools: [], run: async () => '', prompt: () => '', viewerLines },
    });

    expect(viewerLines).toHaveBeenCalledWith([1]);
    const user = String(callOf(complete, 0)[0].at(-1)?.content);
    expect(user).toContain('（你记得的观众：\n[#1] 常客，爱发猫图）');
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('没有首次出现的观众时不注入，也不白调 viewerLines', async () => {
    const complete = vi.fn(async () => say('好'));
    const viewerLines = vi.fn(async () => '不该被调用');

    await replyToWindow({ complete }, () => [], [], {
      viewerUids: [],
      memory: { tools: [], run: async () => '', prompt: () => '', viewerLines },
    });

    expect(viewerLines).not.toHaveBeenCalled();
    expect(callOf(complete, 0)[0].at(-1)).toEqual({ role: 'user', content: '（接着讲你的故事）' });
  });

  it('带工具调用的那一轮里的文字也会保留，和之后的话按顺序拼接', async () => {
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: '顺便记一下', toolCalls: [callTool('memory_save', '{"text":"x"}')] })
      .mockResolvedValueOnce(say('记下了'));
    const memory = {
      tools: [{ type: 'function' as const, function: { name: 'memory_save', description: '存', parameters: {} } }],
      run: async () => '（已记住）',
      prompt: () => '',
    };

    const reply = await replyToWindow({ complete }, () => [], [], { memory });

    expect(reply.text).toBe('顺便记一下记下了');
  });

  it('没开局时不推她玩：只给可选菜单与开局工具，规则书与盘面都不注入', async () => {
    const complete = vi.fn(async () => say('好'));
    const run = vi.fn(() => '新的一局开始了');
    const game = {
      prompt: '（完整规则书：你正在玩数独，sudoku_place 落子…）',
      menu: '（本场可以玩的游戏：数独；想玩的时候自己开一局就行，不玩也没关系）',
      openers: ['sudoku_new'],
      tools: [
        { type: 'function' as const, function: { name: 'sudoku_new', description: '开一局', parameters: {} } },
        { type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } },
      ],
      run,
      describe: () => '（数独：还没有开局，可以调用 sudoku_new 开一局）',
      isActive: () => false,
    };

    await replyToWindow({ complete }, () => [], [], { game });

    // 只暴露开局工具
    expect(callOf(complete, 0)[1]?.map((tool) => tool.function.name)).toEqual(['sudoku_new']);
    // 系统提示词里是菜单，不是规则书
    const system = String(callOf(complete, 0)[0][0].content);
    expect(system).toContain('不玩也没关系');
    expect(system).not.toContain('完整规则书');
    // 盘面描述（"还没有开局，可以调用 sudoku_new 开一局"）也不注入
    expect(String(callOf(complete, 0)[0].at(-1)?.content)).not.toContain('还没有开局');
  });

  it('有游戏时只提供游戏工具，工具调用交给游戏执行，并允许更多轮次', async () => {
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
        toolCalls: [callTool('sudoku_place', '{"row":1,"col":1,"value":5}', `g${i}`)],
      });
    }
    complete.mockResolvedValueOnce(say('完成'));

    const reply = await replyToWindow({ complete }, () => [], [], { game });

    expect(run).toHaveBeenCalledTimes(5);
    expect(run).toHaveBeenCalledWith('sudoku_place', '{"row":1,"col":1,"value":5}');
    expect(reply.text).toBe('第0步第1步第2步第3步第4步完成');
    expect(callOf(complete, 0)[1]?.map((tool) => tool.function.name)).toEqual(['sudoku_place']);
  });

  it('一轮里的多个游戏动作只执行第一个，下一轮恢复', async () => {
    const run = vi.fn<(name: string, rawArguments: string) => string>(() => '已填入');
    const place = { type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } };
    const game = { prompt: '（游戏提示词）', tools: [place], run, describe: () => '（数独进行中）', isActive: () => true };
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({ content: '想想', toolCalls: [callTool('sudoku_place', '{"row":1,"col":0}', 'c0'), callTool('sudoku_place', '{"row":1,"col":1}', 'c1')] })
      .mockResolvedValueOnce({ content: null, toolCalls: [callTool('sudoku_place', '{"row":1,"col":2}', 'c2')] })
      .mockResolvedValueOnce(say('填好了'));

    await replyToWindow({ complete }, () => [], [], { game });

    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls.map((call) => call[1])).toEqual(['{"row":1,"col":0}', '{"row":1,"col":2}']);
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
      .mockResolvedValueOnce({ content: '先看看', toolCalls: [callTool('sudoku_look', '{}', 'g0')] })
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
      .mockResolvedValueOnce({ content: null, toolCalls: [callTool('memory_save', '{"text":"x"}')] })
      .mockResolvedValueOnce(say('回应了'));
    const memory = {
      tools: [{ type: 'function' as const, function: { name: 'memory_save', description: '存', parameters: {} } }],
      run: async () => '（已记住）',
      prompt: () => '',
    };

    await replyToWindow({ complete }, () => [], [], { waitQuiet, memory });

    expect(waitQuiet).not.toHaveBeenCalled();
  });

  it('话题文案：只有开场窗口带话题池素材', async () => {
    const complete = vi.fn(async () => say('好'));
    await replyToWindow({ complete }, () => [], [], { topic: '天气' });
    expect(callOf(complete, 0)[0].at(-1)).toEqual({ role: 'user', content: '（开始直播了，开个头，可以聊聊：天气）' });

    // 已删除按句数换话题的机制：中段窗口只有"接着讲你的故事"
    await replyToWindow({ complete }, () => [], [], {});
    expect(callOf(complete, 1)[0].at(-1)).toEqual({ role: 'user', content: '（接着讲你的故事）' });
  });

  it('每窗口注入记忆上下文：开场带摘要，普通窗口带话题与上一句', async () => {
    const complete = vi.fn(async () => say('好'));
    const context = vi.fn(
      async (input: { topic?: string; lastSpoken?: string; opening: boolean }) =>
        input.opening ? '（你之前讲过：打印机）' : `（相关：${input.topic ?? ''}|${input.lastSpoken ?? ''}）`,
    );
    const memory = { tools: [], run: async () => '', prompt: () => '', context };

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
      prompt: () => '',
    };
    const place = { type: 'function' as const, function: { name: 'sudoku_place', description: '落子', parameters: {} } };
    const game = { prompt: '', tools: [place], run: () => '已填入', describe: () => '', isActive: () => true };
    const complete = vi
      .fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>()
      .mockResolvedValueOnce({
        content: null,
        toolCalls: [callTool('sudoku_place', '{}', 'g0'), callTool('memory_save', '{"text":"x"}', 'm0')],
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
});
