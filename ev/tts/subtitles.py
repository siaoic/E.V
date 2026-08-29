"""字级时间戳字幕队列（整段入队 / 后台线程节拍播出）。

参考 GSV-TTS-Lite README §2 「流式推理 / 字幕同步」，把示例代码
（threading + sleep 对齐）原样照搬并做几处适配：

1. ``start`` 时启动后台线程、``stop`` 时灌入 ``None`` 终止；
2. ``flush`` 提供「到此为止以这一批字幕为最新」，不再累积更早段的剩余字幕；
   用在 LLM 主动对话 / 弹幕切换的「打断-切段」场景，避免上句尾部延迟占用
   字幕面板；
3. ``subtitle_cb`` 回调（兼容 TTS 适配层 ``set_subtitle_callback``），
   每次打印即回推一行字面，外部可对接 SSE / 直播弹幕气泡。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional, Tuple


class SubtitlesQueue:
    """字级时间戳字幕串行播出。

    核心字段::

        self.q          # 入队 (subtitles, text) 元组；None 终止
        self.t          # 后台处理线程（懒启动 / 终止后清零）
        self.last_i     # 本段已打印到的 ``orig_idx_end``
        self.last_t     # 本段起点墙钟（秒），用来计算 start/end_s 的相对偏移

    一段 LLM final 句子切片合成出 ``n`` 个 ``AudioClip``，每个 clip 自带一段
    字级时间戳（``AudioClip.subtitles``）。``add(subtitles, text)`` 把整段
    子轨迹丢进队列即可；后台线程 sleep / 推进 / ``print`` 的调度和真实音频
    播放相互独立（音频在 ``AsyncAudioPlayer`` 自己的播放线程里），两者共享
    同一段 ``last_t`` 起点，体感同步。
    """

    def __init__(self, subtitle_cb: Optional[Callable[[str], None]] = None) -> None:
        self.q: "queue.Queue[Tuple[Optional[List[dict]], str]]" = queue.Queue()
        self.t: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subtitle_cb = subtitle_cb

    # --------------------------- 后台线程 ---------------------------

    def process(self) -> None:
        """逐 segment 弹出字幕，对齐时间戳睡眠，按 orig_idx 切片打印。

        segment 起始记录 ``last_t = time.time()``，后续 ``start_s`` /
        ``end_s`` 都按此为 0 点求 ``time.sleep(start_s - now)``；这样
        迟到 segment 也会从错误的时间点「插队」，但不阻塞下一段。
        """
        while not self._stop.is_set():
            try:
                subtitles, text = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if subtitles is None:
                break

            last_i = 0
            last_t = time.time()

            for subtitle in subtitles:
                # 子标题起点等待
                rel = time.time() - last_t
                if subtitle["start_s"] > rel:
                    self._sleep_interruptible(subtitle["start_s"] - rel)

                # 终点判定：end_s 为 None 表示开放式结尾（流式中段）
                if subtitle.get("end_s") is not None and subtitle["end_s"] > (time.time() - last_t):
                    if subtitle["orig_idx_end"] > last_i:
                        chunk = text[last_i:subtitle["orig_idx_end"]]
                        last_i = subtitle["orig_idx_end"]
                        self._emit(chunk)
                        rel = time.time() - last_t
                        if subtitle["end_s"] > rel:
                            self._sleep_interruptible(subtitle["end_s"] - rel)
            self.t = None
            return  # 任务结束，单段一次跑完

    def _sleep_interruptible(self, seconds: float) -> None:
        """小步长 sleep（50ms），被打断能立即醒。"""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(min(0.05, deadline - time.time()))

    def _emit(self, chunk: str) -> None:
        if not chunk:
            return
        print(chunk, end="", flush=True)
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

    def add(self, subtitles: Optional[List[dict]], text: str) -> None:
        """把一段 segment 的字幕列表和原文一起入队，自动维护 worker。"""
        if self._stop.is_set():
            return
        self.q.put((subtitles, text))
        if self.t is None or not self.t.is_alive():
            self.t = threading.Thread(target=self.process, daemon=True,
                                       name="SubtitlesQueue")
            self.t.start()

    def flush(self) -> None:
        """打断：清空尚未开播的字幕段，立即结束 worker。

        调用时机：用户输入 / 主动对话切换 / 异常退出。把队列里没播完的旧段
        全部丢弃，然后灌一个空批让 process 干净退出。
        """
        self._stop.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        # 等当前线程自然结束（如果还在跑本段，会跑完后 ``self.t = None``）
        if self.t is not None and self.t.is_alive():
            self.t.join(timeout=0.2)
        self._stop.clear()

    def stop(self) -> None:
        """彻底关闭：终止 worker，终止后不再接收 add。"""
        self.flush()
        self.q.put((None, ""))
        if self.t is not None and self.t.is_alive():
            self.t.join(timeout=0.5)
        self.t = None

    # --------------------------- 配置 ---------------------------

    def set_subtitle_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        self._subtitle_cb = cb


__all__ = ["SubtitlesQueue"]
