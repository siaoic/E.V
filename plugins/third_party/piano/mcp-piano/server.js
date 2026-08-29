#!/usr/bin/env node
// ============================================================
//  MCP Piano Server —— 让 AI 自己弹钢琴
//
//  架构：
//    MCP 客户端(AI) ──stdio──> 本服务 ──WebSocket(7788)──> 浏览器虚拟钢琴
//                                                          (Piano.html + mcpBridge.js)
//
//  用法：
//    1) 在浏览器打开 Piano/Piano.html（页面右上角出现"MCP 已连接"）
//    2) 用任意 MCP 客户端把本文件注册为 MCP server：
//         node /path/to/mcp-piano/server.js
//    3) 对 AI 说"弹一首《小星星》"即可
// ============================================================

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

import { readFileSync } from 'node:fs';

import { Bridge } from './src/bridge.js';
import { Scheduler } from './src/scheduler.js';
import { parseMelodyString, scoreToEvents } from './src/melody.js';
import { loadMidiFile } from './src/midiPlayer.js';
import { noteToMidi, normalizeNote, midiToNote } from './src/notes.js';
import { analyzeChord, getScale, keySignature, analyzeInterval, chordProgression, harmonize } from './src/theory.js';

const log = (...args) => process.stderr.write('[mcp-piano] ' + args.join(' ') + '\n');

// ---------- 基础设施 ----------
const bridge = new Bridge();
const scheduler = new Scheduler(bridge);

try {
  await bridge.start();
  log(`WebSocket 桥接层已就绪：浏览器请连接 ws://localhost:${bridge.port}（打开 Piano.html 会自动连接）`);
} catch (e) {
  log(`启动 WebSocket 失败：${e.message}`);
  process.exit(1);
}

const server = new McpServer({
  name: 'mcp-piano',
  version: '1.0.0',
});

const ok = (text, extra = {}) => ({
  content: [{ type: 'text', text }],
  structuredContent: { ok: true, ...extra },
  _meta: {},
});
const fail = (error) => ({
  content: [{ type: 'text', text: `❌ ${error}` }],
  structuredContent: { ok: false, error },
  isError: true,
  _meta: {},
});

const noteSchema = z.union([z.string(), z.number()]).describe('音符：如 "C4"、"C#4"、"Db4"、"A0"，或 MIDI 编号 21~108');

// ---------- 工具 1：获取状态 ----------
server.tool(
  'get_status',
  '查看钢琴/桥接层当前状态：浏览器是否已连接、WebSocket 端口、浏览器标题、音频上下文状态、当前音量等',
  {},
  async () => {
    const connected = bridge.isConnected();
    const meta = bridge.meta || {};
    return ok(
      JSON.stringify(
        {
          浏览器已连接: connected,
          端口: bridge.port,
          浏览器标题: meta.title || null,
          页面地址: meta.url || null,
          音频状态: meta.audioState || null,
          琴键范围: 'A0 ~ C8',
          当前音量: meta.volume ?? null,
          提示: connected ? '可以开始演奏了，例如 play_note("C4")' : '浏览器未连接，请先打开 Piano.html',
        },
        null,
        2
      ),
      { connected, port: bridge.port, title: meta.title, audioState: meta.audioState }
    );
  }
);

// ---------- 工具 2：单音 ----------
server.tool(
  'play_note',
  '弹奏单个音符。note 可以是 "C4"、"C#4"、"Db4" 等，也可以是 MIDI 编号。duration 为时长(秒，默认 0.6)。velocity 为力度 0~1（默认 1）。',
  {
    note: noteSchema,
    duration: z.number().min(0.05).max(30).optional().describe('时长（秒），默认 0.6'),
    velocity: z.number().min(0.05).max(1).optional().describe('力度 0~1，默认 1'),
  },
  async ({ note, duration = 0.6, velocity = 1 }) => {
    const midi = noteToMidi(note);
    if (midi === null) return fail(`无法解析音符 "${note}"`);
    const r = scheduler.schedule([{ midi, time: 0, duration, velocity }]);
    if (!r.ok) return fail(r.error);
    return ok(`正在弹奏 ${midiToNote(midi)}（MIDI ${midi}，${duration}s）`, { note: midiToNote(midi), midi, duration });
  }
);

