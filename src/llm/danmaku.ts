import type { DanmakuEvent, GuardEvent, SuperChatEvent } from '../../plugins/90-danmaku/normalize.ts';
import type { GiftEvent } from '../../plugins/90-danmaku/gift-v2.ts';
import { bigrams, dice, overlap } from '../text/similarity.ts';

/** 进裁决窗口的一批事件（plugin buffer 的条目）。放这里是为了让 plugin 与分层共享同一个类型且不成环 */
export type DanmakuItem = DanmakuEvent | GiftEvent | SuperChatEvent | GuardEvent;

/** 逐条呈现的弹幕条数上限。与旧的 read_danmaku「最新 10 条」保持一致 */
export const DANMAKU_LIST_LIMIT = 10;
/** 与她可见发言的相似度门槛（bigram + Dice） */
export const RELEVANCE_THRESHOLD = 0.25;
/** 短文本补丁：光靠一个二字组就把 Dice 顶过阈值的，不算相关 */
const MIN_OVERLAP = 2;

const QUESTION_ENDINGS = ['？', '?', '吗', '呢', '么', '嘛'];

/** 以疑问结尾（先去掉尾部句末标点）。这是"有人在提问"的最廉价代理，不需要分词也不依赖昵称表 */
export function isQuestion(text: string): boolean {
  const trimmed = text.trim().replace(/[。.！!~～…]+$/u, '').trim();
  // 纯标点/纯表情（「？？」「😂」）不算提问，否则刷屏会被当成有人发问
  if (trimmed === '' || !/[\p{L}\p{N}]/u.test(trimmed)) return false;
  return QUESTION_ENDINGS.some((ending) => trimmed.endsWith(ending));
}

/** 纯表情/纯标点：去掉空白后 ≤2 字符且不含任何字母或数字（汉字算字母） */
export function isNoise(text: string): boolean {
  const stripped = text.replace(/\s+/gu, '');
  return stripped.length <= 2 && !/[\p{L}\p{N}]/u.test(stripped);
}

/** 与她可见发言（spoken）里的任一句相关 */
function relevantTo(text: string, spoken: string[]): boolean {
  const grams = bigrams(text);
  if (grams.size === 0) return false;
  for (const sentence of spoken) {
    const other = bigrams(sentence);
    if (other.size === 0) continue;
    if (overlap(grams, other) < MIN_OVERLAP) continue;
    if (dice(grams, other) >= RELEVANCE_THRESHOLD) return true;
  }
  return false;
}

export type WindowClassification = {
  /** 逐条呈现的条目：先非弹幕（SC/礼物/上舰），再按时间排的弹幕 */
  listed: DanmakuItem[];
  /** 被聚合掉的条数（气氛、复读、纯表情、超上限） */
  hidden: number;
};

/**
 * 分层：**只判断相关，不判断该不该接**（接不接是模型的语义裁决）。
 * - 非弹幕（SC / 礼物 / 上舰）恒逐条：它们是事件，被聚合成"另有 N 条闲聊"就荒唐了
 * - 弹幕命中任一条即逐条：
 *   ① 她上一句是问句（她刚提问，紧接着来的很可能是在回答）
 *   ② 这条自己以疑问结尾（有人在问）
 *   ③ 与她可见发言的 bigram+Dice 过阈值且重合 ≥2
 * - 其余（含同 uid 同文本复读、纯表情/纯标点）→ 只计数，不进上下文
 */
export function classifyWindowItems(items: DanmakuItem[], spoken: string[]): WindowClassification {
  const answerable = spoken.length > 0 && isQuestion(spoken.at(-1) ?? '');
  const events: DanmakuItem[] = [];
  const relevant: DanmakuItem[] = [];
  const seen = new Set<string>();
  let hidden = 0;

  for (const item of items) {
    if (item.type !== 'danmaku') {
      events.push(item);
      continue;
    }
    const key = `${item.uid}:${item.text}`;
    if (seen.has(key) || isNoise(item.text)) {
      hidden += 1;
      continue;
    }
    seen.add(key);
    if (answerable || isQuestion(item.text) || relevantTo(item.text, spoken)) relevant.push(item);
    else hidden += 1;
  }

  // 只呈现最新的一批，与旧 read_danmaku 的口径一致
  const capped = relevant.slice(-DANMAKU_LIST_LIMIT);
  hidden += relevant.length - capped.length;
  return { listed: [...events, ...capped], hidden };
}
