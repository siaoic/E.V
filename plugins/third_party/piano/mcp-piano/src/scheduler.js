// 演奏调度器：把"时间轴事件"一次发给浏览器，由浏览器侧精准调度。
//
// 设计：
// - Node 端不再 setTimeout 逐条发 note_on/note_off（旧的 setTimeout 漂移 +
//   串行化队列会把节奏拖慢）。
// - 改为一次性发送 schedule_play 消息（绝对时间戳），浏览器侧用
//   audioCtx.currentTime + sourceNode.start(when) 精准锁音。
// - 代际(generation)机制保留：新演奏/停止会取消上一批还没响的音符。

export class Scheduler {
  constructor(bridge) {
    this.bridge = bridge;
    this.generation = 0;
  }

  /**
   * 调度一组事件并执行
   * @param {Array<{midi:number, time:number, duration:number, velocity:number}>} events 绝对时间(秒)
   * @returns {{ok:boolean, noteCount:number, totalSeconds:number, error?:string}}
   */
  schedule(events) {
    if (!this.bridge.isConnected()) {
      return { ok: false, noteCount: 0, totalSeconds: 0, error: '浏览器未连接。请先打开 Piano.html 并确认页面右上角显示"MCP 已连接"。' };
    }

    const gen = ++this.generation;
    void gen; // 浏览器侧用 schedGen 自管

    const valid = events.filter(
      (e) => e && Number.isFinite(e.time) && Number.isFinite(e.duration) && e.duration > 0
    );
    if (valid.length === 0) {
      return { ok: false, noteCount: 0, totalSeconds: 0, error: '没有可演奏的音符（时长必须大于 0）' };
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
    };
  }

  /** 停止所有正在响的音符，并取消尚未到时的音符 */
  stopAll() {
    this.generation++;
    this.bridge.send({ type: 'stop_play' });
    return { ok: true };
  }
}