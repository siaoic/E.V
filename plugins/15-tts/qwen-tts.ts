import type { Speaker } from '../45-playback/plugin.ts';

// 服务端流式合成一句约 2-6 秒；15 秒兜底——TTS 挂死会卡住整条播报链，超时视为失败跳过
const REQUEST_TIMEOUT_MS = 15000;

// /speak 在服务端完成流式合成与播放（边推边播），响应返回即播放结束
export function createQwenTtsSpeaker(deps: {
  fetch?: typeof fetch;
  url?: string;
}): Speaker {
  const doFetch = deps.fetch ?? fetch;
  const url = deps.url ?? 'http://127.0.0.1:8756';
  return {
    async speak(sentence: string) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const response = await doFetch(`${url}/speak`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ text: sentence }),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`TTS request failed: ${response.status}`);
        await response.arrayBuffer();
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
