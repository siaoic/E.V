import { describe, expect, it } from 'vitest';
import { defaultExtraBody, parseExtraBody } from './client-plugin.ts';

describe('llm client 的 extraBody 解析', () => {
  it('智谱端点未配置 LLM_EXTRA_BODY 时默认关闭 thinking', () => {
    // 不关 thinking 时 GLM 响应常超过 7 秒，每个窗口都撞超时 → 连续空窗口 → 链条停摆不说话
    expect(defaultExtraBody('https://open.bigmodel.cn/api/paas/v4/')).toEqual({ thinking: { type: 'disabled' } });
    expect(defaultExtraBody('https://open.bigmodel.cn/api/coding/paas/v4')).toEqual({ thinking: { type: 'disabled' } });
  });

  it('非智谱端点不塞 thinking（OpenAI 兼容端点会回 400 unsupported_parameter）', () => {
    expect(defaultExtraBody('https://developer.amd.com.cn/radeon/api/v1')).toBeUndefined();
    expect(defaultExtraBody('http://127.0.0.1:8080/v1')).toBeUndefined();
  });

  it('显式配置优先，空白配置视为未配置', () => {
    expect(parseExtraBody('{"thinking":{"type":"enabled"}}')).toEqual({ thinking: { type: 'enabled' } });
    expect(parseExtraBody('   ')).toBeUndefined();
    expect(parseExtraBody(undefined)).toBeUndefined();
  });
});
