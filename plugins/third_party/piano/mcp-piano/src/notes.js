// 音符工具：科学音高记谱名 <-> MIDI 编号 互转、校验
// 约定：C4 为中央 C（MIDI 60），支持升降号（# / b），如 C#4、Db4、Bb2、A0。

const SEMITONES = {
  C: 0, 'C#': 1, Db: 1, D: 2, 'D#': 3, Eb: 3, E: 4,
  F: 5, 'F#': 6, Gb: 6, G: 7, 'G#': 8, Ab: 8,
  A: 9, 'A#': 10, Bb: 10, B: 11,
};

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

/**
 * 解析音符为 MIDI 编号。
 * @param {string|number} note - "C4" / "c#4" / "Db-1" / 60（MIDI 编号）
 * @returns {number|null} MIDI 编号，解析失败返回 null
 */
export function noteToMidi(note) {
  if (typeof note === 'number') {
    return Number.isInteger(note) && note >= 0 && note <= 127 ? note : null;
  }
  if (typeof note !== 'string') return null;
  const s = note.trim();
  if (/^\d+$/.test(s)) {
    const n = parseInt(s, 10);
    return n >= 0 && n <= 127 ? n : null;
  }
  const m = s.match(/^([A-Ga-g])([#b]?)(-?\d+)$/);
  if (!m) return null;
  const [, letter, acc, octStr] = m;
  const key = letter.toUpperCase() + acc;
  if (SEMITONES[key] === undefined) return null;
  const octave = parseInt(octStr, 10);
  return (octave + 1) * 12 + SEMITONES[key];
}

/** MIDI 编号 -> 规范音符名（如 60 -> "C4"） */
export function midiToNote(midi) {
  if (!Number.isInteger(midi) || midi < 0 || midi > 127) return null;
  return NOTE_NAMES[midi % 12] + (Math.floor(midi / 12) - 1);
}

/** 规范化用户输入的音符：任意合法写法 -> 规范名（"Db4" -> "C#4"）。非法返回 null */
export function normalizeNote(note) {
  const midi = noteToMidi(note);
  return midi === null ? null : midiToNote(midi);
}

export const MIN_MIDI = 21; // A0
export const MAX_MIDI = 108; // C8

/**
 * 判断一个 MIDI 值是否落在标准 88 键钢琴范围（A0=21 ~ C8=108）内，
 * 且为整数。用于入口校验：超范围的音直接拒，禁止用合成替代。
 */
export function isValidPianoMidi(m) {
  return Number.isInteger(m) && m >= MIN_MIDI && m <= MAX_MIDI;
}

/**
 * 把任意 note 表达（音名 / MIDI 数字 / MIDI 字符串）转为钢琴范围内的 MIDI。
 * 超范围 / 非法时返回 fallback（默认 null），便于入口做 fail()。
 */
export function enforcePianoRange(note, fallback = null) {
  const m = noteToMidi(note);
  return isValidPianoMidi(m) ? m : fallback;
}
