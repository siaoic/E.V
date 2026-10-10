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

    ctx.on('agent/reply', (reply: AgentReply) => playNext(queue.enqueue(reply.text)));
  },
  { inject: ['speaker'] },
);

export const config = {};
