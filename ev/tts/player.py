"""TTS 播放端公共逻辑：无缝播放 + 口型回调 + 词级时间戳字幕。

TTSEngine（本地进程内合成）与 TTSClient（远程 HTTP 合成）共用本模块的
TTSPlayer，消除两套播放/字幕/打断逻辑的重复：
- _AudioQueue：单条 sounddevice OutputStream + 常驻消费线程无缝播放；
- _SubtitlesQueue：词级时间戳字幕，锚定播放线程真实开播时刻渐进输出；
- TTSPlayer：编排层，负责拆块入队、口型回调、字幕锚点与打断。
"""

import asyncio
import itertools
import os
import queue
import tempfile
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

from ev.utils import console
from ev.utils import config


def _find_wasapi_device(name: str) -> Optional[int]:
    """按设备名子串匹配 WASAPI 输出端点（低延迟专用，返回 index）。

    Windows 默认输出通常走 MME（最老 API，即使 latency='low' 缓冲仍
    90ms，首句出声晚）。WASAPI 共享端点实测首声延迟降到 ~22ms——
    TTS_OUTPUT_DEVICE 配置混音台/虚拟设备名（如「Voicemeeter Input」）时
    自动改用其 WASAPI 端点；匹配不到或非 Windows 返回 None（走默认）。
    """
    name = (name or "").strip().lower()
    if not name:
        return None
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] <= 0:
                continue
            hostapi = sd.query_hostapis()[d["hostapi"]]["name"]
            if "wasapi" not in hostapi.lower():
                continue
            if name in d["name"].lower():
                return i
    except Exception:
        return None
    return None

# 本地播放入队块长（秒）：整段音频按固定时长分块，块间由设备缓冲衔接无缝播放。
# 过大延迟首块出声，过小口型回调过于频繁，0.5s 为平衡值。
_PLAY_CHUNK_SEC = 0.5

# 播放块首尾淡化时长（秒）：写入声卡前对每块首尾做 2ms 线性淡化。
# 句首非零起播、设备缓冲欠载（underrun）、块边界阶跃等瞬间跳变会打出
# 爆音（click/pop）——2ms 淡化削平阶跃的高频能量；对正常连续音频，
# 每 0.5s 一次 2ms（约 0.4%）的轻微凹口人耳不可闻，是播放器标准防爆音手段。
_FADE_SEC = 0.002


def _apply_edge_fade(data: np.ndarray, sr: int) -> np.ndarray:
    """对音频块首尾做 _FADE_SEC 线性淡化（消除爆音），返回原数组（原地修改）。

    data：1D float32 或 (N,1)；sr：采样率。块长不足两倍淡化窗时跳过。
    """
    n = int(_FADE_SEC * sr)
    if n < 1 or data.shape[0] <= 2 * n:
        return data
    d = data[:, 0] if data.ndim == 2 else data
    fade_in = np.linspace(0.0, 1.0, n, dtype=np.float32)
    d[:n] *= fade_in
    d[-n:] *= fade_in[::-1]
    return data

# 说话结束复原防抖（秒）：队列排空后仍无新音频才视为说话结束。
# 多句连播时句间合成间隙（< 1.2s）不触发复原，避免表情/动作在句间闪烁。
_DONE_DEBOUNCE = 1.2


