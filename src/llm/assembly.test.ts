import { Context } from '@deepseek-ai/cordis';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, ToolDefinition } from './client.ts';
import { apply as llmApply } from './plugin.ts';
import { apply as memoryApply } from '../memory/plugin.ts';

// 装配级测试（对齐 src/memory/plugin.test.ts 的写法）：llm 插件 + memory 插件真实装配。
// 这一条专门挡「机械唤起挂在 read_danmaku 的工具结果上，删工具时被一起带走」——
// 单元测试用的是假 memory，真实断线只有装配起来才看得见（同类事故见 计划.md「人格层断线修复」）。
describe('机械唤起的装配（llm + memory）', () => {
  let close: (() => Promise<void>) | undefined;

  afterEach(async () => {
    await close?.();
    close = undefined;
  });

  // 模型从不调工具：机械唤起必须与 read_danmaku 无关
  async function boot() {
    const dir = await mkdtemp(path.join(tmpdir(), 'viewer-wake-'));
    const calls: ChatMessage[][] = [];
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(
      async (messages) => {
        calls.push(messages);
        return { content: '好', toolCalls: [] };
      },
    );
    const root = new Context();
    root.provide('llm', { complete });
    const memoryFiber = await root.plugin(memoryApply, { file: path.join(dir, 'memory.jsonl') });
    const llmFiber = await root.plugin(llmApply, {});
    close = async () => {
      await llmFiber.dispose();
      await memoryFiber.dispose();
    };
    return { root, calls, complete };
  }

  it('档案首行进 user 消息，模型一次工具都不调也能看到', async () => {
    const { root, calls, complete } = await boot();
    await root.memory.run('memory_save', JSON.stringify({ text: '第一次来就送了舰长', kind: 'viewer', viewer: 42 }));

    root.emit('world/live-event', { type: 'danmaku', uid: 42, uname: '舰长', text: '晚上好', timestamp: 0 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));

    // 本用例没有工具调用，messages 不会被原地追加，可以直接读最后一条
    const user = String(calls[0].at(-1)?.content);
    expect(user).toContain('（你记得的观众：');
    expect(user).toContain('第一次来就送了舰长');
    // 注入的是 user 段，不是系统提示词
    expect(String(calls[0][0].content)).not.toContain('你记得的观众');
  });

  it('同一位观众本场只唤起一次，换个人才会再唤起', async () => {
    const { root, calls, complete } = await boot();
    await root.memory.run('memory_save', JSON.stringify({ text: '爱发猫图', kind: 'viewer', viewer: 7 }));
    await root.memory.run('memory_save', JSON.stringify({ text: '上夜班的', kind: 'viewer', viewer: 8 }));

    root.emit('world/live-event', { type: 'danmaku', uid: 7, uname: '甲', text: '第一句', timestamp: 0 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));
    expect(String(calls[0].at(-1)?.content)).toContain('爱发猫图');

    // 同一个人再说话：不再重复喂同一份档案（注意只断言"你记得的观众"这个段头，
    // 别的记忆召回段落里出现同样的字眼不算）
    root.emit('world/live-event', { type: 'danmaku', uid: 7, uname: '甲', text: '第二句', timestamp: 1 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(2));
    expect(String(calls[1].at(-1)?.content)).not.toContain('你记得的观众');

    // 换一位有档案的观众：该唤起还是唤起
    root.emit('world/live-event', { type: 'danmaku', uid: 8, uname: '乙', text: '我也来了', timestamp: 2 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(3));
    expect(String(calls[2].at(-1)?.content)).toContain('上夜班的');
  });
});
