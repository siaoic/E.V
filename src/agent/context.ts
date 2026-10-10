import type { ChatMessage } from '../llm/client.ts';

export type ContextInput = {
  /** 宪法种子 + 自我认知（persona 层），渲染进系统消息头部 */
  personaPrompt?: string;
  /** 机械纪律（如直播的一句话约束） */
  disciplinePrompt: string;
  /** 游戏规则，紧贴纪律之后追加 */
  gamePrompt?: string;
  /** 已说过的句子，按序成 assistant 历史 */
  spoken?: string[];
  /** 盘面描述（游戏窗口） */
  gameDescribe?: string;
  /** 记忆召回注入（别重复摘要、相关记忆、dream 摘要） */
  memoryContext?: string;
  /** 本窗口的提示词，收尾 */
  windowPrompt: string;
};

// 架构图的「Agent Loop → 构建上下文 → 宪法·唤起」：种子在头部，纪律与游戏规则紧随，
// 历史按发言顺序，窗口提示收尾。token 裁剪将来也落在这里。
export function buildContext(input: ContextInput): ChatMessage[] {
  const discipline = input.gamePrompt ? `${input.disciplinePrompt}${input.gamePrompt}` : input.disciplinePrompt;
  const system = [input.personaPrompt?.trim(), discipline].filter(Boolean).join('\n\n');
  const userParts: string[] = [];
  if (input.gameDescribe !== undefined) userParts.push(input.gameDescribe);
  if (input.memoryContext) userParts.push(input.memoryContext);
  userParts.push(input.windowPrompt);
  return [
    { role: 'system', content: system },
    ...(input.spoken ?? []).map((sentence): ChatMessage => ({ role: 'assistant', content: sentence })),
    { role: 'user', content: userParts.join('\n') },
  ];
}
