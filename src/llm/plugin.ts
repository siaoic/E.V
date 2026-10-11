import type { Context } from '@deepseek-ai/cordis';
import type { LiveEvent } from '../../plugins/90-danmaku/plugin.ts';
import type { SuperChatEvent } from '../../plugins/90-danmaku/normalize.ts';
import { SpeechGate } from './speech-gate.ts';
import { isRateLimited, type LlmPort } from './client.ts';
import { replyToWindow, type GameHooks } from './reply.ts';
import type { DanmakuItem } from './danmaku.ts';
import { bigrams, overlap } from '../text/similarity.ts';

const BUFFER_SIZE = 20;
const RECENT_SPEECH_SIZE = 20;
const SUPER_CHAT_QUEUE_SIZE = 10;
// 一个窗口正常只花几秒（一次模型调用 + 至多几轮工具）；超过这个时长说明链条被拖住了
const WINDOW_SLOW_MS = 20_000;
// 端点限流（429）后的降温拍数：被顶回来后按心跳数冷静几拍再试。用心跳计数而不是计时器（零计时器架构），
// 心跳也正是原本的停摆恢复信号。没有这层降温时，限流窗口一结束就被下一个心跳重新拉起（心跳每 2 秒一拍），
// 每个窗口还要烧 4 次尝试 —— 等于自己把配额焊死，直播长时间没声音。
const RATE_LIMIT_COOLDOWN_BEATS = 10;
// 停摆恢复节拍：链条停下且空闲时，每这么多拍心跳给一次机会（实测心跳约 2 秒一拍 ⇒ 约 10 秒一次）。
// 不能写成"下一个心跳就拉起"——那就是每 2 秒开一个窗口；沉默是合法态，恢复只需要有节奏。
const HEARTBEAT_REVIVE_BEATS = 5;
// 礼物门槛：**金瓜子总价**（单价 × 数量）达到这个数才进缓冲、空闲时开窗。
// 金瓜子 100000 = 100 元（1 元 = 1000 金瓜子）——用户决定「只理大礼」：1–99 元的礼物她看不见。
// （旧注释写的"大于 1000 电池"换算过来正是 100000 金瓜子，已按此对齐，不再有两套单位。）
const GIFT_WINDOW_PRICE = 100_000;
// 礼物总价：数量缺失或为 0 时按一件算
const giftTotal = (gift: { price: number; num: number }): number => gift.price * Math.max(1, gift.num);
// 「接住观众的话」的判定：她的某句话与被列出的某条弹幕重合 ≥2 个二字组（同一套 bigram 口径）
const MIN_ENGAGE_OVERLAP = 2;

export type WindowItem = DanmakuItem;

export type AgentReply = { text: string; replyTo: WindowItem[] };

