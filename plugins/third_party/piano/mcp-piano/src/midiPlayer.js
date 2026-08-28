// MIDI 文件播放支持：读取本地 .mid 文件，转成带绝对时间的事件后交给调度器
// 依赖 @tonejs/midi

import fs from 'node:fs';
import { default as MidiModule } from '@tonejs/midi';
import { midiToNote } from './notes.js';

const { Midi } = MidiModule;

/**
 * 解析 MIDI 文件，返回 { tempo, events, error? }
 * @param {string} path 本地 .mid 文件绝对路径
 */
export function loadMidiFile(path) {
  let buf;
  try {
    buf = fs.readFileSync(path);
  } catch (e) {
    return { error: `无法读取文件 "${path}": ${e.message}` };
  }

  let midi;
  try {
    midi = new Midi(buf);
  } catch (e) {
    return { error: `解析 MIDI 失败: ${e.message}` };
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