// ---------- 工具 3：和弦 ----------
server.tool(
  'play_chord',
  '同时弹奏多个音符（和弦）。notes 为音符数组，如 ["C4","E4","G4"]。',
  {
    notes: z.array(noteSchema).min(1).max(20),
    duration: z.number().min(0.05).max(30).optional().describe('时长（秒），默认 1.2'),
    velocity: z.number().min(0.05).max(1).optional().describe('力度 0~1，默认 1'),
  },
  async ({ notes, duration = 1.2, velocity = 1 }) => {
    const midis = [];
    for (const n of notes) {
      const m = noteToMidi(n);
      if (m === null) return fail(`无法解析音符 "${n}"`);
      midis.push(m);
    }
    const events = midis.map((midi) => ({ midi, time: 0, duration, velocity }));
    const r = scheduler.schedule(events);
    if (!r.ok) return fail(r.error);
    return ok(`正在弹奏和弦 [${midis.map(midiToNote).join(', ')}]`, { notes: midis.map(midiToNote), duration });
  }
);

// ---------- 工具 4：旋律 ----------
const melodyItemSchema = z.object({
  note: noteSchema.optional(),
  beat: z.number().min(0).optional().describe('起始拍（从 0 开始）'),
  duration: z.number().min(0.05).optional().describe('持续拍数'),
  velocity: z.number().min(0.05).max(1).optional(),
});

server.tool(
  'play_melody',
  '按顺序弹奏一段旋律。melody 可以是：① 字符串，如 "C4 E4 G4 C5"（空格分隔，每拍一个音；可用 "C4:2" 指定拍数、"C4@0.8" 指定力度、"R" 休止）；② 音符对象数组，每个 {note, beat, duration}。tempo 为速度(BPM，默认 100)。',
  {
    melody: z.union([z.string(), z.array(melodyItemSchema)]),
    tempo: z.number().min(20).max(300).optional().describe('速度 BPM，默认 100'),
  },
  async ({ melody, tempo = 100 }) => {
    let notes;
    if (typeof melody === 'string') {
      const parsed = parseMelodyString(melody);
      if (parsed.error) return fail(parsed.error);
      notes = parsed.notes.map((n) => ({ midi: n.midi, beat: n.beat, duration: n.beats, velocity: n.velocity }));
    } else {
      notes = melody;
      if (!Array.isArray(notes) || notes.length === 0) return fail('melody 数组为空');
      const invalid = notes.find((n) => !n || (!n.note && n.midi === undefined));
      if (invalid) return fail('melody 中的每个对象都需要提供 note（如 "C4"）或 midi');
    }
    const events = scoreToEvents(notes, tempo);
    if (events.length === 0) return fail('没有可演奏的音符');
    const r = scheduler.schedule(events);
    if (!r.ok) return fail(r.error);
    return ok(
      `开始弹奏旋律：${events.length} 个音符，约 ${r.totalSeconds}s（${tempo} BPM）`,
      { noteCount: events.length, totalSeconds: r.totalSeconds, tempo }
    );
  }
);

