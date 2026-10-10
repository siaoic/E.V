import type { Context } from '@deepseek-ai/cordis';
import type { Speaker } from '../45-playback/plugin.ts';
import { createQwenTtsSpeaker } from './qwen-tts.ts';

export type TtsPluginConfig = {
  /** 'silent'（默认，控制台替身）或 'qwen' */
  mode?: string;
  url?: string;
};

declare module '@deepseek-ai/cordis' {
  interface Context {
    speaker: Speaker;
  }
}

export const apply = Object.assign(
  function tts(ctx: Context, config: TtsPluginConfig) {
    const speaker: Speaker =
      config.mode === 'qwen'
        ? createQwenTtsSpeaker({ url: config.url?.trim() || 'http://127.0.0.1:8756' })
        : { speak: () => new Promise((resolve) => setTimeout(resolve, 800)) };
    ctx.provide('speaker', speaker);
  },
  { inject: [] as string[] },
);

export const config = {
  mode: '${TTS_MODE:-silent}',
  url: '${TTS_URL:-}',
};
