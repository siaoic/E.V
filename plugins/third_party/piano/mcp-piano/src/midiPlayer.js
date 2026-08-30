// MIDI 文件播放支持：读取本地 .mid 文件，转成带绝对时间的事件后交给调度器
// 依赖 @tonejs/midi

import fs from 'node:fs';
import path from 'node:path';
import { default as MidiModule } from '@tonejs/midi';
import { midiToNote } from './notes.js';

const { Midi } = MidiModule;

// 仅允许标准 MIDI 文件扩展名（不区分大小写）。其他文件（png/jpg/txt/json 等）
// 一律在此层拦截，避免把非 MIDI 的二进制大文件塞进 @tonejs/midi 导致 OOM 或 undefined 报错。
const MIDI_EXT_WHITELIST = new Set(['.mid', '.midi']);
// 超过 8MB 也拒绝，避免恶意/超大文件把 Node 进程撑爆
const MAX_MIDI_BYTES = 8 * 1024 * 1024;

/** 把 catch(e) 中拿到的对象格式化成稳定可读的错误字符串，避免出现 "undefined" */
function formatError(e, fallback = '（无错误信息）') {
  if (e == null) return fallback;
  if (typeof e === 'string') return e || fallback;
  const msg = e.message || e.msg || e.error || e.code;
  if (typeof msg === 'string' && msg.length > 0) return msg;
  try {
    return String(e) || fallback;
  } catch {
    return fallback;
  }
}

/**
 * 解析 MIDI 文件，返回 { tempo, events, error? }
 * @param {string} filePath 本地 .mid 文件绝对路径
 */
export function loadMidiFile(filePath) {
  if (!filePath || typeof filePath !== 'string') {
    return { error: 'path 为空或不是字符串，无法读取 MIDI 文件' };
  }

  // 1) 扩展名白名单：先把 "." 前面的查询参数/#锚点（理论上本地路径不会有，但解析一下更稳妥）去掉
  let normalized;
  try {
    normalized = String(filePath).trim();
  } catch {
    normalized = filePath;
  }
  const ext = path.extname(normalized).toLowerCase();
  if (!MIDI_EXT_WHITELIST.has(ext)) {
    const msg =
      `不支持的文件扩展名 "${ext}"（当前文件: "${filePath}"）。` +
      `play_midi_file 仅接受真钢琴谱对应的标准 MIDI 文件，扩展名必须是 .mid 或 .midi。` +
      `如果给的是乐谱图片，请先用 read_sheet_music 识谱、再用 play_score 弹奏；` +
      `如果是 txt/JSON 谱面，请改用 play_score（JSON）或 play_melody（文本旋律）。`;
    return { error: msg };
  }

  let buf;
  try {
    const stat = fs.statSync(normalized);
    if (!stat.isFile()) {
      return { error: `"${filePath}" 不是一个普通文件（可能是目录或设备）` };
    }
    if (stat.size > MAX_MIDI_BYTES) {
      return {
        error: `MIDI 文件过大（${(stat.size / 1024 / 1024).toFixed(2)}MB，上限 ${MAX_MIDI_BYTES / 1024 / 1024}MB），拒绝读取`,
      };
    }
    buf = fs.readFileSync(normalized);
  } catch (e) {
    return { error: `无法读取文件 "${filePath}": ${formatError(e)}` };
  }

  if (!buf || buf.length === 0) {
    return { error: `MIDI 文件为空（0 字节）："${filePath}"` };
  }

  let midi;
  try {
    midi = new Midi(buf);
  } catch (e) {
    return {
      error:
        `解析 MIDI 文件失败："${filePath}"。` +
        `可能不是有效的标准 MIDI 文件，或文件已损坏。详情：${formatError(e)}`,
    };
  }

  const tempo = midi.header.tempos?.[0]?.bpm ?? 120;
  const events = [];
  let maxTime = 0;

  for (const track of midi.tracks) {
    // 只取音高类音符，跳过鼓组(9)
    if (track.number === 9) continue;
    for (const note of track.notes) {
      if (note.midi < 21 || note.midi > 108) continue; // 超出钢琴音域则跳过
      events.push({
        midi: note.midi,
        time: note.time,
        duration: Math.max(0.05, note.duration),
        velocity: note.velocity ?? 1,
      });
      if (note.time + note.duration > maxTime) maxTime = note.time + note.duration;
    }
  }

  if (events.length === 0) {
    return { error: '该 MIDI 文件中没有可演奏的音符（或全部超出钢琴 88 键音域）' };
  }

  events.sort((a, b) => a.time - b.time);

  return {
    tempo,
    durationSeconds: Number(maxTime.toFixed(3)),
    noteCount: events.length,
    title: midi.header.name || null,
    events,
    sample: events.slice(0, 8).map((e) => `${midiToNote(e.midi)}@${e.time.toFixed(2)}s`).join(' '),
  };
}
