import type { Context } from '@deepseek-ai/cordis';
import type { AgentReply } from '../../src/llm/plugin.ts';
import { PlaybackQueue } from './playback-queue.ts';

export type Speaker = { speak(sentence: string): Promise<void> };

export type PlaybackStartedEvent = { sentence: string; waiting: number };

export type PlaybackCompletedEvent = { sentence: string };

declare module '@deepseek-ai/cordis' {
  interface Events {
    'playback/started'(event: PlaybackStartedEvent): void;
    'playback/completed'(event: PlaybackCompletedEvent): void;
  }
}

export type PlaybackPluginConfig = Record<string, never>;

export const apply = Object.assign(
  function playback(ctx: Context, _config: PlaybackPluginConfig) {
    const logger = ctx.logger('playback');
    const queue = new PlaybackQueue();

    async function play(sentence: string) {
      ctx.emit('playback/started', { sentence, waiting: queue.waiting });
      try {
        await ctx.speaker.speak(sentence);
      } catch (error) {
        logger.error(error);
      }
      ctx.emit('playback/completed', { sentence });
      playNext(queue.playbackCompleted());
    }

    function playNext(effects: ReturnType<PlaybackQueue['playbackCompleted']>) {
      for (const effect of effects) void play(effect.sentence);
    }

    // 用户决定（2026-10-11）：一律插到队首——最新产出的一句优先播，当前句一播完就接上它。
    // 排尾那行保留不删，需要回退时取消注释、把下面这行注掉即可。
    // 代价：同一批排了多句时会倒序（后生成的先播），细节见 playback-queue.ts 的注释。
    // ctx.on('agent/reply', (reply: AgentReply) => playNext(queue.enqueue(reply.text)));
    ctx.on('agent/reply', (reply: AgentReply) => playNext(queue.enqueueFront(reply.text)));
  },
  { inject: ['speaker'] },
);

export const config = {};