// ---------- 工具 5：乐谱（多声部） ----------
server.tool(
  'play_score',
  '弹奏一首完整乐谱（支持左右手多声部）。两种传法：① score = {tempo: 120, tracks: [{notes: [{note:"C4", beat:0, duration:1}]}]}；② path = 本地 score JSON 文件路径（大谱面推荐，read_sheet_music 的输出）。注意：path 仅接受 read_sheet_music 产出的 score JSON 文件——.txt/.mid/.png 等其他文件一律禁止传入本工具（.mid 用 play_midi_file，图片先 read_sheet_music；用户给的 .txt 不是乐谱，直接说明无法弹奏）。各声部同时进行。tempo 必须用真实值：谱面标注的、或联网搜索（bing_search）到的原曲 BPM；谱面无标注且未搜索时不要凭记忆猜。',
  {
    score: z
      .object({
        tempo: z.number().min(20).max(300).optional(),
        tracks: z
          .array(z.object({ notes: z.array(melodyItemSchema) }))
          .min(1)
          .max(16),
      })
      .optional(),
    path: z.string().optional().describe('本地 score JSON 文件的绝对路径（与 score 二选一）'),
  },
  async ({ score, path }) => {
    let sc = score;
    if (!sc && path) {
      try {
        sc = JSON.parse(readFileSync(path, 'utf-8'));
      } catch (e) {
        return fail(`读取谱面文件失败：${e.message}`);
      }
    }
    if (!sc || !Array.isArray(sc.tracks) || sc.tracks.length === 0) {
      return fail('需要提供 score 对象或 path（谱面 JSON 文件路径）');
    }
    const tempo = sc.tempo ?? 120;
    const all = [];
    for (const track of sc.tracks) {
      const notes = track.notes ?? [];
      if (notes.length === 0) continue;
      const evs = scoreToEvents(notes, tempo);
      all.push(...evs);
    }
    if (all.length === 0) return fail('乐谱中没有可演奏的音符');
    const r = scheduler.schedule(all);
    if (!r.ok) return fail(r.error);
    return ok(`开始弹奏乐谱：${r.noteCount} 个音符（${sc.tracks.length} 个声部），约 ${r.totalSeconds}s`, {
      noteCount: r.noteCount,
      tracks: sc.tracks.length,
      tempo,
      totalSeconds: r.totalSeconds,
    });
  }
);

// ---------- 工具 6：MIDI 文件 ----------
server.tool(
  'play_midi_file',
  '读取本地 .mid 文件并弹奏。仅支持 .mid/.midi 文件——jpg/png 等乐谱图片不能用本工具（会报文件不存在），图片谱必须先用 read_sheet_music 识谱、再调用 play_score。path 为文件的绝对路径。',
  {
    path: z.string().describe('本地 .mid 文件的绝对路径（乐谱图片不可用本工具）'),
  },
  async ({ path }) => {
    const parsed = loadMidiFile(path);
    if (parsed.error) return fail(parsed.error);
    const r = scheduler.schedule(parsed.events);
    if (!r.ok) return fail(r.error);
    return ok(
      `开始弹奏 MIDI「${parsed.title || path}」：${parsed.noteCount} 个音符，${parsed.tempo} BPM，约 ${parsed.durationSeconds}s`,
      { title: parsed.title, noteCount: parsed.noteCount, tempo: parsed.tempo, totalSeconds: parsed.durationSeconds }
    );
  }
);

// ---------- 工具 7：停止 ----------
server.tool(
  'stop_all',
  '立即停止当前所有正在响的音符（包括还没轮到响的音符）。',
  {},
  async () => {
    scheduler.stopAll();
    return ok('已停止所有音符');
  }
);

// ---------- 工具 8：音量 ----------
server.tool(
  'set_volume',
  '设置钢琴主音量（0~200，默认 100）。',
  { volume: z.number().min(0).max(200) },
  async ({ volume }) => {
    const sent = bridge.send({ type: 'set_volume', value: volume });
    if (!sent) return fail('浏览器未连接');
    return ok(`音量已设为 ${volume}`);
  }
);

// ---------- 工具 9：延音踏板 ----------
server.tool(
  'set_sustain',
  '开启/关闭延音踏板（sustain）。开启后音符会自然延续。',
  { on: z.boolean() },
  async ({ on }) => {
    const sent = bridge.send({ type: 'set_sustain', on });
    if (!sent) return fail('浏览器未连接');
    return ok(`延音踏板已${on ? '开启' : '关闭'}`);
  }
);