class _AudioQueue:
    """本地扬声器无缝播放队列（复用旧引擎实现，与 GSV-TTS-Lite 播放无关）。

    单条 sounddevice OutputStream + 常驻消费线程：逐块 write，块间由设备
    缓冲衔接实现无缝播放。队列空时置空闲标志（drain 判定播完）并继续等待，
    线程不随队列空退出——打断（清队列）后新块能自动续播，避免「打断后
    合成了却无人播放」的竞态（旧一次性线程在打断瞬间退出导致丢新块）。

    on_block 回调：每块音频真正 write 前调用（引擎用它取播放元信息：
    字幕锚点 + 口型回调 + 临时 wav 即时删除）。
    """

    def __init__(self, samplerate: int,
                 on_block: Optional[Callable[[], None]] = None,
                 on_idle: Optional[Callable[[], None]] = None) -> None:
        self.samplerate = samplerate
        self.q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue()
        # 未播放块计数：put +1、消费 -1、clear 归零。playback_finished 只在
        # 计数归零时置位——避免「队列瞬间为空但新块将入队」时 drain 误判结束
        # （原实现在队列空 0.25s 检测窗内置位，与 put 之间存在竞态窗口）。
        self._pending = 0
        self._pending_lock = threading.Lock()
        self.playback_finished = threading.Event()
        self.playback_finished.set()
        self._stopped = False       # close() 后线程退出
        self._on_block = on_block
        self._on_idle = on_idle
        self._idle_fired = True     # 初始空闲不触发；put 后置 False，播空触发一次
        try:
            # 显式 50ms 缓冲替代 'low'：'low' 在 Windows 默认 MME 下仍高达
            # ~90ms，首句出声明显偏晚；50ms 是播放稳定与低延迟的平衡值
            # （CPU 偶发抖动也不易爆音）。配置 TTS_OUTPUT_DEVICE 时改走
            # WASAPI 独占端点（如混音台 Voicemeeter），首声延迟再降一半。
            self.stream = self._open_stream(samplerate)
            self.stream.start()
        except Exception:
            self.stream = None      # 无声卡/设备占用：仅消费队列不写设备
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _open_stream(self, samplerate: int):
        """创建输出流：配置了 TTS_OUTPUT_DEVICE 时优先 WASAPI 共享模式（低延迟）。

        WASAPI 共享 + auto_convert：Windows 音频引擎自动把 32k 转为设备
        原生采样率（Voicemeeter/CABLE 虚拟设备为 48k）。实测独占模式在
        VB 虚拟声卡上采样率协商错乱全是噪音、且独占占用设备导致其它
        程序无法使用；共享模式无噪音、多客户端共存，延迟 ~22-24ms。
        任一失败回退默认设备（保持旧行为，仅缓冲降至 50ms）。
        """
        kwargs: dict = {"latency": 0.05}
        device_name = str(getattr(config.cfg, "TTS_OUTPUT_DEVICE", "") or "")
        if device_name:
            idx = _find_wasapi_device(device_name)
            if idx is not None:
                kwargs.update(device=idx)
                try:
                    # 共享模式 + auto_convert：系统负责 32k→设备原生采样率
                    # 转换（block 大小由音频引擎自动选择）
                    kwargs["extra_settings"] = sd.WasapiSettings(
                        exclusive=False, auto_convert=True)
                except Exception:
                    pass
        try:
            return sd.OutputStream(samplerate=samplerate, channels=1,
                                   dtype="float32", **kwargs)
        except Exception:
            # 回退默认设备：清掉独占/指定设备参数重试
            return sd.OutputStream(samplerate=samplerate, channels=1,
                                   dtype="float32", latency=0.05)

    def put(self, data: np.ndarray) -> None:
        """把一块 1D float32 音频入队，由常驻线程写入声卡。"""
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        self.q.put(data)
        with self._pending_lock:
            self._pending += 1
        self._idle_fired = False
        self.playback_finished.clear()

    def _run(self) -> None:
        while not self._stopped:
            try:
                data = self.q.get(timeout=0.25)
            except queue.Empty:
                # 队列空：标记空闲（drain 可返回），继续等待新数据；
                # 本轮有音频播完后排空 → 通知上层（说话结束复原等）
                if not self._idle_fired:
                    self._idle_fired = True
                    if self._on_idle is not None:
                        try:
                            self._on_idle()
                        except Exception:
                            pass
                with self._pending_lock:
                    if self._pending <= 0:
                        self.playback_finished.set()
                continue
            self.playback_finished.clear()
            try:
                if self._on_block is not None:
                    self._on_block()
                if self.stream is not None:
                    # 写入前首尾淡化：削平起播/块边界/欠载引起的爆音阶跃
                    self.stream.write(_apply_edge_fade(data, self.samplerate))
            except Exception:
                pass  # 打断等瞬态错误：丢弃该块，等待下一块
            finally:
                with self._pending_lock:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._pending = 0
                        self.playback_finished.set()
        self.playback_finished.set()

    def clear(self) -> None:
        """丢弃所有未播块并标记空闲（不碰 stream，避免打断卡死播放线程）。"""
        with self.q.mutex:
            self.q.queue.clear()
        with self._pending_lock:
            self._pending = 0
        self.playback_finished.set()

    def close(self) -> None:
        """停止消费线程并关闭输出流（进程退出前调用）。"""
        self._stopped = True
        self.clear()
        if self.t.is_alive():
            self.t.join(timeout=1.0)
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass


