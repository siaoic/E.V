import type { ChatMessage, LlmPort, ToolDefinition } from '../llm/client.ts';

export const SILENT = '（空）';

export type AgentTool = {
  name: string;
  definition: ToolDefinition;
  /** 动作类工具：一轮只执行一个，且执行前等上一句播完（阶段纪律：说 → 等播完 → 动） */
  action?: boolean;
  run(args: string): Promise<string>;
};

export type AgentLoopOptions = {
  llm: LlmPort;
  messages: ChatMessage[];
  tools: AgentTool[];
  /** 超过这个轮次后不再提供工具，逼模型收口 */
  maxRounds: number;
  say?: (sentence: string) => void;
  waitQuiet?: () => Promise<void>;
  /** 首轮沉默且没调过工具时的督促语；不给就不督促 */
  nudge?: string;
};

export type AgentLoopResult = { sentences: string[] };

// 架构图的 Agent Loop 节点：推理回合与工具分发的裁决引擎，对入口（直播/游戏/dream）零依赖。
// 说出口的句子立即交出去播报，与后续工具动作重叠，解说不落后于动作。
export async function runAgentLoop(options: AgentLoopOptions): Promise<AgentLoopResult> {
  const { llm, messages, tools, maxRounds } = options;
  const byName = new Map(tools.map((tool) => [tool.name, tool]));
  const sentences: string[] = [];
  let toolCalled = false;
  let nudged = false;

  for (let round = 0; ; round += 1) {
    const definitions: ToolDefinition[] | undefined = round < maxRounds ? tools.map((tool) => tool.definition) : undefined;
    const reply = await llm.complete(messages, definitions);
    const sentence = (reply.content ?? '').trim();
    if (sentence !== '' && sentence !== SILENT) {
      sentences.push(sentence);
      options.say?.(sentence);
    }

    if (definitions && reply.toolCalls.length > 0) {
      messages.push({ role: 'assistant', content: reply.content, tool_calls: reply.toolCalls });
      const results: string[] = [];
      let actionDone = false;
      for (const call of reply.toolCalls) {
        const tool = byName.get(call.function.name);
        if (!tool) {
          results.push('（未知工具）');
          continue;
        }
        if (tool.action) {
          if (actionDone) {
            results.push('（一次只能做一个游戏动作，先说说你的想法）');
            continue;
          }
          actionDone = true;
          await options.waitQuiet?.();
        }
        toolCalled = true;
        results.push(await tool.run(call.function.arguments));
      }
      for (let i = 0; i < reply.toolCalls.length; i += 1) {
        messages.push({ role: 'tool', tool_call_id: reply.toolCalls[i].id, content: results[i] });
      }
      messages.push({ role: 'user', content: '（工具结果已返回，接着说）' });
      continue;
    }

    if (sentences.length > 0) return { sentences };
    if (toolCalled || nudged || !options.nudge) return { sentences };
    nudged = true;
    messages.push({ role: 'user', content: options.nudge });
  }
}
