import type { Context } from '@deepseek-ai/cordis';
import type { LiveEvent } from '../../plugins/90-danmaku/plugin.ts';
import type { DanmakuEvent, SuperChatEvent } from '../../plugins/90-danmaku/normalize.ts';
import type { GiftEvent } from '../../plugins/90-danmaku/gift-v2.ts';
import { SpeechGate } from './speech-gate.ts';
import type { LlmPort } from './client.ts';
import { replyToWindow, type GameHooks, type MemoryHooks } from './reply.ts';

const BUFFER_SIZE = 20;
const READ_LIMIT = 10;
const RECENT_SPEECH_SIZE = 20;
const MAX_EMPTY_WINDOWS = 3;
const SUPER_CHAT_QUEUE_SIZE = 10;
const TOPIC_SWITCH_EVERY = 8;
// 金瓜子 1000 = 1 元，即用户定的"大于 1000 电池"
const GIFT_WINDOW_PRICE = 1000;

export type WindowItem = DanmakuEvent | GiftEvent | SuperChatEvent;

export type AgentReply = { text: string; replyTo: WindowItem[] };

export type WindowSource = 'start' | 'playback' | 'danmaku';

declare module '@deepseek-ai/cordis' {
  interface Events {
    'agent/reply'(reply: AgentReply): void;
    'llm/window'(window: { source: WindowSource }): void;
  }
}

export type LlmPluginConfig = { topics?: string[] };

export const apply = Object.assign(
  function llm(ctx: Context, config: LlmPluginConfig) {
  const logger = ctx.logger('llm');
  const gate = new SpeechGate();
  const buffer: WindowItem[] = [];
  const readItems = new Set<WindowItem>();
  const pendingSuperChats: SuperChatEvent[] = [];
  const recentSpeech: string[] = [];
  // 话题池：启动时洗牌，用掉即弹，避免每次开播都讲同一批话题。
  // 话题来源：LLM_TOPICS 环境变量（逗号分隔）；config.topics 优先（测试注入用）
  const envTopics = (process.env.LLM_TOPICS ?? '')
    .split(',')
    .map((topic) => topic.trim())
    .filter(Boolean);
  const topicPool = [...(config.topics?.length ? config.topics : envTopics)];
  for (let i = topicPool.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [topicPool[i], topicPool[j]] = [topicPool[j], topicPool[i]];
  }
  const takeTopic = () => topicPool.shift();
  let speaking = false;
  let windowRunning = false;
  let queued: WindowSource | undefined;
  let started = false;
  let emptyStreak = 0;
  let topicSentences = 0;
  let unspoken = 0;
  let heardPlaybackCompleted = false;
  const quietWaiters: (() => void)[] = [];

  ctx.on('playback/completed', () => {
    heardPlaybackCompleted = true;
    unspoken = Math.max(0, unspoken - 1);
    if (unspoken === 0) for (const resolve of quietWaiters.splice(0)) resolve();
  });
  // 没有播报管线回流事件时（如单测）不等待，避免死等
  const waitQuiet = () =>
    !heardPlaybackCompleted || unspoken === 0
      ? Promise.resolve()
      : new Promise<void>((resolve) => quietWaiters.push(resolve));

  const isIdle = () => !speaking && !windowRunning && queued === undefined;
  const readDanmaku = (): WindowItem[] => {
    const superChats = pendingSuperChats.splice(0);
    const fresh = buffer.filter((item) => !readItems.has(item));
    const recent = fresh.slice(Math.max(0, fresh.length - Math.max(0, READ_LIMIT - superChats.length)));
    for (const item of recent) readItems.add(item);
    return [...superChats, ...recent];
  };

  const respond = (source: WindowSource) => {
    started = true;
    if (windowRunning) {
      queued = source;
      return;
    }
    windowRunning = true;
    ctx.emit('llm/window', { source });
    // 每个窗口现取服务：游戏/记忆是可选服务，可能在插件之后才挂上
    const game = ctx.get('game') as GameHooks | undefined;
    const memory = ctx.get('memory') as MemoryHooks | undefined;
    const changeTopic = topicSentences >= TOPIC_SWITCH_EVERY && !game?.isActive();
    const topic = source === 'start' || changeTopic ? takeTopic() : undefined;
    if (topic) console.log(`[话题] ${topic}`);
    let spoke = false;
    const say = (text: string) => {
      if (!gate.check({ text }, recentSpeech).accepted) return;
      spoke = true;
      emptyStreak = 0;
      topicSentences = changeTopic ? 0 : topicSentences + 1;
      recentSpeech.push(text);
      recentSpeech.splice(0, Math.max(0, recentSpeech.length - RECENT_SPEECH_SIZE));
      unspoken += 1;
      ctx.emit('agent/reply', { text, replyTo: [] });
    };
    replyToWindow(ctx.llm, readDanmaku, recentSpeech, {
      changeTopic,
      topic,
      opening: source === 'start',
      game,
      memory,
      say,
      waitQuiet,
    })
      .catch((error) => logger.error(error))
      .finally(() => {
        windowRunning = false;
        if (!spoke) emptyStreak += 1;
        const next = queued;
        queued = undefined;
        if (next) {
          respond(next);
          return;
        }
        if (source !== 'danmaku' && emptyStreak > 0 && emptyStreak <= MAX_EMPTY_WINDOWS) respond(source);
      });
  };

  ctx.on('world/live-event', (event: LiveEvent) => {
    if (event.type === 'heartbeat') {
      // 首个心跳开场；链条因连续空窗口停摆后，由下一个心跳重新拉起，安静房间不至于永远沉默
      if (isIdle() && (!started || emptyStreak > MAX_EMPTY_WINDOWS)) respond('start');
      return;
    }
    if (event.type === 'danmaku' || (event.type === 'gift' && event.coinType === 'gold' && event.price >= GIFT_WINDOW_PRICE)) {
      buffer.push(event);
      for (const dropped of buffer.splice(0, Math.max(0, buffer.length - BUFFER_SIZE))) readItems.delete(dropped);
      if (isIdle()) respond('danmaku');
      return;
    }
    if (event.type === 'super-chat') {
      pendingSuperChats.push(event);
      pendingSuperChats.splice(0, Math.max(0, pendingSuperChats.length - SUPER_CHAT_QUEUE_SIZE));
      if (isIdle()) respond('danmaku');
    }
  });

  ctx.on('playback/started', ({ waiting }) => {
    speaking = true;
    if (waiting === 0) respond('playback');
  });
  ctx.on('playback/completed', () => {
    speaking = false;
  });
  }, { inject: ['llm'] },
);

export const config = {};