export type WindowSource = 'start' | 'playback' | 'danmaku' | 'revive';

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
  // 「已呈现」标记：条目进了某个窗口的上下文就不再重复进（聚合掉的也算，它已被那行计数代表）
  const presented = new Set<WindowItem>();
  const pendingSuperChats: SuperChatEvent[] = [];
  const recentSpeech: string[] = [];
  // 话题池：启动时洗牌，用掉即弹。只有开场窗口（source === 'start'）会取
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
  let unspoken = 0;
  let heardPlaybackCompleted = false;
  // 限流降温倒计时（单位：心跳拍数）
  let rateLimitCooldown = 0;
  // 停摆恢复的节拍计数（单位：心跳拍数）
  let reviveBeats = 0;
  // 机械唤起：本场已经唤起过的观众。只对「首次出现」注入档案首行，避免每个窗口都重复喂同一份档案
  const wokenViewers = new Set<number>();
  const quietWaiters: (() => void)[] = [];
  // 节奏水位：只作数据注入，不参与开窗；一律事件单位，不读时钟（ADR-0008）
  let selfTalk = 0; // 自上次接住观众的话以来，她讲了几句
  let danmakuSinceEngage = 0; // 同期经过的弹幕条数
  let playbackWaiting = 0; // 正在播之外还排着几句
  let lastHeat: number | null = null;

  // 她这一窗的话有没有接住某条被列出的弹幕（重合 ≥2 个二字组）
  const engagedWith = (said: string[], items: WindowItem[]): boolean => {
    const lines = items.filter((item) => item.type === 'danmaku');
    return said.some((sentence) => {
      const grams = bigrams(sentence);
      return lines.some((line) => line.type === 'danmaku' && overlap(grams, bigrams(line.text)) >= MIN_ENGAGE_OVERLAP);
    });
  };

  const statusLine = (): string => {
    const parts: string[] = [];
    if (selfTalk > 0) {
      parts.push(
        danmakuSinceEngage > 0
          ? `你已连着自聊 ${selfTalk} 句，期间 ${danmakuSinceEngage} 条弹幕经过`
          : `你已连着自聊 ${selfTalk} 句`,
      );
    }
    if (emptyStreak > 0) parts.push(`连续 ${emptyStreak} 个窗口没有产出`);
    parts.push(playbackWaiting > 0 ? `待播队列还有 ${playbackWaiting} 句` : '待播队列空');
    if (lastHeat !== null) parts.push(`热度 ${lastHeat}`);
    return `（直播状态：${parts.join('；')}）`;
  };

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
  // 本窗要呈现的条目：SC 优先（送达保证），其余按到达顺序。
  // 这里只"取"不记账：窗口失败（限流/超时）时不能把它们算成已呈现 —— 否则一批弹幕会在没被任何模型看到的情况下消失。
  let takenThisWindow: WindowItem[] = [];
  const takeItems = (): WindowItem[] => {
    const fresh = buffer.filter((item) => !presented.has(item));
    takenThisWindow = [...pendingSuperChats, ...fresh];
    return takenThisWindow;
  };
  // 窗口真的走完（没出错）才把这一批记为已呈现；SC 同时从待送队列里摘掉
  const commitTaken = () => {
    for (const item of takenThisWindow) presented.add(item);
    for (const item of takenThisWindow) {
      if (item.type !== 'super-chat') continue;
      const index = pendingSuperChats.indexOf(item);
      if (index >= 0) pendingSuperChats.splice(index, 1);
    }
    takenThisWindow = [];
  };

  const respond = (source: WindowSource) => {
    started = true;
    if (windowRunning) {
      queued = source;
      return;
    }
    windowRunning = true;
    const windowStartedAt = Date.now();
    ctx.emit('llm/window', { source });
    // 每个窗口现取服务：游戏/记忆是可选服务，可能在插件之后才挂上
    const game = ctx.get('game') as GameHooks | undefined;
    const memory = ctx.get('memory');
    // 机械唤起：本窗未呈现条目里「本场首次出现」的观众 uid。注入失败（如限流）不在 finally 里记账，下次还能唤起
    const freshViewerUids = [...new Set(buffer.filter((item) => !presented.has(item)).map((item) => item.uid))].filter(
      (uid) => Number.isInteger(uid) && uid > 0 && !wokenViewers.has(uid),
    );
    // 话题池只在开场用一次；中段不再由机制指定话题（按句数强制换话题已删除，见 ADR-0009）
    const topic = source === 'start' ? takeTopic() : undefined;
    if (topic) console.log(`[话题] ${topic}`);
    let spoke = false;
    let failure: unknown;
    const said: string[] = [];
    const say = (text: string) => {
      if (!gate.check({ text }, recentSpeech).accepted) return;
      spoke = true;
      emptyStreak = 0;
      reviveBeats = 0;
      said.push(text);
      recentSpeech.push(text);
      recentSpeech.splice(0, Math.max(0, recentSpeech.length - RECENT_SPEECH_SIZE));
      unspoken += 1;
      ctx.emit('agent/reply', { text, replyTo: [] });
    };
    replyToWindow(ctx.llm, takeItems, recentSpeech, {
      topic,
      opening: source === 'start',
      game,
      memory,
      viewerUids: freshViewerUids,
      statusContext: statusLine(),
      say,
      waitQuiet,
    })
      .then((reply) => {
        // 接了观众的话就清零；一直在自聊就把句数攒着（这些数字只进上下文，不参与开窗）
        if (said.length === 0) return;
        if (engagedWith(said, reply.replyTo)) {
          selfTalk = 0;
          danmakuSinceEngage = 0;
          return;
        }
        selfTalk += said.length;
      })
      .catch((error) => {
        failure = error;
        if (isRateLimited(error)) {
          rateLimitCooldown = RATE_LIMIT_COOLDOWN_BEATS;
          console.warn(`[窗口] 端点限流（429），暂停 ${RATE_LIMIT_COOLDOWN_BEATS} 个心跳再恢复`);
        }
        logger.error(error);
      })
      .finally(() => {
        windowRunning = false;
        // 窗口没出错才算真的认过这批人、真的呈现过这批条目；限流/超时失败的那次下次原样重来
        if (!failure) {
          for (const uid of freshViewerUids) wokenViewers.add(uid);
          commitTaken();
        }
        const elapsed = Date.now() - windowStartedAt;
        if (!spoke) emptyStreak += 1;
        // 静默可见化：空窗口、超长窗口是「长时间不说话」的两个前兆，控制台上要看得见，别让停摆无声发生
        if (!spoke) {
          const why = failure
            ? `，失败：${(failure instanceof Error ? failure.message : String(failure)).slice(0, 120)}`
            : '，模型没说话';
          console.warn(
            `[窗口] ${source} 空窗口（连续第 ${emptyStreak} 次，耗时 ${(elapsed / 1000).toFixed(1)}s${why}）——链条停下，等新弹幕或心跳节拍`,
          );
        } else if (elapsed > WINDOW_SLOW_MS) {
          console.warn(`[窗口] ${source} 耗时 ${(elapsed / 1000).toFixed(1)}s，链条被拖慢`);
        }
        const next = queued;
        queued = undefined;
        // 降温期不再开窗（含排队的那个）：等心跳把配额让出来
        if (rateLimitCooldown > 0) return;
        // 只有"合并进来的排队窗口"会立刻继续；沉默与失败都不再立即重试——
        // 沉默是合法裁决（ADR-0002 / 种子「沉默的合法性」），失败在 client 内部已经重试过，
        // 恢复交给心跳节拍（HEARTBEAT_REVIVE_BEATS）。
        if (next) respond(next);
      });
  };

  ctx.on('world/live-event', (event: LiveEvent) => {
    if (event.type === 'heartbeat') {
      if (typeof event.count === 'number') lastHeat = event.count;
      // 限流降温期：只数拍不开窗，把配额让出来
      if (rateLimitCooldown > 0) {
        rateLimitCooldown -= 1;
        if (rateLimitCooldown > 0) return;
        console.warn('[窗口] 限流降温结束，重新给一次机会');
        if (isIdle()) respond('revive');
        return;
      }
      // 首个心跳开场（话题池的那一条只在这里取）
      if (!started) {
        if (isIdle()) respond('start');
        return;
      }
      // 停摆恢复：链条已经停下（空闲）时，每 HEARTBEAT_REVIVE_BEATS 拍给一次机会。
      // 用 'revive' 而不是 'start'：中段恢复不是开场，不该吃话题池、也不该说"开始直播了"。
      if (!isIdle()) {
        reviveBeats = 0;
        return;
      }
      reviveBeats += 1;
      if (reviveBeats < HEARTBEAT_REVIVE_BEATS) return;
      reviveBeats = 0;
      console.warn(`[窗口] 链条停摆 ${HEARTBEAT_REVIVE_BEATS} 拍，重新给一次机会`);
      respond('revive');
      return;
    }
    if (
      event.type === 'danmaku' ||
      // 上舰是低频高价值的付费行为：进缓冲，空闲时开窗（与 ≥门槛礼物同档）
      event.type === 'guard' ||
      (event.type === 'gift' && event.coinType === 'gold' && giftTotal(event) >= GIFT_WINDOW_PRICE)
    ) {
      buffer.push(event);
      if (event.type === 'danmaku') danmakuSinceEngage += 1;
      for (const dropped of buffer.splice(0, Math.max(0, buffer.length - BUFFER_SIZE))) presented.delete(dropped);
      // 降温期弹幕照旧进缓冲（恢复后一次取走），但不开窗
      if (rateLimitCooldown > 0) return;
      if (isIdle()) respond('danmaku');
      return;
    }
    if (event.type === 'super-chat') {
      pendingSuperChats.push(event);
      pendingSuperChats.splice(0, Math.max(0, pendingSuperChats.length - SUPER_CHAT_QUEUE_SIZE));
      if (rateLimitCooldown > 0) return;
      if (isIdle()) respond('danmaku');
    }
  });

  ctx.on('playback/started', ({ waiting }) => {
    speaking = true;
    playbackWaiting = waiting;
    if (waiting === 0) respond('playback');
  });
  ctx.on('playback/completed', () => {
    speaking = false;
    playbackWaiting = Math.max(0, playbackWaiting - 1);
  });
  }, { inject: ['llm'] },
);

export const config = {};
