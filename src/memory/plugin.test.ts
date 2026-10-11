import { Context } from '@deepseek-ai/cordis';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AssistantReply, ChatMessage, LlmPort, ToolDefinition } from '../llm/client.ts';
import { apply as llmApply } from '../llm/plugin.ts';
import { apply } from './plugin.ts';

// 这个文件守的是「装配」而不是单元行为：hooks.test.ts 自己构造 helpers，
// 因此漏掉过两处真实断线——toolkit 没暴露 prompt()（宪法种子进不了提示词）、
// plugin.ts 没接 ViewerBook（观众档案与机械唤起整体失效）。
// 这里按 src/main.ts 的方式只 provide llm，其余走 memory 插件的 apply。
const stubLlm = (): LlmPort => ({
  complete: vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(async () => ({
    content: '',
    toolCalls: [],
  })),
});

async function bootMemory() {
  const dir = await mkdtemp(path.join(tmpdir(), 'memory-plugin-'));
  const root = new Context();
  root.provide('llm', stubLlm());
  const fiber = await root.plugin(apply, { file: path.join(dir, 'memory.jsonl') });
  return { dir, memory: root.memory, dispose: () => fiber.dispose() };
}

describe('memory plugin 装配', () => {
  let close: (() => Promise<void>) | undefined;

  afterEach(async () => {
    await close?.();
    close = undefined;
  });

  it('人格前缀可用：宪法种子进 prompt()', async () => {
    const booted = await bootMemory();
    close = booted.dispose;

    expect(booted.memory.prompt()).toContain('黛冬优子');
  });

  it('观众档案已接线：memory_save(viewer) 落盘且可机械唤起', async () => {
    const booted = await bootMemory();
    close = booted.dispose;

    const saved = await booted.memory.run(
      'memory_save',
      JSON.stringify({ text: '第一次来就送了舰长', kind: 'viewer', viewer: 42 }),
    );
    expect(saved).toBe('（已记进 #42 的观众档案：第一次来就送了舰长）');

    expect(await booted.memory.viewerLines([42])).toContain('第一次来就送了舰长');
    expect(await readFile(path.join(booted.dir, 'viewers', '42.md'), 'utf8')).toContain('第一次来就送了舰长');
  });

  it('persona_update 写进 PERSONA.md，并出现在之后的人格前缀里', async () => {
    const booted = await bootMemory();
    close = booted.dispose;

    await booted.memory.run('persona_update', JSON.stringify({ note: '我不喜欢被夸声音好听' }));

    expect(await readFile(path.join(booted.dir, 'PERSONA.md'), 'utf8')).toContain('我不喜欢被夸声音好听');
    expect(booted.memory.prompt()).toContain('我不喜欢被夸声音好听');
  });

  it('recall_viewer 能取回整档（不只首行）', async () => {
    const booted = await bootMemory();
    close = booted.dispose;

    await booted.memory.run('memory_save', JSON.stringify({ text: '常问关于猫的事', kind: 'viewer', viewer: 7 }));
    await booted.memory.run('memory_save', JSON.stringify({ text: '说自己是夜班', kind: 'viewer', viewer: 7 }));

    const profile = await booted.memory.run('recall_viewer', JSON.stringify({ uid: 7 }));
    expect(profile).toContain('常问关于猫的事');
    expect(profile).toContain('说自己是夜班');
  });

  // 端到端：llm 插件 + memory 插件真实装配，看宪法种子与自我认知是否真的进了模型的消息
  it('人格真的进模型：种子常驻系统提示词，persona_update 之后下一窗就带上', async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'persona-live-'));
    const calls: ChatMessage[][] = [];
    const complete = vi.fn<(messages: ChatMessage[], tools?: ToolDefinition[]) => Promise<AssistantReply>>(
      async (messages) => {
        calls.push(messages);
        return { content: '好', toolCalls: [] };
      },
    );
    const root = new Context();
    root.provide('llm', { complete });
    const memoryFiber = await root.plugin(apply, { file: path.join(dir, 'memory.jsonl') });
    const llmFiber = await root.plugin(llmApply, {});
    close = async () => {
      await llmFiber.dispose();
      await memoryFiber.dispose();
    };

    // 首个心跳开场
    root.emit('world/live-event', { type: 'heartbeat', count: 1, timestamp: 0 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(1));

    expect(calls[0][0].role).toBe('system');
    expect(String(calls[0][0].content)).toContain('黛冬优子');

    // 她说下一句自我认知，之后的新弹幕开窗必须能看到它
    await root.memory.run('persona_update', JSON.stringify({ note: '我不喜欢被夸声音好听' }));
    root.emit('world/live-event', { type: 'danmaku', uid: 5, uname: '路人', text: '晚上好', timestamp: 0 });
    await vi.waitFor(() => expect(complete).toHaveBeenCalledTimes(2));

    expect(String(calls[1][0].content)).toContain('我不喜欢被夸声音好听');
  });
});
