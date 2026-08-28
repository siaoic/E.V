// 旋律/乐谱解析：把"音符数组"或"紧凑字符串"转成带绝对时间的事件
// 紧凑字符串示例：
//   "C4 E4 G4 C5"                      -> 每拍一个音，默认 1 拍时长
//   "C4:2 E4:1 G4:0.5"                 -> 用 :n 指定该音符持续拍数
//   "C4@0.8 E4@0.5"                    -> 用 @v 指定力度(0~1)
//   "R"                                -> 休止符（占一拍但不发声）
//   "C4 E4 G4 | A4 G4"                 -> "|" 只是可选的小节分隔符（忽略）

import { noteToMidi, normalizeNote } from './notes.js';

/** 解析紧凑旋律字符串 -> [{midi, beat, beats, velocity}] */
export function parseMelodyString(str) {
  const tokens = str.trim().split(/[\s|]+/).filter(Boolean);
  const notes = [];
  let beat = 0;
  for (const tok of tokens) {
    const m = tok.match(/^([A-Ga-g][#b]?-?\d+|R|r)(?::([\d.]+))?(?:@([\d.]+))?$/);
    if (!m) {
      return { error: `无法识别的音符 "${tok}"，示例：C4、C#4、Db4、R（休止）、C4:2（两拍）、C4@0.8（力度）` };
    }
    const [, name, durStr, velStr] = m;
    const duration = durStr ? parseFloat(durStr) : 1;
    if (!Number.isFinite(duration) || duration <= 0) {
      return { error: `音符 "${tok}" 的时长必须 > 0` };
    }
    const velocity = velStr !== undefined ? Math.min(1, Math.max(0.05, parseFloat(velStr))) : undefined;

    if (name.toUpperCase() === 'R') {
      beat += duration; // 休止符只占时间
      continue;
    }

    const midi = noteToMidi(name);
    if (midi === null) return { error: `无法解析音符 "${name}"` };
    notes.push({ midi, beat, beats: duration, velocity });
    beat += duration;
  }
  return { notes };
}

/**
 * 把"音符列表（拍为单位）"转为绝对秒事件
 * @param {Array<{note?:string|number, midi?:number, beat:number, duration:number, velocity?:number, hand?:string}>} notes
 * @param {number} bpm
 * @param {object} [opts]
 * @param {boolean} [opts.mergeShort=true] 合并同音高、连续 16 分音符级别的短音符，
 *   因为 OMR（homr）经常把一个二分音符拆成 4×0.25 拍，合并后节奏更自然。
 * @param {boolean} [opts.dynamicVelocity=true] 按节拍位置自动加力度（小节首拍重、
 *   次拍中、16 分弱），让听感有强弱起伏而不是机械平推。
 * @param {number} [opts.beatsPerBar=4] 每小节拍数（用于判断小节首拍位置）
 */
export function scoreToEvents(notes, bpm = 100, opts = {}) {
  const {
    mergeShort = true,
    dynamicVelocity = true,
    beatsPerBar = 4,
  } = opts;
  const spb = 60 / bpm; // 每拍秒数

  // ---- 阶段 1：归一化为统一字段 ----
  const raw = [];
  for (const n of notes) {
    const beat = n.beat ?? 0;
    const beats = n.duration ?? 1;
    const velocity = n.velocity;
    const midi = n.midi !== undefined ? n.midi : noteToMidi(n.note);
    if (midi === null || !Number.isFinite(midi)) continue;
    raw.push({
      midi,
      beat,
      beats,
      velocity,
      hand: n.hand,
    });
  }
  // 按 beat 排序（同 beat 内保持原序）
  raw.sort((a, b) => a.beat - b.beat);

  // ---- 阶段 2：合并同音高、连续 16 分级别的短音符 ----
  // 策略：相邻两个音符若 midi 相同、后一个 beat == 前一个 (beat+beats)、
  // 且每个 dur <= mergeThresholdBeat，则合并为一个，dur 相加。
  const merged = [];
  const mergeThresholdBeat = 0.5; // <= 8 分音符的短音符才考虑合并
  for (const n of raw) {
    const last = merged[merged.length - 1];
    if (
      mergeShort &&
      last &&
      last.midi === n.midi &&
      last.hand === n.hand &&
      last.beats <= mergeThresholdBeat &&
      n.beats <= mergeThresholdBeat &&
      Math.abs(n.beat - (last.beat + last.beats)) < 1e-6
    ) {
      last.beats += n.beats;
      // 取较大力度（如果用户没显式给 velocity，下面的动态逻辑会接管）
      if (n.velocity !== undefined) {
        last.velocity = last.velocity !== undefined
          ? Math.max(last.velocity, n.velocity)
          : n.velocity;
      }
    } else {
      merged.push({ ...n });
    }
  }

  // ---- 阶段 3：按节拍位置施加动态力度 ----
  // 规则（per beat position）：
  //   beat % 1 == 0          -> 1.00（小节首拍重音）
  //   beat % 1 == 0.5        -> 0.80（次拍中音）
  //   beat % 1 == 0.25/0.75  -> 0.65（16 分弱音）
  //   其它                   -> 0.75
  // 同时考虑每小节第一拍（beat % beatsPerBar == 0）：额外 +0.05
  function dynamicVel(beat) {
    const inBeat = beat - Math.floor(beat);
    let v;
    if (inBeat < 0.05) v = 1.0;
    else if (Math.abs(inBeat - 0.5) < 0.05) v = 0.8;
    else if (Math.abs(inBeat - 0.25) < 0.05 || Math.abs(inBeat - 0.75) < 0.05) v = 0.65;
    else v = 0.75;
    if (beat % beatsPerBar < 1e-6) v = Math.min(1, v + 0.05);
    return v;
  }

  // ---- 阶段 4：转绝对秒事件 ----
  const events = [];
  for (const n of merged) {
    const baseVel = n.velocity !== undefined
      ? Math.min(1, Math.max(0.05, n.velocity))
      : (dynamicVelocity ? dynamicVel(n.beat) : 1);
    events.push({
      midi: n.midi,
      time: n.beat * spb,
      duration: Math.max(0.05, n.beats * spb),
      velocity: baseVel,
      hand: n.hand,
    });
  }
  return events;
}

export { normalizeNote };