class _SubtitlesQueue:
    """按时间戳渐进输出字幕（对齐真实播放时刻，与 GSV-TTS-Lite 官方对齐）。

    GSV-TTS-Lite 返回词级时间戳（subtitles: [{text, start_s, end_s,
    orig_idx_start, orig_idx_end}]，相对音频起点），逐词锚定到播放线程真实
    开播时刻推进：
    - 锚点 t0 = 句首块真实开播墙钟时刻（播放线程在首块 write 前写入）；
    - 每个词在 t0 + start_s 时刻把「原文累积文本」（前 orig_idx_end+1 个
      字符）交给 sink（本项目为 sub.push("text", ...)），气泡/网页随之
      逐字浮现；
    - 流式合成按块到达：每块的字幕增量 add_blocks() 单独入队（官方示例
      subtitlesqueue.add(audio.subtitles, audio.orig_text) 的 1:1 语义），
      队列按整句基准时间戳跨块推进，句首块起即与播放逐词同步；
    - 播放领先时间轴时自动追赶（一次推送多词），不丢字；
    - 字幕只用 GSV-TTS-Lite 返回的真实词级时间戳，绝不估算；某句缺
      少时间戳时不输出该句字幕（宁缺毋假）。
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        self.q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self.t: Optional[threading.Thread] = None
        self._sink = sink
        self._anchors: dict = {}      # sent_id -> 句首块实际开播墙钟时刻
        self._anchors_lock = threading.Lock()
        self._last_idx: dict = {}     # sent_id -> 已推进到的原文下标（跨块累积）
        self._closed = False          # 打断：终止当前字幕线程（下次 add 重建）

    def process(self) -> None:
        """字幕处理主循环：逐块按词级时间戳渐进输出（追赶式，不丢字）。"""
        while True:
            item = self.q.get()
            if item is None:
                break
            text, subtitles, sent_id, first = item
            self._run_block(text, subtitles, sent_id, first)
        self.t = None

    def _run_block(self, text: str, subtitles: list,
                   sent_id: int, first: bool) -> None:
        """推进一块字幕增量：每个词在 t0+start_s 推送原文累积文本。

        first=True 表示该句首块：重置推进下标并等待句首块真实开播锚点；
        后续块沿用已推进下标继续（跨块累积，与官方 SubtitlesQueue 的
        last_i 语义一致）。
        """
        if not text or not subtitles:
            return
        if first:
            # 句首块：等待播放线程真正开播（write 前触发 anchor）
            while sent_id not in self._anchors:
                if self._closed:
                    self.t = None
                    return
                time.sleep(0.005)
            with self._anchors_lock:
                t0 = self._anchors[sent_id]
            idx = 0
        else:
            with self._anchors_lock:
                t0 = self._anchors.get(sent_id, 0.0)
            idx = self._last_idx.get(sent_id, 0)
        for item in subtitles:
            if self._closed:
                self.t = None
                return
            try:
                start_s = float(item.get("start_s", 0.0) or 0.0)
            except Exception:
                start_s = 0.0
            target = t0 + start_s
            # 循环等到该词真实开始时刻再推送；20ms 粒度睡眠省 CPU 且能
            # 及时响应打断（不能只睡 20ms 就穿透——那会把整句瞬间推完）
            while time.time() < target:
                if self._closed:
                    self.t = None
                    return
                delta = target - time.time()
                if delta > 0:
                    time.sleep(min(0.02, delta))
            # 该词开始时刻已到（或播放领先）：推进并显示累积文本
            i_end = item.get("orig_idx_end")
            if isinstance(i_end, int) and 0 <= i_end < len(text):
                new_idx = i_end + 1
            else:
                new_idx = idx + len(str(item.get("text", "")))
            if new_idx > idx:
                idx = new_idx
                try:
                    self._sink(text[:idx])
                except Exception:
                    pass
        self._last_idx[sent_id] = idx

    def add_clip(self, text: str, subtitles: list, sent_id: int) -> None:
        """添加整句字幕（emit 整段路径）：内部等价于该句唯一一块增量。

        缺少时间戳的句子不输出字幕（宁缺毋假），绝不按时长估算。
        """
        self.add_blocks(text, subtitles, sent_id, first=True)

    def add_blocks(self, text: str, subtitles: list,
                   sent_id: int, first: bool) -> None:
        """添加一块字幕增量（流式按块到达路径，官方 subtitlesqueue.add 对齐）。

        同一句的后续块 first=False，时间戳为整句基准，跨块继续推进。
        """
        if not text or not subtitles:
            return
        self.q.put((text, list(subtitles), sent_id, first))
        if self.t is None or not self.t.is_alive():
            self._closed = False
            self.t = threading.Thread(target=self.process, daemon=True)
            self.t.start()

    def anchor(self, sent_id: int, latency: float = 0.0) -> None:
        """播放线程在句首块真正写入设备前调用：记录该句开播墙钟时刻。

        latency：声卡输出缓冲延迟（write 到实际出声的间隔，秒）。字幕
        时间轴锚定到「实际出声时刻」= 写入时刻 + latency，保证字幕与
        发音逐字同步（念到哪个字显示哪个字），避免字幕整体偏早。
        """
        with self._anchors_lock:
            self._anchors[sent_id] = time.time() + max(latency, 0.0)

    def clear(self) -> None:
        """打断：丢弃未播字幕并结束当前字幕线程（下一次 add 自动重建）。"""
        self._closed = True
        with self._anchors_lock:
            self._anchors.clear()
        self._last_idx.clear()
        with self.q.mutex:
            self.q.queue.clear()
        self.q.put(None)  # 终止信号：process 收到后退出


class TTSPlayer:
    """播放编排层（合成无关）：拆块无缝播放 + 口型回调 + 字幕锚点 + 打断。

    TTSEngine 与 TTSClient 各自把「合成得到的音频 + 字幕时间戳」交给
    emit()，本类统一负责拆块入队、真实开播时刻锚定、口型曲线回调与
    打断/排空语义，保证两条合成链路播放行为完全一致。
    """

    def __init__(self) -> None:
        self._queue: Optional[_AudioQueue] = None  # 无缝播放队列（按首个音频采样率懒创建）
        self._on_play: Optional[Callable[[str, str, float], None]] = None
        self._on_play_done: Optional[Callable[[], None]] = None
        self._done_timer: Optional[threading.Timer] = None  # 说话结束防抖定时器
        self._done_lock = threading.Lock()
        self._sub_sink: Optional[Callable[[str], None]] = None
        self._sub_q: Optional[_SubtitlesQueue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._gen = 0                        # 播放代次号：interrupt 递增，拆块循环检测后提前退出
        self._sub_mute = False               # 打断后抑制字幕线程的残留推送
        self._play_metas: "queue.Queue[Tuple[str, str, float, int, int]]" = queue.Queue()
        self._sent_seq = itertools.count(1)  # 句子序号：字幕锚点/游标按句隔离
        # 每句 emit 入队的墙钟时刻（句首块真正写入设备时取差，诊断出声等待）
        self._sent_emit_ts: dict = {}
        # 流式句上下文（begin_stream 建立，feed/end 消费）：整句合成改为
        # 增量播放时，音频块按到达节奏入队，首块到达即出声（无需等整句）。
        self._stream_ctx: Optional[dict] = None
        self._tmp_dir = tempfile.mkdtemp(prefix="vtuber_tts_")

    # ---------- 对外回调 / 事件循环 ----------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置主事件循环（字幕线程跨线程转发用，Qt 线程安全）。"""
        self._loop = loop

    def set_on_play_callback(self, cb: Optional[Callable[[str, str, float], None]]) -> None:
        """设置音频开始播放回调（wav 路径, 文本, 时长秒）——口型同步用。"""
        self._on_play = cb

    def set_on_play_done_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """设置整段播放结束回调（队列排空/打断清空后触发）——说话结束复原用。"""
        self._on_play_done = cb

    def set_subtitle_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        """设置字幕回调：逐字推进时调用（入参为累积文本）。"""
        self._sub_sink = cb

    # ---------- 播放 ----------

    def _ensure_queue(self, sr: int) -> None:
        """按实际采样率懒创建播放队列（GSV-TTS-Lite 固定 32000）。

        首个音频块到达时创建并挂载常驻播放线程；之后所有块共用同一输出流
        无缝播放。
        """
        if self._queue is not None:
            return
        self._queue = _AudioQueue(sr, self._on_block_play, self._schedule_play_done)

    def _on_block_play(self) -> None:
        """每块音频写入声卡前调用：字幕锚点 + 口型回调 + 临时 wav 即时删除。

        播放元信息（_play_metas）在入队前先于音频写入，按块顺序严格对齐。
        句首块（chunk_idx==0）在此记录真实开播墙钟时刻（补偿声卡输出缓冲
        延迟），字幕线程据此把逐字时间轴锚定到「实际出声时刻」，保证念到
        哪个字显示哪个字。
        """
        meta = None
        if not self._play_metas.empty():
            try:
                meta = self._play_metas.get_nowait()
            except Exception:
                meta = None
        if meta is None:
            return
        wav, _text, dur_s, sent_id, chunk_idx = meta
        if chunk_idx == 0:
            # 诊断：emit 入队 → 句首块真正开播的等待（合成完但声音晚 = 这里大）
            emit_ts = self._sent_emit_ts.pop(sent_id, None)
            if emit_ts is not None:
                wait_ms = (time.perf_counter() - emit_ts) * 1000
                console.dim(f"[TTS] 句{sent_id} 入队→开播等待 {wait_ms:.0f}ms")
            try:
                latency = 0.0
                if self._queue is not None and self._queue.stream is not None:
                    latency = float(
                        getattr(self._queue.stream, "latency", 0.0) or 0.0)
                self._sub_q.anchor(sent_id, latency)
            except Exception:
                pass
        try:
            self._fire_play(wav, "", dur_s)
        finally:
            if wav:
                try:
                    os.remove(wav)  # 曲线已同步加载完毕，wav 即时删除
                except Exception:
                    pass

    def _fire_play(self, wav: str, text: str, dur_s: float) -> None:
        """音频真正开始播放时触发回调（口型曲线）。"""
        cb = self._on_play
        if cb is None:
            return
        try:
            cb(wav, text, dur_s)
        except Exception as e:
            console.dim(f"TTS 口型回调异常：{e}")

    def _fire_play_done(self) -> None:
        """整段播放结束（队列排空/打断清空）时触发回调（播放线程/防抖定时器调用）。"""
        cb = self._on_play_done
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            console.dim(f"TTS 播放结束回调异常：{e}")

    def _schedule_play_done(self) -> None:
        """队列排空（可能是句间空隙）→ 防抖定时器：防抖期内仍无新音频才触发复原。

        多句连播时句间合成间隙（< _DONE_DEBOUNCE）内新音频入队会取消本定时器，
        避免表情/动作在句间闪烁；只有真正长时间无后续音频才视为说话结束。
        """
        with self._done_lock:
            if self._done_timer is not None:
                self._done_timer.cancel()
            self._done_timer = threading.Timer(_DONE_DEBOUNCE, self._fire_play_done)
            self._done_timer.daemon = True
            self._done_timer.start()

    def _cancel_play_done(self) -> None:
        """取消待触发的说话结束复原定时器（新音频入队时调用）。"""
        with self._done_lock:
            if self._done_timer is not None:
                self._done_timer.cancel()
                self._done_timer = None

    def _push_subtitle(self, text: str) -> None:
        """字幕线程 → 事件循环线程（Qt 线程）转发，保证 GUI 安全。"""
        if (not text or self._sub_sink is None or self._loop is None
                or self._sub_mute):
            return
        try:
            self._loop.call_soon_threadsafe(self._sub_sink, text)
        except Exception:
            pass

    def emit(self, audio: np.ndarray, sr: int, text: str,
             subtitles: list, gen: int) -> None:
        """把一段合成音频拆块入队无缝播放 + 口型临时 wav + 词级时间戳字幕。

        audio：1D float32；sr：采样率；text：本段文本；subtitles：词级
        时间戳列表（相对音频起点）；gen：合成侧代次快照（打断后递增，
        拆块循环据此提前停止入队）。
        """
        try:
            self._ensure_queue(sr)
        except Exception as e:
            console.dim(f"TTS 播放队列初始化失败（口型/字幕可能不可用）：{e}")
            return
        # 新音频到达：取消待触发的说话结束复原定时器（多句连播防抖）
        self._cancel_play_done()
        if self._sub_q is None:
            self._sub_q = _SubtitlesQueue(self._push_subtitle)
        sent_id = next(self._sent_seq)
        self._sent_emit_ts[sent_id] = time.perf_counter()  # 入队时刻（诊断用）
        frames_per_chunk = max(1, int(_PLAY_CHUNK_SEC * sr))
        chunk_idx = 0
        for start in range(0, len(audio), frames_per_chunk):
            if not self._emit_block(
                    audio[start:start + frames_per_chunk], sr,
                    sent_id, chunk_idx, gen):
                return  # 打断：放弃剩余音频
            chunk_idx += 1
        # 字幕：GSV-TTS-Lite 返回的真实词级时间戳，锚定播放时刻逐词推进
        if self._sub_q is not None:
            try:
                self._sub_q.add_clip(text, list(subtitles or []), sent_id)
            except Exception:
                pass

    def _emit_block(self, chunk: np.ndarray, sr: int, sent_id: int,
                    chunk_idx: int, gen: int) -> bool:
        """把单个音频块入队（口型临时 wav + 元信息 + 音频），供 emit 与流式共用。

        返回 False 表示代次已变（打断）或入队失败，调用方应停止。
        """
        if gen != self._gen:
            return False
        chunk_dur = len(chunk) / sr
        wav_path = ""
        try:
            if self._on_play is not None:
                fd, wav_path = tempfile.mkstemp(
                    suffix=".wav", prefix="vtuber_tts_", dir=self._tmp_dir)
                os.close(fd)
                sf.write(wav_path, chunk, sr)
        except Exception as e:
            console.dim(f"TTS 写临时音频失败：{e}")
            wav_path = ""
        self._play_metas.put((wav_path, "", chunk_dur, sent_id, chunk_idx))
        try:
            # 无缝播放：同一 OutputStream 逐块 write，块间由设备缓冲衔接
            self._queue.put(chunk)
        except Exception as e:
            console.error(f"TTS 播放失败：{e}")
            return False
        return True

    # ---------- 流式播放（Token 级流式，首块到达即出声） ----------

    def begin_stream(self, sr: int, text: str, gen: int) -> Optional[int]:
        """开始一句流式音频的增量播放，返回 sent_id（播放器不可用/打断返回 None）。

        与 emit() 同款初始化（播放队列/防抖/字幕句柄/入队时刻），但不拆整句：
        后续 feed_stream() 按到达节奏把音频增量入队，句首块到达即开播，
        首字延迟只受「首块到达」限制，不再等整句合成完。词级时间戳字幕由
        feed_subtitles() 按块增量喂入（官方示例逐块 add 的 1:1 语义），
        句首块起即与播放逐词同步。
        """
        if gen != self._gen:
            return None
        try:
            self._ensure_queue(sr)
        except Exception as e:
            console.dim(f"TTS 播放队列初始化失败（口型/字幕可能不可用）：{e}")
            return None
        # 新音频到达：取消待触发的说话结束复原定时器（多句连播防抖）
        self._cancel_play_done()
        if self._sub_q is None:
            self._sub_q = _SubtitlesQueue(self._push_subtitle)
        sent_id = next(self._sent_seq)
        self._sent_emit_ts[sent_id] = time.perf_counter()  # 入队时刻（诊断用）
        self._stream_ctx = {
            "sent_id": sent_id, "gen": gen, "sr": sr, "text": text,
            "chunk_idx": 0, "sub_first": True,
            "buffer": np.zeros(0, dtype=np.float32),
        }
        return sent_id

    def feed_stream(self, pcm: bytes, gen: int) -> bool:
        """喂入一块流式音频（int16 小端 PCM 字节），块到即播（边到边）。

        服务端流式块（GPT 按 stream_chunk 产出，块间已 SOLA 重叠混音）
        到达后直接整块入队播放，不再按 0.5s 攒块——旧攒块逻辑在服务端块
        小于 0.5s（如 stream_chunk=12 → 每块 ~0.22s）时，先播完首块再等
        攒够下一块，产生句内空窗"顿挫"。块到即播时块间由设备缓冲连续
        衔接，首个 HTTP 块到达即出声，首字延迟只受「首块到达」限制。

        返回 False 表示代次已变（打断）或入队失败，调用方应停止喂入。
        """
        ctx = self._stream_ctx
        if ctx is None or gen != self._gen:
            return False
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            return True
        if not self._emit_block(audio, ctx["sr"], ctx["sent_id"],
                                ctx["chunk_idx"], gen):
            return False
        ctx["chunk_idx"] += 1
        return True

    def feed_subtitles(self, subtitles: list, gen: int) -> None:
        """喂入一块字幕增量（官方示例 subtitlesqueue.add 的 1:1 语义）。

        流式块随带的词级时间戳增量（相对整句起点）按块入队，字幕队列
        锚定句首块真实开播时刻逐词推进——句首块起即与播放同步，无需等
        整句合成完。
        """
        ctx = self._stream_ctx
        if ctx is None or gen != self._gen or self._sub_q is None:
            return
        if not subtitles:
            return
        try:
            self._sub_q.add_blocks(ctx["text"], list(subtitles),
                                   ctx["sent_id"], ctx["sub_first"])
        except Exception:
            pass
        ctx["sub_first"] = False

    def end_stream(self, gen: int) -> None:
        """流式句收尾：冲刷残余块并结束增量会话（字幕已逐块入队）。"""
        ctx = self._stream_ctx
        self._stream_ctx = None
        if ctx is None or gen != self._gen:
            return
        if ctx["buffer"].size:
            self._emit_block(ctx["buffer"], ctx["sr"], ctx["sent_id"],
                             ctx["chunk_idx"], gen)

    def abort_stream(self) -> None:
        """放弃当前流式句：丢弃未入队缓冲；已入队块照常播完。

        退化/失败回退由调用方决定（重新批量合成），本方法只终止增量会话。
        """
        self._stream_ctx = None

    # ---------- 打断 / 排空 / 关闭 ----------

    def interrupt(self) -> None:
        """立即闭嘴：停播 + 丢弃未播字幕（由调用方决定是否同时停合成）。"""
        self._gen += 1
        self._sub_mute = True
        if self._queue is not None:
            try:
                self._queue.clear()
            except Exception:
                pass
        # 丢弃未播块元信息时顺带删除其临时 wav——否则被打断的块文件
        # 永久残留（_on_block_play 只删「已播放」块的 wav）
        while not self._play_metas.empty():
            try:
                meta = self._play_metas.get_nowait()
            except Exception:
                break
            wav = meta[0]
            if wav:
                try:
                    os.remove(wav)
                except Exception:
                    pass
        self._sent_emit_ts.clear()  # 打断：作废未开播句的入队时刻
        self._stream_ctx = None     # 打断：放弃流式句剩余喂入
        if self._sub_q is not None:
            try:
                self._sub_q.clear()
            except Exception:
                pass

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志。"""
        self._sub_mute = False

    async def drain(self) -> None:
        """等待全部音频播完（playback_finished 在队列空 0.25s 后置位）。"""
        aq = self._queue
        if aq is not None:
            for _ in range(6000):  # 最长等 300s
                if aq.playback_finished.is_set():
                    break
                await asyncio.sleep(0.05)
        # 字幕队列与播放基本同步，给残留尾字推送一点收尾时间
        await asyncio.sleep(0.3)

    def close(self) -> None:
        """停止播放线程、关闭输出流并清理临时目录（进程退出前调用）。"""
        self._cancel_play_done()
        self._stream_ctx = None
        if self._sub_q is not None:
            try:
                self._sub_q.clear()
            except Exception:
                pass
            self._sub_q = None
        if self._queue is not None:
            try:
                self._queue.close()
            except Exception:
                pass
            self._queue = None
        # 清理临时目录（正常路径下各 wav 已即时删除，这里兜底）
        try:
            if self._tmp_dir and os.path.isdir(self._tmp_dir):
                for f in os.listdir(self._tmp_dir):
                    try:
                        os.remove(os.path.join(self._tmp_dir, f))
                    except Exception:
                        pass
                os.rmdir(self._tmp_dir)
        except Exception:
            pass
        self._tmp_dir = None