// ---------- 工具 10：乐理小助手 ----------
server.tool(
  'music_theory',
  '乐理小助手：本地分析乐理问题并给出可直接试听的音符。query 可选：' +
    '"chord"（解析和弦符号，如 Cmaj7，返回组成音+MIDI 编号）、' +
    '"scale"（查音阶/调式，key+type，如 key=C type=major）、' +
    '"key_signature"（查调号与关系大小调，key 如 C/Am）、' +
    '"interval"（分析两个音的音程，from/to，如 C4→G4）、' +
    '"progression"（级数进行解析，key + roman，如 roman="I-V-vi-IV"）、' +
    '"harmonize"（为旋律推荐调内和弦，melody=音符数组 + key）。' +
    '返回结果附 MIDI 编号，可用 play_chord / play_melody / play_score 直接试听。',
  {
    query: z.enum(['chord', 'scale', 'key_signature', 'interval', 'progression', 'harmonize'])
      .describe('要做的乐理分析类型'),
    symbol: z.string().optional().describe('chord 用：和弦符号，如 "Cmaj7"、"Am"、"Gsus4"'),
    key: z.string().optional().describe('scale/key_signature/progression/harmonize 用：调，如 "C"、"Am"、"F#"'),
    type: z.string().optional().describe('scale 用：音阶类型，如 major/minor/harmonic_minor/pentatonic/blues/dorian/mixolydian 等'),
    from: noteSchema.optional().describe('interval 用：起始音，如 "C4"'),
    to: noteSchema.optional().describe('interval 用：目标音，如 "G4"'),
    roman: z.string().optional().describe('progression 用：罗马数字级数，如 "I-V-vi-IV"、"ii-V-I"'),
    melody: z.array(noteSchema).min(1).max(64).optional().describe('harmonize 用：旋律音符数组，如 ["E4","D4","C4","D4","E4","E4","E4"]'),
    seventh: z.boolean().optional().describe('progression/harmonize 用：是否使用七和弦（默认 false）'),
  },
  async ({ query, symbol, key, type, from, to, roman, melody, seventh }) => {
    let result;
    switch (query) {
      case 'chord':
        if (!symbol) return fail('chord 查询需要 symbol 参数，如 "Cmaj7"');
        result = analyzeChord(symbol);
        break;
      case 'scale':
        if (!key) return fail('scale 查询需要 key 参数，如 "C"');
        result = getScale(key, type ?? 'major');
        break;
      case 'key_signature':
        if (!key) return fail('key_signature 查询需要 key 参数，如 "C" 或 "Am"');
        result = keySignature(key);
        break;
      case 'interval':
        if (from === undefined || to === undefined) return fail('interval 查询需要 from 和 to 两个音符，如 from="C4", to="G4"');
        result = analyzeInterval(from, to);
        break;
      case 'progression':
        if (!key) return fail('progression 查询需要 key 参数，如 "C" 或 "Am"');
        if (!roman) return fail('progression 查询需要 roman 参数，如 "I-V-vi-IV"');
        result = chordProgression(key, roman, !!seventh);
        break;
      case 'harmonize':
        if (!melody) return fail('harmonize 查询需要 melody 参数，如 ["E4","D4","C4"]');
        result = harmonize(melody, key ?? 'C', !!seventh);
        break;
      default:
        return fail(`未知的 query 类型 "${query}"`);
    }
    if (!result || result.error) return fail(result?.error ?? '分析失败');
    return ok(JSON.stringify(result, null, 2), result);
  }
);

// ---------- 启动（stdio） ----------
const transport = new StdioServerTransport();
await server.connect(transport);
log('✅ MCP Piano Server 已启动（stdio）。现在可以让 AI 弹琴了。');

// 优雅退出
process.on('SIGINT', () => {
  scheduler.stopAll();
  process.exit(0);
});
