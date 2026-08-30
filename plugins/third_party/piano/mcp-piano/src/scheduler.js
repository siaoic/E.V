// 演奏调度器：把"时间轴事件"一次发给浏览器，由浏览器侧精准调度。
//
// 设计：
// - Node 端不再 setTimeout 逐条发 note_on/note_off（旧的 setTimeout 漂移 +
//   串行化队列会把节奏拖慢）。
// - 改为一次性发送 schedule_play 消息（绝对时间戳），浏览器侧用
//   audioCtx.currentTime + sourceNode.start(when) 精准锁音。
// - 代际(generation)机制保留：新演奏/停止会取消上一批还没响的音符。
// - 严格只放行真钢琴采样覆盖范围内的 MIDI (21~108)，禁止任何合成音源兜底。

const MIN_PIANO_MIDI = 21; // A0
const MAX_PIANO_MIDI = 108; // C8

export class Scheduler {
  constructor(bridge) {
    this.bridge = bridge;
    this.generation = 0;
  }

  /**
   * 调度一组事件并执行
   * @param {Array<{midi:number, time:number, duration:number, velocity:number}>} events 绝对时间(秒)
   * @returns {{ok:boolean, noteCount:number, totalSeconds:number, error?:string, skippedCount:number, skippedInvalid:Array}}
   */
  schedule(events) {
    if (!this.bridge.isConnected()) {
      return {
        ok: false, noteCount: 0, totalSeconds: 0,
        skippedCount: 0, skippedInvalid: [],
        error: '浏览器未连接。请先打开 Piano.html 并确认页面右上角显示"MCP 已连接"。',
      };
    }

    const gen = ++this.generation;
    void gen; // 浏览器侧用 schedGen 自管

    // 校验：time/duration 有限、duration>0；midi 必须是整数且落在 88 键钢琴范围内（A0~C8）。
    // 超范围 / 非法直接 skip（不允许用合成音源替代），并返回 skipped 明细。
    const skippedInvalid = [];
    const valid = [];
    const safeEvents = Array.isArray(events) ? events : [];
    for (let i = 0; i < safeEvents.length; i += 1) {
      const e = safeEvents[i];
      if (!e) { skippedInvalid.push({ index: i, reason: '事件对象为空 (null/undefined)' }); continue; }
      if (!Number.isFinite(e.time)) { skippedInvalid.push({ index: i, midi: e.midi, reason: `time 非法 (${String(e.time)})` }); continue; }
      if (!Number.isFinite(e.duration) || e.duration <= 0) { skippedInvalid.push({ index: i, midi: e.midi, reason: `duration 必须大于 0（当前 ${String(e.duration)}）` }); continue; }
      if (!Number.isInteger(e.midi)) { skippedInvalid.push({ index: i, midi: e.midi, reason: `midi 必须是整数（当前 ${String(e.midi)}）` }); continue; }
      if (e.midi < MIN_PIANO_MIDI || e.midi > MAX_PIANO_MIDI) {
        skippedInvalid.push({ index: i, midi: e.midi, reason: `midi 超出 88 键钢琴范围 A0(21)~C8(108)，禁止合成替代` });
        continue;
      }
      valid.push(e);
    }

    if (valid.length === 0) {
      return {
        ok: false, noteCount: 0, totalSeconds: 0,
        skippedCount: skippedInvalid.length,
        skippedInvalid,
        error: skippedInvalid.length === 0
          ? '没有可演奏的音符（时长必须大于 0）'
          : `全部 ${skippedInvalid.length} 个事件非法：${skippedInvalid[0].reason}${skippedInvalid.length > 1 ? ` （另有 ${skippedInvalid.length - 1} 条）` : ''}`,
      };
    }

    // 一次性把所有音符交给浏览器侧精准调度
    // at: 绝对 ms 时间戳（相对起点 t0 = audioCtx.currentTime + leadMs）
    // dur: ms
    // velocity: 0~1
    // hand: 'left' | 'right' | undefined（给节奏大师着色）
    const notes = valid.map((e) => ({
      midi: e.midi,
      at: Math.round(Math.max(0, e.time) * 1000),
      dur: Math.round(Math.max(20, e.duration * 1000)), // 最少 20ms
      velocity: e.velocity != null ? e.velocity : 1,
      hand: e.hand,
    }));

    const lastEnd = Math.max(...valid.map((e) => e.time + e.duration));

    // 浏览器侧接管节奏：定速 leadMs（默认 120ms）后开始按 at 时间精准播放。
    this.bridge.send({ type: 'schedule_play', notes, leadMs: 120 });

    return {
      ok: true,
      noteCount: valid.length,
      totalSeconds: Number(lastEnd.toFixed(3)),
      skippedCount: skippedInvalid.length,
      skippedInvalid,
    };
  }

  /** 停止所有正在响的音符，并取消尚未到时的音符 */
  stopAll() {
    this.generation++;
    this.bridge.send({ type: 'stop_play' });
    return { ok: true };
  }
}