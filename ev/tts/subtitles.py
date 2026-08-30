"""字级时间戳字幕队列（整段入队 / 后台线程节拍播出）。

参考 GSV-TTS-Lite README §2 「流式推理 / 字幕同步」示例实现。

2026-08-30 显示修复（字幕滞后 / 整句替换渲染不匹配 / 落定条目丢字）：

1. **时间轴锚点按句延续（对齐参考实现）**：GSV ``infer_stream`` 每个音频块
   产出的 ``start_s`` / ``end_s`` 是**相对整句合成起点**的累计时间戳（跨块
   连续，SOLA 重叠与头部静音已在源内修剪）。参考示例在 worker 启动时把
   ``last_t = time.time()`` 置一次、所有批次共用；此前本实现每批重置锚点，
   把累计时间戳当块内相对时间 → 第 N 块要「重新睡完整段累计时长」→ 字幕
   比语音越来越滞后。现改为：**同一句（同一 ``text`` 对象）的所有批次共用
   首批处理时刻为 0 点**（首批在 ``chunk.play()`` 后立即入队，即本句音频
   播放起点）；新句（text 对象身份变化）重置游标与锚点。

2. **回推累积整句而非增量碎片**：消费者（字幕网页 SSE ``sub.textContent =
   text``、SubtitleServer 语义「sent = 新显示的整句」）都是**整句替换**
   渲染；此前回推 ``text[last_i:orig_idx_end]`` 增量碎片，网页整行被碎片
   反复替换，永远拼不成完整句子。现回推 ``text[:last_i]``（当前应显示的
   整句），网页端即为打字机逐字效果。

3. **落后时补显、不丢字**：流式中段每批末条字幕 ``end_s=None``（结束时刻
   未定），留待下一块以同一 ``orig_idx_end`` 补发落定。若处理时刻已越过
   该条窗口（合成慢于播放 / 线程停顿），旧逻辑 ``end_s > elapsed`` 判定
   会把补发条目整条跳过 → **该段文字永远不上屏**。现改为：只要字已开口
   （``start_s`` 已到，含已过）且 ``orig_idx_end`` 有新增，立即补显累积
   文本；``end_s`` 只用作节拍（领先于语音才睡），不再作为显示门槛。

4. **播放进度时钟（2026-08-30 抢跑修复）**：GSV AudioQueue 的播放是
   「声卡实时消费 + 合成间隙空转」，纯音频时间戳 + 墙钟对齐会在合成慢于
   实时时把空档也走完 → 字幕比语音抢跑。``add`` 现接受 ``clock``（引擎
   传入的播放进度时钟，返回本句已播放秒数，见
   ``ev/tts/engine/core.py::_PlaybackWriteClock``）：条目等进度到位才上屏，
   合成间隙时钟冻结 → 字幕原地等待，从机制上消除抢跑；时钟不可用时回退
   墙钟对齐（上一版行为）。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional, Tuple


class SubtitlesQueue:
    """字级时间戳字幕串行播出。

    核心字段::

        self.q          # 入队 (subtitles, text, clock) 元组；None 终止
        self.t          # 后台处理线程（懒启动 / 终止后清零）
        self._cur_text  # 当前游标归属句（text str 对象身份）
        self._cur_i     # 当前句已回推到的 ``orig_idx_end``（跨批次延续）
        self._last_t    # 本句墙钟锚点（clock 回退用，跨批次延续）
        self._cur_clock # 本句播放进度时钟（引擎传入，跨批次延续）

    一段 LLM final 句子切片合成出 ``n`` 个 AudioClip，每个 clip 自带一段
    增量字级时间戳（``AudioClip.subtitles``，时间值相对**整句**起点）。
    ``add(subtitles, text, clock)`` 把子轨迹丢进队列即可；后台线程按播放
    进度（或墙钟回退）睡眠 / 回推，与真实音频播放同步。控制台不在此打印：
    字幕消费者是 ``subtitle_cb`` 回调（→ 网页字幕 SSE），终端回复显示由
    converse() 的 delta 打字机负责。
    """

    # 播放进度时钟停滞兜底（秒）：设备异常导致进度永不前进时放弃等待，
    # 立即回推剩余字幕（正常合成间隙仅 1~3s，远小于此值）
    _CLOCK_STALL_TIMEOUT = 15.0

    def __init__(self, subtitle_cb: Optional[Callable[[str], None]] = None) -> None:
        self.q: "queue.Queue[Tuple[Optional[List[dict]], str, Optional[Callable[[], float]]]]" = queue.Queue()
        self.t: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subtitle_cb = subtitle_cb
        # 句级游标 / 锚点 / 播放时钟（跨批次延续，见模块 docstring）
        self._cur_text: Optional[str] = None
        self._cur_i = 0
        self._last_t: float = 0.0
        self._cur_clock: Optional[Callable[[], float]] = None

    # --------------------------- 后台线程 ---------------------------

    def process(self) -> None:
        """常驻消费字幕批次：按句级时间轴对齐睡眠，回推累积整句。

        同一句（同一 ``text`` 对象）的后续块**延续游标与时间轴锚点**——
        时间戳本就是相对整句起点的累计值；补发条目（``end_s`` 由 None
        落定为实际值）与常规条目统一处理，落后时立即补显、不丢字。
        新句（text 对象身份变化）重置游标与锚点。
        flush() 置位 stop 后：睡眠立即打断，且**放弃本批剩余条目**
        （被打断句子的残留碎片不得在 clear 之后迟至上屏）。
        """
        try:
            while not self._stop.is_set():
                try:
                    subtitles, text, clock = self.q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if subtitles is None:
                    break

                if text is not self._cur_text:
                    # 新句：重置游标；墙钟锚点 = 本句首块处理时刻；
                    # 播放进度时钟换成本句的（0 点 = 本句首样本被消费）
                    self._cur_text = text
                    self._cur_i = 0
                    self._last_t = time.time()
                    self._cur_clock = clock
                elif clock is not None:
                    self._cur_clock = clock
                clock = self._cur_clock
                last_i = self._cur_i
                last_t = self._last_t

                for subtitle in subtitles:
                    if self._stop.is_set():
                        break
                    start_s = subtitle.get("start_s") or 0.0
                    end_s = subtitle.get("end_s")
                    idx_end = subtitle.get("orig_idx_end") or 0

                    if idx_end <= last_i:
                        continue  # 补发重复条目：文字已上屏，跳过

                    # 逐字上屏（说哪个字显示哪个字）：GSV 每条字幕是一个词，
                    # 词内按字均分 [start_s, end_s] 时间——第 m 个字在它开口
                    # 的瞬间上屏（前缀长到它）。
                    # 末条 end_s=None（词的音频跨块未完、结束时刻未知）→
                    # 本批整条跳过，等下一块补发落定后逐字上屏——提前显示
                    # 尚未合成的字同样违背「说哪显示哪」。
                    # 不做词尾 end 等待：下一个字的 start 等待即是节拍，
                    # 且 clock 模式下 end 可能越过时钟上限（设备延迟补偿）
                    # 造成死等。
                    n = idx_end - last_i
                    if end_s is None:
                        continue
                    step = (end_s - start_s) / n if n > 0 else 0.0
                    for m in range(1, n + 1):
                        self._wait_until(start_s + (m - 1) * step,
                                         clock, last_t)
                        if self._stop.is_set():
                            break
                        last_i += 1
                        self._cur_i = last_i
                        self._emit(text[:last_i])
        finally:
            self.t = None

    def _wait_until(self, target: float, clock, last_t: float) -> None:
        """等待到时间轴 target 秒：clock 模式等播放进度，回退模式等墙钟。"""
        if clock is not None:
            self._wait_progress(clock, target)
        else:
            rel = time.time() - last_t
            if target > rel:
                self._sleep_interruptible(target - rel)

    def _wait_progress(self, clock: Callable[[], float], target: float) -> None:
        """等待播放进度**越过** target 秒（50ms 轮询，stop 可打断）。

        严格大于（非 >=）：合成间隙期进度恰好停在边界值上——边界处的字
        要等语音恢复才真正出声，>= 会在静音期提前上屏；且句首进度为 0
        时，>= 会让首字在音频尚未流动时就上屏（首字抢跑源头）。
        进度落后（合成间隙 / 队列尾音未播完）→ 字幕原地等待，绝不抢跑；
        进度已越过（迟到批次）→ 立即返回，补显不丢字。进度停滞超过
        _CLOCK_STALL_TIMEOUT（设备异常）→ 放弃等待立即回推。
        """
        last_v = clock()
        last_advance = time.time()
        while not self._stop.is_set():
            v = clock()
            if v > target:
                return
            now = time.time()
            if v > last_v + 1e-6:
                last_v, last_advance = v, now
            elif now - last_advance > self._CLOCK_STALL_TIMEOUT:
                return  # 播放时钟停滞（设备异常兜底）：不再等待
            time.sleep(0.05)

    def _sleep_interruptible(self, seconds: float) -> None:
        """小步长 sleep（50ms），被打断能立即醒。"""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(min(0.05, deadline - time.time()))

    def _emit(self, chunk: str) -> None:
        """把当前应显示的累积整句回推给回调（网页字幕 SSE 等）。

        注意：不向控制台打印。回复文本由 converse() 的 delta 打字机实时
        显示（带 CHAT_TAG，控制中心路由到「对话」栏）；这里裸 print 会把
        无标记、无换行的碎片混进「工具日志」栏，且流式分块下会重复前缀。
        """
        if not chunk or self._stop.is_set():
            return
        cb = self._subtitle_cb
        if cb is not None:
            try:
                cb(chunk)
            except Exception:
                # 字幕回推失败不影响主链路
                pass

    def push_text(self, text: str) -> None:
        """不经时间戳对齐，立即回推一段文字（跳句告警等系统提示用）。"""
        self._emit(text)

    # --------------------------- 入队 ---------------------------

    def add(self, subtitles: Optional[List[dict]], text: str,
            clock: Optional[Callable[[], float]] = None) -> None:
        """把一段 segment 的字幕列表和原文一起入队，自动维护 worker。

        clock：本句播放进度时钟（返回本句已播放秒数，引擎侧
        ``_PlaybackWriteClock.sentence_clock()`` 提供）；None → 墙钟对齐。
        同句所有批次须传同一个 clock（同一闭包，时间轴一致）。
        """
        if self._stop.is_set():
            return
        self.q.put((subtitles, text, clock))
        if self.t is None or not self.t.is_alive():
            self.t = threading.Thread(target=self.process, daemon=True,
                                       name="SubtitlesQueue")
            self.t.start()

    def flush(self) -> None:
        """打断：清空尚未开播的字幕段，立即结束 worker。

        调用时机：用户输入 / 主动对话切换 / 异常退出。把队列里没播完的旧段
        全部丢弃，然后等 worker 干净退出。游标与时间轴锚点一并作废
        （被打断句子的剩余字幕不再延续旧进度）。
        """
        self._stop.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        # 用局部引用等待当前线程自然结束（worker 退出时会把 self.t 置 None）
        t = self.t
        if t is not None and t.is_alive():
            t.join(timeout=0.2)
        self._stop.clear()
        self._cur_text = None
        self._cur_i = 0
        self._last_t = 0.0

    def stop(self) -> None:
        """彻底关闭：终止 worker，终止后不再接收 add。"""
        self.flush()
        self.q.put((None, "", None))
        t = self.t
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self.t = None

    # --------------------------- 配置 ---------------------------

    def set_subtitle_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        self._subtitle_cb = cb


__all__ = ["SubtitlesQueue"]
