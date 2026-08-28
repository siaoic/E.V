// 乐理小助手核心计算：和弦分析、音阶/调式、调号、音程、级数进行、旋律配和弦
// 全部纯本地计算，结果附带 MIDI 编号与音名，可直接交给 play_chord / play_melody 试听。

import { noteToMidi, midiToNote } from './notes.js';

const PITCH = {
  C: 0, 'C#': 1, Db: 1, D: 2, 'D#': 3, Eb: 3, E: 4, F: 5, 'F#': 6, Gb: 6,
  G: 7, 'G#': 8, Ab: 8, A: 9, 'A#': 10, Bb: 10, B: 11,
};

// 音级名（不带八度）解析，非法返回 null
export function parsePitchClass(name) {
  const m = String(name).trim().match(/^([A-Ga-g])([#b♯♭]?)(\d*)$/);
  if (!m) return null;
  const acc = m[2] === '♯' ? '#' : m[2] === '♭' ? 'b' : m[2];
  const key = m[1].toUpperCase() + acc;
  if (PITCH[key] === undefined) return null;
  return PITCH[key];
}

// ---------- 和弦 ----------

const CHORD_SHAPES = {
  maj: [0, 4, 7], m: [0, 3, 7], dim: [0, 3, 6], aug: [0, 4, 8],
  '5': [0, 7], sus2: [0, 2, 7], sus4: [0, 5, 7],
  '6': [0, 4, 7, 9], m6: [0, 3, 7, 9],
  '7': [0, 4, 7, 10], maj7: [0, 4, 7, 11], m7: [0, 3, 7, 10],
  m7b5: [0, 3, 6, 10], dim7: [0, 3, 6, 9], mMaj7: [0, 3, 7, 11],
  '9': [0, 4, 7, 10, 14], maj9: [0, 4, 7, 11, 14], m9: [0, 3, 7, 10, 14],
  add9: [0, 4, 7, 14], '11': [0, 4, 7, 10, 14, 17], '13': [0, 4, 7, 10, 21],
};

const CHORD_ALIASES = {
  '': 'maj', M: 'maj', maj: 'maj', major: 'maj',
  min: 'm', minor: 'm', '-': 'm',
  min7: 'm7', '-7': 'm7', min9: 'm9',
  M7: 'maj7', MAJ7: 'maj7', 'Δ': 'maj7', '△': 'maj7', major7: 'maj7',
  minMaj7: 'mMaj7', 'm(maj7)': 'mMaj7',
  'ø': 'm7b5', 'ø7': 'm7b5',
  '°': 'dim', 'º': 'dim', o: 'dim', 'o7': 'dim7', '°7': 'dim7',
  '+': 'aug', aug7: '7#5',
};

const CHORD_CN = {
  maj: '大三和弦', m: '小三和弦', dim: '减三和弦', aug: '增三和弦',
  '5': '强力和弦（纯五度）', sus2: '挂二和弦', sus4: '挂四和弦',
  '6': '大六和弦', m6: '小六和弦',
  '7': '属七和弦', maj7: '大七和弦', m7: '小七和弦',
  m7b5: '半减七和弦', dim7: '减七和弦', mMaj7: '小大七和弦',
  '9': '属九和弦', maj9: '大九和弦', m9: '小九和弦',
  add9: '加九和弦', '11': '属十一和弦', '13': '属十三和弦',
};

// 相对根音的半音差 -> 中文音级名
const DEGREE_CN = {
  0: '根音', 1: '小二度', 2: '大二度', 3: '小三度', 4: '大三度', 5: '纯四度',
  6: '三全音', 7: '纯五度', 8: '增五度', 9: '大六度', 10: '小七度',
  11: '大七度', 14: '大九度', 17: '纯十一度', 21: '大十三度',
};

/** 解析和弦符号（如 "Cmaj7"、"Am"、"F#m7b5"、"Ebsus4"），返回结构化信息 */
export function analyzeChord(symbol) {
  const s = String(symbol).trim();
  const m = s.match(/^([A-Ga-g][#b♯♭]?)(.*)$/);
  if (!m) return { error: `无法解析和弦 "${symbol}"` };
  const rootPitch = parsePitchClass(m[1]);
  if (rootPitch === null) return { error: `无法识别根音 "${m[1]}"` };

  let suffix = m[2].replace(/[\s()]/g, '');
  if (CHORD_ALIASES[suffix] !== undefined) suffix = CHORD_ALIASES[suffix];
  const shape = CHORD_SHAPES[suffix];
  if (!shape) {
    return {
      error: `不认识的和弦类型 "${m[2]}"`,
      支持: Object.keys(CHORD_SHAPES).join(' / '),
    };
  }

  const rootMidi = 48 + rootPitch; // 从 C3(48) 起排布，落在钢琴常规音域
  const midi = shape.map((x) => rootMidi + x);
  const notes = midi.map(midiToNote);
  const degrees = shape.map((x) => DEGREE_CN[x] || `+${x} 半音`);
  const intervals = shape.map((x) => `${x}`);

  return {
    和弦: s,
    根音: m[1],
    类型: suffix,
    中文名: CHORD_CN[suffix] || suffix,
    组成音: notes,
    midi,
    音级: degrees,
    相对根音半音: intervals,
    提示: '可用 play_chord(notes 或 midi) 直接试听',
  };
}

// ---------- 音阶 / 调式 ----------

const SCALES = {
  major: { shape: [0, 2, 4, 5, 7, 9, 11], cn: '自然大调', degrees: '1 2 3 4 5 6 7' },
  'natural minor': { shape: [0, 2, 3, 5, 7, 8, 10], cn: '自然小调', degrees: '1 2 b3 4 5 b6 b7' },
  'harmonic minor': { shape: [0, 2, 3, 5, 7, 8, 11], cn: '和声小调', degrees: '1 2 b3 4 5 b6 7' },
  'melodic minor': { shape: [0, 2, 3, 5, 7, 9, 11], cn: '旋律小调', degrees: '1 2 b3 4 5 6 7' },
  'pentatonic major': { shape: [0, 2, 4, 7, 9], cn: '大调五声', degrees: '1 2 3 5 6' },
  'pentatonic minor': { shape: [0, 3, 5, 7, 10], cn: '小调五声', degrees: '1 b3 4 5 b7' },
  blues: { shape: [0, 3, 5, 6, 7, 10], cn: '布鲁斯', degrees: '1 b3 4 b5 5 b7' },
  dorian: { shape: [0, 2, 3, 5, 7, 9, 10], cn: '多利亚（大调第2级）', degrees: '1 2 b3 4 5 6 b7' },
  phrygian: { shape: [0, 1, 3, 5, 7, 8, 10], cn: '弗里几亚（大调第3级）', degrees: '1 b2 b3 4 5 b6 b7' },
  lydian: { shape: [0, 2, 4, 6, 7, 9, 11], cn: '利底亚（大调第4级）', degrees: '1 2 3 #4 5 6 7' },
  mixolydian: { shape: [0, 2, 4, 5, 7, 9, 10], cn: '混合利底亚（大调第5级）', degrees: '1 2 3 4 5 6 b7' },
  locrian: { shape: [0, 1, 3, 5, 6, 8, 10], cn: '洛克里亚（大调第7级）', degrees: '1 b2 b3 4 b5 b6 b7' },
  'whole tone': { shape: [0, 2, 4, 6, 8, 10], cn: '全音阶', degrees: '1 2 3 #4 #5 #6' },
  chromatic: { shape: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], cn: '半音阶', degrees: '12 个半音' },
};

const SCALE_TYPE_ALIASES = {
  major: 'major', major_pentatonic: 'pentatonic major', 'major pentatonic': 'pentatonic major',
  penta_major: 'pentatonic major', 大调五声: 'pentatonic major',
  minor: 'natural minor', natural_minor: 'natural minor', 'natural minor': 'natural minor',
  'minor pentatonic': 'pentatonic minor', minor_pentatonic: 'pentatonic minor', penta_minor: 'pentatonic minor',
  harmonic_minor: 'harmonic minor', melodic_minor: 'melodic minor',
  blues: 'blues', 布鲁斯: 'blues',
};

/** 查询音阶/调式。key 如 "C"、"A"、"Eb"；type 如 "major"、"minor"、"blues" */
export function getScale(key, type = 'major') {
  const rootPitch = parsePitchClass(String(key).replace(/m$/i, ''));
  if (rootPitch === null) return { error: `无法识别调主音 "${key}"` };
  const t = SCALE_TYPE_ALIASES[String(type).trim().toLowerCase()] ?? String(type).trim().toLowerCase();
  const sc = SCALES[t];
  if (!sc) return { error: `不认识的音阶类型 "${type}"`, 支持: Object.keys(SCALES).join(' / ') };

  const rootMidi = 60 + rootPitch; // 从 C4 起排布一个八度
  const midi = sc.shape.map((x) => rootMidi + x);
  const notes = midi.map(midiToNote);
  return {
    调: `${key} ${t}`,
    中文名: sc.cn,
    音阶音: notes,
    midi,
    级数: sc.degrees,
    涉及调式: '如需从其它音起排可说"以 X 音开始的 XX 调式"',
    提示: '可用 play_melody 逐个弹奏，或 play_chord 试听整体色彩',
  };
}

// ---------- 调号 ----------

const KEY_SIGNATURES = {
  C: { acc: [], relMinor: 'A', cn: 'C 大调' },
  G: { acc: ['F#'], relMinor: 'E', cn: 'G 大调（1 个升号）' },
  D: { acc: ['F#', 'C#'], relMinor: 'B', cn: 'D 大调（2 个升号）' },
  A: { acc: ['F#', 'C#', 'G#'], relMinor: 'F#', cn: 'A 大调（3 个升号）' },
  E: { acc: ['F#', 'C#', 'G#', 'D#'], relMinor: 'C#', cn: 'E 大调（4 个升号）' },
  B: { acc: ['F#', 'C#', 'G#', 'D#', 'A#'], relMinor: 'G#', cn: 'B 大调（5 个升号）' },
  'F#': { acc: ['F#', 'C#', 'G#', 'D#', 'A#', 'E#'], relMinor: 'D#', cn: 'F# 大调（6 个升号）' },
  'C#': { acc: ['F#', 'C#', 'G#', 'D#', 'A#', 'E#', 'B#'], relMinor: 'A#', cn: 'C# 大调（7 个升号）' },
  F: { acc: ['Bb'], relMinor: 'D', cn: 'F 大调（1 个降号）' },
  Bb: { acc: ['Bb', 'Eb'], relMinor: 'G', cn: 'Bb 大调（2 个降号）' },
  Eb: { acc: ['Bb', 'Eb', 'Ab'], relMinor: 'C', cn: 'Eb 大调（3 个降号）' },
  Ab: { acc: ['Bb', 'Eb', 'Ab', 'Db'], relMinor: 'F', cn: 'Ab 大调（4 个降号）' },
  Db: { acc: ['Bb', 'Eb', 'Ab', 'Db', 'Gb'], relMinor: 'Bb', cn: 'Db 大调（5 个降号）' },
  Gb: { acc: ['Bb', 'Eb', 'Ab', 'Db', 'Gb', 'Cb'], relMinor: 'Eb', cn: 'Gb 大调（6 个降号）' },
  Cb: { acc: ['Bb', 'Eb', 'Ab', 'Db', 'Gb', 'Cb', 'Fb'], relMinor: 'Ab', cn: 'Cb 大调（7 个降号）' },
};

const MINOR_RELATIVE_MAJOR = { A: 'C', E: 'G', B: 'D', 'F#': 'A', 'C#': 'E', 'G#': 'B', 'D#': 'F#', 'A#': 'C#', D: 'F', G: 'Bb', C: 'Eb', F: 'Ab', Bb: 'Db', Eb: 'Gb', Ab: 'Cb' };

/** 查询调号。key 大写主音=大调（"Ab"），加 m=小调（"Am"） */
export function keySignature(key) {
  const raw = String(key).trim();
  const isMinor = /m$/i.test(raw);
  const tonic = parsePitchClass(raw.replace(/m$/i, ''));
  if (tonic === null) return { error: `无法识别调 "${key}"` };

  // 用「音名 + 升降记号」精确匹配调号表（C#/Db 视为同一调）
  const rootName = raw.replace(/m$/i, '').replace(/♯|♭/, (c) => (c === '♯' ? '#' : 'b'));
  let name = rootName[0].toUpperCase() + rootName.slice(1);
  if (isMinor) name = MINOR_RELATIVE_MAJOR[name] ?? Object.keys(KEY_SIGNATURES).find((k) => PITCH[k] === tonic);

  const sig = KEY_SIGNATURES[name];
  if (!sig) return { error: `不支持的调 "${key}"`, 支持: Object.keys(KEY_SIGNATURES).join(' / ') };

  return {
    调: isMinor ? `${name.replace(PITCH[tonic], '') || raw.replace(/m$/i, '')} 小调` : sig.cn,
    升降号: sig.acc.length === 0 ? '无' : sig.acc.join(' '),
    关系小调: sig.relMinor + ' 小调',
    关系大调: name + ' 大调',
    常用音级和弦: isMinor ? 'i ii° III iv v(或V) VI VII' : 'I ii iii IV V vi vii°',
  };
}

// ---------- 音程 ----------

const INTERVAL_CN = ['纯一度', '小二度', '大二度', '小三度', '大三度', '纯四度', '增四度/三全音', '纯五度', '小六度', '大六度', '小七度', '大七度'];
const CONSONANT = new Set([0, 3, 4, 5, 7, 8, 9]);

/** 分析两个音之间的音程 */
export function analyzeInterval(from, to) {
  const f = noteToMidi(from);
  const t = noteToMidi(to);
  if (f === null) return { error: `无法解析音符 "${from}"` };
  if (t === null) return { error: `无法解析音符 "${to}"` };

  const semis = Math.abs(t - f);
  const simple = semis % 12;
  const octaves = Math.floor(semis / 12);
  let name = INTERVAL_CN[simple];
  if (octaves > 0) name = `${INTERVAL_CN[simple] === '纯一度' ? '纯八度' : INTERVAL_CN[simple]} + ${octaves} 个八度`;

  return {
    音程: `${midiToNote(f)} → ${midiToNote(t)}`,
    半音数: semis,
    音程名: name,
    协和性: CONSONANT.has(simple) ? '协和' : '不协和（有张力）',
    提示: '可用 play_chord([from, to]) 试听',
  };
}

// ---------- 级数进行 ----------

const DIATONIC_MAJOR = [
  { deg: 'I', root: 0, triad: 'maj', seventh: 'maj7' },
  { deg: 'ii', root: 2, triad: 'm', seventh: 'm7' },
  { deg: 'iii', root: 4, triad: 'm', seventh: 'm7' },
  { deg: 'IV', root: 5, triad: 'maj', seventh: 'maj7' },
  { deg: 'V', root: 7, triad: 'maj', seventh: '7' },
  { deg: 'vi', root: 9, triad: 'm', seventh: 'm7' },
  { deg: 'vii°', root: 11, triad: 'dim', seventh: 'm7b5' },
];
const DIATONIC_MINOR = [
  { deg: 'i', root: 0, triad: 'm', seventh: 'm7' },
  { deg: 'ii°', root: 2, triad: 'dim', seventh: 'm7b5' },
  { deg: 'III', root: 3, triad: 'maj', seventh: 'maj7' },
  { deg: 'iv', root: 5, triad: 'm', seventh: 'm7' },
  { deg: 'v', root: 7, triad: 'm', seventh: 'm7' },
  { deg: 'VI', root: 8, triad: 'maj', seventh: 'maj7' },
  { deg: 'VII', root: 10, triad: 'maj', seventh: '7' },
];

const ROMAN_RE = /^(VII|VI|V|IV|III|II|I|vii|vi|v|iv|iii|ii|i)(°|o|7|maj7|m7|m7b5|dim7|dim|aug|sus4|sus2|6|m6|9|maj9|m9|add9|11|13)?$/;

function buildChordMidi(rootPitch, type, octaveBase = 48) {
  const shape = CHORD_SHAPES[type];
  if (!shape) return null;
  return shape.map((x) => octaveBase + rootPitch + x);
}

/** 级数进行 -> 具体和弦。key 如 "C"/"Am"，roman 如 "I-V-vi-IV"、"ii-V-I" */
export function chordProgression(key, roman, useSeventh = false) {
  const raw = String(key).trim();
  const isMinor = /m$/i.test(raw);
  const tonicPitch = parsePitchClass(raw.replace(/m$/i, ''));
  if (tonicPitch === null) return { error: `无法识别调 "${key}"` };

  const table = isMinor ? DIATONIC_MINOR : DIATONIC_MAJOR;
  const tokens = String(roman).split(/[-–—>\s]+/).filter(Boolean);
  if (tokens.length === 0) return { error: '级数进行为空，如 "I-V-vi-IV"' };

  const chords = [];
  for (const token of tokens) {
    const m = token.match(ROMAN_RE);
    if (!m) return { error: `无法解析级数 "${token}"（示例：I ii iii IV V vi vii°，可带后缀如 V7、ii m7）` };
    const degBase = m[1];
    const suffix = m[2] || '';
    const entry = table.find((d) => d.deg.toLowerCase().replace('°', '') === degBase.toLowerCase());
    if (!entry) return { error: `${isMinor ? '小调' : '大调'}中没有级数 "${degBase}"` };

    let type;
    if (suffix && CHORD_SHAPES[suffix] && !['7', 'maj7', 'm7', 'm7b5', 'dim'].includes(suffix)) type = suffix;
    else if (suffix === '7' || suffix === 'maj7' || suffix === 'm7' || suffix === 'm7b5') type = suffix;
    else type = useSeventh ? entry.seventh : entry.triad;
    // 用户写了显式小写罗马数字但没写后缀时尊重调内默认（如大调 ii 本来就是小三）
    if (suffix === 'dim') type = 'dim';
    if (suffix === 'o' || suffix === '°') type = entry.triad === 'dim' ? 'dim' : 'dim';

    const rootPitch = (tonicPitch + entry.root) % 12;
    const midi = buildChordMidi(rootPitch, type);
    if (!midi) return { error: `未知和弦类型 "${type}"` };
    const rootNote = midiToNote(midi[0]).replace(/\d+$/, '');
    chords.push({ 级数: entry.deg, 和弦: rootNote + (type === 'maj' ? '' : type), midi, 组成音: midi.map(midiToNote) });
  }

  return {
    调: raw,
    进行: tokens.join(' → '),
    和弦: chords.map((c) => c.和弦),
    明细: chords,
    提示: '可用 play_chord 逐个弹，或用 play_score 按拍子整体弹奏（每拍一个和弦）',
  };
}

// ---------- 旋律配和弦 ----------

/** 为旋律推荐调内和弦。melody 为音符数组，key 如 "C"/"Am" */
export function harmonize(melody, key, useSeventh = false) {
  if (!Array.isArray(melody) || melody.length === 0) return { error: 'melody 为空，传入音符数组如 ["E4","D4","C4"]' };
  const raw = String(key ?? 'C').trim();
  const isMinor = /m$/i.test(raw);
  const tonicPitch = parsePitchClass(raw.replace(/m$/i, ''));
  if (tonicPitch === null) return { error: `无法识别调 "${key}"` };

  const pitches = melody.map((n) => {
    const midi = noteToMidi(n);
    return midi === null ? null : midi % 12;
  });
  if (pitches.some((p) => p === null)) return { error: 'melody 中有无法解析的音符' };

  const table = isMinor ? DIATONIC_MINOR : DIATONIC_MAJOR;
  const type = useSeventh ? 'seventh' : 'triad';

  const scored = table.map((entry) => {
    const rootPitch = (tonicPitch + entry.root) % 12;
    const chordPcs = new Set(buildChordMidi(rootPitch, entry[type]).map((m) => m % 12));
    const hits = pitches.filter((p) => chordPcs.has(p)).length;
    const rootNote = midiToNote(48 + rootPitch).replace(/\d+$/, '');
    return {
      和弦: rootNote + (entry.triad === 'maj' ? '' : entry.triad),
      级数: entry.deg,
      匹配音数: hits,
      总音数: pitches.length,
      midi: buildChordMidi(rootPitch, entry[type]),
    };
  }).sort((a, b) => b.匹配音数 - a.匹配音数);

  return {
    旋律: melody,
    调: raw,
    推荐: scored.slice(0, 3),
    提示: '匹配音数越高越好；推荐结果可用 play_chord(midi) 试听效果',
  };
}

// ---------- 节奏 / 拍号 ----------
// 参考 dsh-pianist 的设计：
// - DEFAULT_PPQ 整数时间刻度
// - 类型守卫 helper 集中 + 路径标注错误
// - 类型优先级表（POSITION_ORDER）
// - 结果 Object.freeze 不可变

/** 专用于乐理函数的异常，带 path 标注 */
export class TheoryError extends Error {
  constructor(message, path) {
    super(path ? `${path}: ${message}` : message);
    this.name = 'TheoryError';
    this.path = path;
  }
}

/** PPQ：每个 4 分音符 = 480 ticks（参考 dsh-pianist DEFAULT_PPQ = 960，本项目选 480 兼容 MIDI 标准） */
const DEFAULT_PPQ = 480;

// 类型守卫 helper（参考 normalizer.ts 的 record/array/toBigInt/toNumber）
function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function ensureRecord(value, path) {
  if (!isRecord(value)) throw new TheoryError('必须是对象', path);
  return value;
}
function ensureArray(value, path) {
  if (!Array.isArray(value)) throw new TheoryError('必须是数组', path);
  return value;
}
function ensureFiniteNumber(value, path) {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) throw new TheoryError('必须是有限数', path);
  return n;
}
function ensureInteger(value, path) {
  if (!Number.isInteger(value)) throw new TheoryError('必须是整数', path);
  return value;
}
function ensurePositiveInteger(value, path) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new TheoryError('必须是正整数', path);
  }
  return value;
}

/**
 * 拍 → tick 整数化。
 * @param {number} beat
 * @param {{ppq?: number}} [opts]
 * @returns {number} 整数 tick
 */
export function beatToTick(beat, opts = {}) {
  const ppq = opts.ppq ?? DEFAULT_PPQ;
  const b = ensureFiniteNumber(beat, 'beat');
  return Math.round(b * ppq);
}

/**
 * tick → 拍（浮点）。
 * @param {number} tick
 * @param {{ppq?: number}} [opts]
 * @returns {number}
 */
export function tickToBeat(tick, opts = {}) {
  const ppq = opts.ppq ?? DEFAULT_PPQ;
  const t = ensureInteger(tick, 'tick');
  return t / ppq;
}

/**
 * 推断拍号（拍分子/分母）。
 * 算法（参考 dsh-pianist TempoMap + 位置加权）：
 * 1. 数据 < 16 on-beat（≈ 4 小节）→ 默认 4/4 + 低置信度（位置信息不足区分 2/4 与 4/4）
 * 2. 收集 on-beat 位置 + 对应音符 duration
 * 3. 对每个候选 X ∈ [minBar, maxBar]，按 origin=⌊onBeat[0]/X⌋·X 计算「重拍加权命中率」
 *    （重拍位置 ±tolerance 内的音符 duration 总和 / 总 duration）
 * 4. 选加权命中率最高的 X；如 X=6 且音符出现 dur≥2.5（暗示二分音符 = 3 拍）→ 输出 6/8
 *
 * @param {Array<{beat: number, duration: number}>} notes
 * @param {{minBar?: number, maxBar?: number, tolerance?: number}} [opts]
 * @returns {Readonly<{
 *   拍号: string,
 *   分子: number,
 *   分母: number,
 *   重拍间隔拍: number,
 *   重拍位置: number[],
 *   置信度: number,
 *   候选: Record<number, number>,
 *   提示: string,
 * }>}
 */
export function inferTimeSignature(notes, opts = {}) {
  const arr = ensureArray(notes, 'notes');
  if (arr.length === 0) throw new TheoryError('音符数组为空', 'notes');
  const minBar = opts.minBar ?? 2;
  const maxBar = opts.maxBar ?? 12;
  const tolerance = opts.tolerance ?? 0.15;

  // 收集所有 on-beat 位置 + 对应音符 duration
  const onBeat = [];
  const onBeatDur = [];
  for (let i = 0; i < arr.length; i += 1) {
    const beat = ensureFiniteNumber(arr[i].beat, `notes[${i}].beat`);
    const dur = ensureFiniteNumber(arr[i].duration, `notes[${i}].duration`);
    const round = Math.round(beat);
    if (Math.abs(beat - round) < tolerance) {
      onBeat.push(round);
      onBeatDur.push(dur);
    }
  }
  if (onBeat.length < 2) {
    throw new TheoryError('on-beat 音符不足 2 个，无法推断拍号', 'notes');
  }

  // 不论数据多少，都跑加权命中算法
  const totalDur = onBeatDur.reduce((a, b) => a + b, 0);
  const scores = new Map();
  for (let X = minBar; X <= maxBar; X += 1) {
    const origin = Math.floor(onBeat[0] / X) * X;
    let weightedHit = 0;
    for (let k = 0; k < onBeat.length; k += 1) {
      const p = onBeat[k];
      // 计算 p 与最近重拍位置 (origin + kX) 的偏移（环形距离）
      const offset = ((p - origin) % X + X) % X;
      const dist = Math.min(offset, X - offset);
      if (dist < tolerance) weightedHit += onBeatDur[k];
    }
    scores.set(X, weightedHit / totalDur);
  }

  // 6/8 拍特判：长音符 dur≥2.5 占比 ≥ 15%（暗示大量二分音符 = 3 拍）
  // → 强制优选 X=6
  const longNoteCount = arr.filter(
    (n) => ensureFiniteNumber(n.duration, 'notes[].duration') >= 2.5,
  ).length;
  const is68 = longNoteCount / arr.length >= 0.15;

  // 选最高分 X；若并列，优先选最常见拍号（4 > 3 > 6 > 2）
  // 注：dsh-pianist 风格的优先级表（PRIORITY_ORDER），数字越小越优先
  const PRIORITY_ORDER = [4, 3, 6, 2, 5, 7, 8, 9, 10, 11, 12];
  const priority = (x) => {
    const i = PRIORITY_ORDER.indexOf(x);
    return i === -1 ? 99 : i;
  };

  // 「位置密集度」= onBeat 数 / 数据覆盖范围。
  // 当所有整数拍位都有音符（密集度 ≈ 1.0）→ 数据是「每拍一音」均匀分布，
  // 纯位置信息不足以区分 2/4/3/4/4/4，应优先选最常见拍号（4/4）。
  const dataSpan = onBeat[onBeat.length - 1] - onBeat[0] + 1;
  const density = onBeat.length / dataSpan;
  const isUniformDense = density > 0.85;

  // 选最优 X：
  //   - 数据均匀密集：按 PRIORITY_ORDER 选（4/4 > 3/4 > 6/8 > 2/4 > ...）
  //   - 其他情况：按归一化命中率 norm = score * X（越大越优）
  let bestX = 0;
  let bestScore = 0;
  let bestNorm = -Infinity;
  for (const [X, score] of scores) {
    if (is68 && X !== 6) continue; // 6/8 模式下只看 X=6
    const norm = score * X; // 归一化命中率
    let isBetter;
    if (isUniformDense) {
      // 均匀密集：按 priority 选最小
      isBetter = bestX === 0 || priority(X) < priority(bestX);
    } else {
      // 其他：按 norm 选最大
      isBetter = norm > bestNorm + 1e-9;
    }
    if (isBetter) {
      bestX = X;
      bestScore = score;
      bestNorm = norm;
    }
  }

  // 6/8 拍特判：如 bestX=6 且音符中 dur≥2.5 出现至少 2 次 → 输出 6/8
  let 拍号 = `${bestX}/4`;
  let 分母 = 4;
  if (bestX === 6 && is68) {
    拍号 = '6/8';
    分母 = 8;
  }

  // 重拍位置
  const origin = Math.floor(onBeat[0] / bestX) * bestX;
  const downbeats = [];
  for (const p of onBeat) {
    if ((p - origin) % bestX === 0) downbeats.push(p);
  }

  // 置信度：top1 与 top2 的差距 + 基础分
  const sortedScores = [...scores.entries()]
    .filter(([X]) => !(is68 && X !== 6))
    .sort((a, b) => b[1] - a[1]);
  const top1 = sortedScores[0]?.[1] ?? 0;
  const top2 = sortedScores[1]?.[1] ?? 0;
  const margin = top1 - top2;
  let confidence = Math.round((top1 * 0.5 + margin * 0.5) * 100) / 100;
  // 数据 < 16 on-beat 时位置信息不足，置信度上限 0.6
  if (onBeat.length < 16) confidence = Math.min(confidence, 0.6);
  // 如果所有 X 命中率相同（≈1/X 的均匀分布），置信度强制 0.4
  if (margin < 0.05) confidence = Math.min(confidence, 0.4);

  const top = sortedScores.slice(0, 5);
  let hint = '加权命中率算法：重拍位置 ±tolerance 内音符 duration 总和 / 总 duration';
  if (is68) hint += '；检测到长音符 dur≥2.5，按 6/8 拍识别';
  if (onBeat.length < 16) hint += '；数据 < 4 小节，置信度上限 0.6';
  if (margin < 0.05) hint += '；多个候选命中率接近，建议人工确认拍号';

  return Object.freeze({
    拍号,
    分子: bestX,
    分母,
    重拍间隔拍: bestX,
    重拍位置: downbeats,
    置信度: confidence,
    候选: Object.fromEntries(top),
    提示: hint,
  });
}

/**
 * 拍位置分析。给定 beat，返回小节号、拍内位置、强度档、强度值。
 * 强度档约定：首拍 1.0 / 次拍 0.8 / 8 分 0.75 / 16 分 0.65（与 melody.js dynamicVel 一致）。
 *
 * @param {number} beat
 * @param {number} [beatsPerBar=4]
 * @returns {Readonly<{
 *   小节: number,
 *   拍: number,
 *   拍内位置: number,
 *   位置名: '首拍' | '次拍' | '8分' | '16分首' | '16分末',
 *   强度档: '重' | '中' | '中弱' | '弱',
 *   强度值: number,
 *   强度排序: number,
 * }>}
 */
export function beatPosition(beat, beatsPerBar = 4) {
  const eps = 1e-6;
  const bpb = ensurePositiveInteger(beatsPerBar, 'beatsPerBar');
  const b = ensureFiniteNumber(beat, 'beat');
  const intBeat = Math.floor(b + eps);
  const frac = b - intBeat;
  const bar = Math.floor(intBeat / bpb);
  const beatInBar = intBeat - bar * bpb;

  let 位置名, 强度档, 强度值, 强度排序;
  // 优先级参考 dsh-pianist EVENT_TYPE_ORDER：先看更靠前的强位
  // 注意：beatInBar 必须是 0 才是「首拍」（整数拍位 1/2/3 是次拍/中拍，不是首拍）
  if (Math.abs(frac) < eps && beatInBar === 0) {
    位置名 = '首拍';
    强度档 = '重';
    强度值 = 1.0;
    强度排序 = 0;
  } else if (Math.abs(frac) < eps && beatInBar > 0) {
    // 整数非 0 拍位（如 4/4 拍的 1/2/3）= 次拍
    位置名 = '次拍';
    强度档 = '中';
    强度值 = 0.8;
    强度排序 = 1;
  } else if (Math.abs(frac - 0.5) < eps) {
    // 拍位中点（如 0.5, 1.5, 2.5, 3.5）= 8 分音符位
    位置名 = '8分';
    强度档 = '中弱';
    强度值 = 0.75;
    强度排序 = 2;
  } else if (Math.abs(frac - 0.25) < eps) {
    位置名 = '16分首';
    强度档 = '弱';
    强度值 = 0.65;
    强度排序 = 2;
  } else if (Math.abs(frac - 0.75) < eps) {
    位置名 = '16分末';
    强度档 = '弱';
    强度值 = 0.65;
    强度排序 = 3;
  } else {
    位置名 = '8分';
    强度档 = '中弱';
    强度值 = 0.75;
    强度排序 = 4;
  }

  return Object.freeze({
    小节: bar,
    拍: beatInBar,
    拍内位置: frac,
    位置名,
    强度档,
    强度值,
    强度排序,
  });
}

/**
 * 节奏分析：拍号 + 每个音符的时值/位置/强度 + 时值分布。
 * 输出可直接喂给 scoreToEvents 当作「带节奏理解的乐谱」。
 *
 * @param {Array<{beat: number, duration: number, midi?: number}>} notes
 * @param {{beatsPerBar?: number, minBar?: number, maxBar?: number}} [opts]
 * @returns {Readonly<{
 *   拍号: string,
 *   置信度: number,
 *   总音符数: number,
 *   时值分布: Record<string, number>,
 *   音符: ReadonlyArray<{...}>,
 *   摘要: {...},
 *   提示: string,
 * }>}
 */
export function analyzeRhythm(notes, opts = {}) {
  const arr = ensureArray(notes, 'notes');
  if (arr.length === 0) throw new TheoryError('音符数组为空', 'notes');
  const beatsPerBar = opts.beatsPerBar ?? 4;

  const ts = inferTimeSignature(arr, opts);

  // 时值名（按 4 分音符为基准）
  function durationName(d) {
    if (d >= 3.5) return '全音符';
    if (d >= 2.75) return '附点二分';
    if (d >= 1.75) return '附点四分';
    if (d >= 1.4) return '四分音符';
    if (d >= 0.85) return '四分音符';
    if (d >= 0.65) return '附点八分';
    if (d >= 0.4) return '八分音符';
    if (d >= 0.18) return '16分音符';
    if (d >= 0.09) return '32分音符';
    return '64分音符';
  }

  // 时值分布
  const durHist = new Map();
  for (const n of arr) {
    const d = ensureFiniteNumber(n.duration, 'notes[].duration');
    const name = durationName(d);
    durHist.set(name, (durHist.get(name) || 0) + 1);
  }

  // 每个音符标注
  const annotated = arr.map((n, i) => {
    const beat = ensureFiniteNumber(n.beat, `notes[${i}].beat`);
    const duration = ensureFiniteNumber(n.duration, `notes[${i}].duration`);
    const pos = beatPosition(beat, beatsPerBar);
    return Object.freeze({
      序号: i,
      midi: n.midi ?? null,
      拍: beat,
      时值拍: duration,
      时值名: durationName(duration),
      小节: pos.小节,
      拍内: pos.拍,
      拍内位置: pos.拍内位置,
      位置名: pos.位置名,
      强度档: pos.强度档,
      强度值: pos.强度值,
    });
  });

  // 切分检测：两个 8 分音符之间夹 16 分音符
  let 切分数 = 0;
  for (let i = 1; i < annotated.length - 1; i += 1) {
    const prev = annotated[i];
    const cur = annotated[i + 1];
    const curStart = cur.拍;
    const prevEnd = prev.拍 + prev.时值拍;
    if (
      prev.时值名 === '八分音符' &&
      cur.时值名 === '八分音符' &&
      Math.abs(curStart - (prev.拍 + 0.5)) > 1e-6 &&
      Math.abs(curStart - prevEnd) < 1e-6
    ) {
      切分数 += 1;
    }
  }

  // 摘要
  const sortedDur = [...durHist.entries()].sort((a, b) => b[1] - a[1]);
  const 重拍数 = annotated.filter((n) => n.位置名 === '首拍').length;
  const 次拍数 = annotated.filter((n) => n.位置名 === '次拍').length;
  const 弱拍数 = annotated.filter((n) => n.位置名 === '16分首' || n.位置名 === '16分末').length;

  return Object.freeze({
    拍号: ts.拍号,
    置信度: ts.置信度,
    总音符数: arr.length,
    时值分布: Object.fromEntries(durHist),
    切分数,
    音符: annotated,
    摘要: Object.freeze({
      最常用时值: sortedDur[0]?.[0] ?? '未知',
      时值种类: durHist.size,
      重拍数,
      次拍数,
      弱拍数,
    }),
    提示: '音符[i].强度值 与 melody.js dynamicVel 一致，可直接喂给 schedule_play',
  });
}
