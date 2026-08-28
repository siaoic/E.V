#!/usr/bin/env node
// 独立演示脚本：不依赖 MCP 客户端，直接通过 WebSocket 让浏览器钢琴弹一段曲子。
// 用法：
//   node demo.js [twinkle|ode|jingle]     （默认 twinkle）
// 前提：浏览器已打开 Piano/Piano.html，并且本脚本会等待其连上桥接层。

import { Bridge } from './src/bridge.js';
import { Scheduler } from './src/scheduler.js';
import { parseMelodyString, scoreToEvents } from './src/melody.js';

const SONGS = {
  twinkle: {
    name: '小星星',
    tempo: 100,
    melody:
      'C4 C4 G4 G4 A4 A4 G4:2 F4 F4 E4 E4 D4 D4 C4:2 ' +
      'G4 G4 F4 F4 E4 E4 D4:2 G4 G4 F4 F4 E4 E4 D4:2 ' +
      'C4 C4 G4 G4 A4 A4 G4:2 F4 F4 E4 E4 D4 D4 C4:2',
  },
  ode: {
    name: '欢乐颂',
    tempo: 110,
    melody:
      'E4 E4 F4 G4 G4 F4 E4 D4 C4 C4 D4 E4 E4:1.5 D4:0.5 D4:2 ' +
      'E4 E4 F4 G4 G4 F4 E4 D4 C4 C4 D4 E4 D4:1.5 C4:0.5 C4:2',
  },
  jingle: {
    name: '铃儿响叮当',
    tempo: 120,
    melody:
      'E4 E4 E4:2 E4 E4 E4:2 E4 G4 C4:1.5 D4:0.5 E4:2 ' +
      'F4 F4 F4:1.5 F4:0.5 F4 E4 E4 E4:1 E4:1 E4 D4 D4 E4 D4:2 G4:2',
  },
};

const songKey = process.argv[2] || 'twinkle';
const song = SONGS[songKey];
if (!song) {
  console.error(`未知歌曲：${songKey}。可选：${Object.keys(SONGS).join(', ')}`);
  process.exit(1);
}

const bridge = new Bridge();
await bridge.start();
console.log(`🎹 演示程序已启动：请确保浏览器已打开 Piano/Piano.html`);
console.log(`   等待浏览器连接桥接层（ws://localhost:${bridge.port}）...`);

const deadline = Date.now() + 30_000;
while (!bridge.isConnected() && Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 500));
}
if (!bridge.isConnected()) {
  console.error('❌ 30 秒内没有检测到浏览器连接。请打开 Piano/Piano.html 后重试。');
  process.exit(1);
}

console.log(`✅ 浏览器已连接，开始弹奏《${song.name}》（${song.tempo} BPM）...`);
const scheduler = new Scheduler(bridge);
const parsed = parseMelodyString(song.melody);
if (parsed.error) {
  console.error('旋律解析失败：' + parsed.error);
  process.exit(1);
}
const events = scoreToEvents(
  parsed.notes.map((n) => ({ midi: n.midi, beat: n.beat, duration: n.beats, velocity: 0.9 })),
  song.tempo
);
const r = scheduler.schedule(events);
console.log(`   ${r.ok ? `已调度 ${r.noteCount} 个音符，约 ${r.totalSeconds}s` : '失败: ' + r.error}`);
console.log('   演奏结束后本程序退出。');
await new Promise((r) => setTimeout(r, (r.ok ? r.totalSeconds : 2) * 1000 + 500));
scheduler.stopAll();
console.log('✅ 演示结束。');
process.exit(0);
