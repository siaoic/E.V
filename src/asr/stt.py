"""语音识别（STT）：麦克风录音 → 转写（本地 ASR 服务 / SiliconFlow 云端）。

流程：sounddevice 采集 16kHz 单声道 → WebRTC VAD 语音活动检测（缺失时回退
能量阈值）静音分割（开口开始 / 静音结束 / 超时强制切段）→ 写入临时 WAV
→ 转写（STT_ENGINE=local 走本地 ASR 服务 src/asr/asr_server.py，由 asr.bat
启动、监听 127.0.0.1:8487；默认 cloud 走 POST /v1/audio/transcriptions
multipart：file + model=FunAudioLLM/SenseVoiceSmall，Bearer 认证）
→ 识别文本经 asyncio future 投递给主循环，与键盘输入并存（谁先到谁生效）。

依赖：sounddevice + numpy（录音），本地转写需 requests（HTTP 调用 ASR 服务），
云端转写需 requests，WebRTC VAD 需 webrtcvad（缺失时回退能量阈值判定）。
任一缺失时启动引擎会报错并优雅降级——主程序捕获后仅告警，不影响对话。
"""

import asyncio
import os
import queue
import tempfile
import threading
import wave

import numpy as np

from src.utils import console
from src.adapter.input import BaseInputAdapter

# 录音参数（与 STT_LEVEL_THRESHOLD 等 .env 配置区分：这些是硬件级常量）
SAMPLE_RATE = 16000          # SiliconFlow/SenseVoice 标准采样率
CHANNELS = 1
DTYPE = "int16"
BLOCK_SECONDS = 0.1          # 每块 100ms（VAD 状态机的最小粒度）
_MIN_SPEECH_SECONDS = 0.3    # 最短有效语音：低于此长度视为环境杂音，不提交
_EMPTY_RMS = 1.0             # 转写前忽略的能量下限（防纯静音段上传）
# WebRTC VAD 帧参数：20ms 帧（16000Hz 下 WebRTC 最稳粒度），每块 5 帧
_VAD_FRAME_SECONDS = 0.02
_VAD_FRAME_LEN = int(SAMPLE_RATE * _VAD_FRAME_SECONDS)  # 320 样本/帧
_VAD_FRAME_BYTES = _VAD_FRAME_LEN * 2                   # int16 = 2 字节/样本
_VAD_SPEECH_RATIO = 0.4      # 块内语音帧占比 ≥ 40% 判为有声块


def _rms(audio: np.ndarray) -> float:
    """整段音频的均方根能量（float32→float64 防溢出）。"""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


class STTEngine(BaseInputAdapter):
    """麦克风监听 + 语音识别引擎。

    线程模型：
    - 采集线程：sounddevice InputStream 回调只把块塞进队列（轻量，不阻塞音频回调）
    - 处理线程：VAD 状态机消费块，切出语音段 → 写临时 WAV
    - 转写线程：每段语音独立线程上传转写（不阻塞 VAD，可连续说话）
    - 交付：转写文本 + 说话时长经 `loop.call_soon_threadsafe` 设置 asyncio future
      （future 结果为 `(文本, 说话时长秒)` 元组），主循环 `_wait_input` /
      `_interruptible_converse` 用 `asyncio.wait` 同时等待 stdin 与识别结果。
    """

    def __init__(self, cfg) -> None:
        # 转写引擎：local（本地 ASR 服务 asr.bat，8487 端口）/ cloud（SiliconFlow 云端）
        self.engine = str(getattr(cfg, "STT_ENGINE", None) or "cloud").lower()
        # 本地 ASR 服务地址（src/asr/asr_server.py 独立进程）
        self.server_url = (
            getattr(cfg, "STT_SERVER_URL", None)
            or "http://127.0.0.1:8487").rstrip("/")
        # 独立 STT key 优先，留空回退复用 SiliconFlow 主 key（与嵌入同源）
        self.api_key = (
            getattr(cfg, "STT_API_KEY", None) or cfg.SILICONFLOW_API_KEY or ""
        )
        self.base_url = (
            getattr(cfg, "STT_BASE_URL", None) or cfg.SILICONFLOW_BASE_URL
            or "https://api.siliconflow.cn/v1").rstrip("/")
        self.model = getattr(cfg, "STT_MODEL", None) or "FunAudioLLM/SenseVoiceSmall"
        self.level_threshold = float(
            getattr(cfg, "STT_LEVEL_THRESHOLD", None) or 500)
        self.vad_mode = int(getattr(cfg, "STT_VAD_MODE", None) or 2)
        self.silence_seconds = float(
            getattr(cfg, "STT_SILENCE_SECONDS", None) or 0.6)
        self.max_seconds = float(
            getattr(cfg, "STT_MAX_SECONDS", None) or 10.0)
        # WebRTC VAD：区分语音/噪声比纯能量阈值更准，可安全缩短静音等待
        # （STT_SILENCE_SECONDS）以降低识别延迟；依赖缺失时回退能量判定
        self._vad = None
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(self.vad_mode)
        except Exception as e:
            console.warn(f"[STT] WebRTC VAD 不可用，回退能量阈值判定：{e}")

        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop_event = threading.Event()
        self._blocks: "queue.Queue[np.ndarray]" = queue.Queue()
        # 识别结果交付：future 队列 + 无等待者时的文本缓冲
        self._futures: "list[asyncio.Future]" = []
        self._pending_texts: "list[str]" = []
        self._listener: "threading.Thread | None" = None
        self._processor: "threading.Thread | None" = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """在 asyncio 主循环内调用：记录 loop，启动采集/处理线程。"""
        self._loop = asyncio.get_running_loop()
        if self._listener is not None and self._listener.is_alive():
            return
        self._stop_event.clear()
        self._listener = threading.Thread(
            target=self._listen_loop, name="stt-listener", daemon=True)
        self._listener.start()
        self._processor = threading.Thread(
            target=self._process_loop, name="stt-processor", daemon=True)
        self._processor.start()

    def stop(self) -> None:
        """停止监听/处理线程（转写中的线程自然结束）。"""
        self._stop_event.set()
        for t in (self._listener, self._processor):
            if t is not None:
                t.join(timeout=2)

    # ---------- 结果交付（供主循环 asyncio.wait） ----------

    def result_future(self) -> "asyncio.Future":
        """返回下一个识别结果的 future，结果为 `(文本, 说话时长秒)` 元组。

        多次调用返回同一 future（首个未完成者），消费后下一次调用拿到新的。
        先清掉已取消的残留 future（对话结束 / 被打断时会取消未消费的识别
        future，不清理会让下一次等输入拿到已取消的 future，result() 抛
        CancelledError 打崩主循环——表现为语音对话一结束程序就退出）。
        """
        while self._futures and self._futures[0].done():
            self._futures.pop(0)
        if self._futures:
            return self._futures[0]
        assert self._loop is not None, "STTEngine.start() 必须在 asyncio 主循环内调用"
        fut = self._loop.create_future()
        if self._pending_texts:
            fut.set_result(self._pending_texts.pop(0))
        self._futures.append(fut)
        return fut

    def _deliver(self, text: str, speech_seconds: float) -> None:
        """主循环线程内：把识别文本+说话时长交付给等待中的 future（FIFO）。"""
        text = (text or "").strip()
        if not text:
            return
        # 跳过已取消的残留 future（被打断 / 对话结束取消的），把文本交给
        # 下一个等待者；没有可用等待者时进缓冲，下次等输入时立刻拿到
        while self._futures:
            fut = self._futures.pop(0)
            if not fut.done():
                fut.set_result((text, speech_seconds))
                return
        self._pending_texts.append((text, speech_seconds))

    # ---------- 采集线程 ----------

    def _listen_loop(self) -> None:
        try:
            import sounddevice as sd
        except Exception as e:
            console.error(f"[STT] 无法导入 sounddevice，录音不可用：{e}")
            return
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=int(SAMPLE_RATE * BLOCK_SECONDS),
                callback=lambda indata, _frames, _time, _status: self._blocks.put(
                    indata.copy()),
            ):
                # InputStream 打开成功即持续采集；阻塞等待停止信号
                self._stop_event.wait()
        except Exception as e:
            console.error(f"[STT] 麦克风打开失败：{e}")

    # ---------- 处理线程（VAD 状态机） ----------

    def _is_speech_block(self, audio: np.ndarray) -> bool:
        """块级语音判定：WebRTC VAD 为主，能量阈值兜底（VAD 不可用时）。

        当前块 100ms = 5 个 20ms 帧，语音帧占比达 _VAD_SPEECH_RATIO 判为
        有声块。相比纯能量阈值更能区分语音与噪声尾音，从而允许更短的
        静音等待切段（STT_SILENCE_SECONDS），降低识别延迟。
        """
        if self._vad is not None:
            buf = audio.astype(np.int16).tobytes()
            frames = len(audio) // _VAD_FRAME_LEN
            speech_frames = sum(
                self._vad.is_speech(
                    buf[i * _VAD_FRAME_BYTES:(i + 1) * _VAD_FRAME_BYTES],
                    SAMPLE_RATE,
                )
                for i in range(frames)
            )
            return speech_frames >= max(1, int(frames * _VAD_SPEECH_RATIO))
        return _rms(audio) > self.level_threshold

    def _process_loop(self) -> None:
        silence_blocks = max(1, int(self.silence_seconds / BLOCK_SECONDS))
        state = "silent"
        buf: "list[np.ndarray]" = []
        silent_run = 0
        speech_blocks = 0   # 说话（有声）块计数：供打断时长判定（不含结尾静音）

        while not self._stop_event.is_set():
            try:
                block = self._blocks.get(timeout=0.2)
            except queue.Empty:
                # 麦克风没给数据时也推进静音计时（防末尾段永不切出）
                if state == "speaking":
                    silent_run += 1
                    if silent_run >= silence_blocks:
                        self._commit(buf, speech_blocks * BLOCK_SECONDS)
                        buf, state, silent_run, speech_blocks = [], "silent", 0, 0
                continue

            speech = self._is_speech_block(block)
            if state == "silent":
                if speech:
                    state = "speaking"
                    buf = [block]
                    speech_blocks = 1
                    silent_run = 0
            else:
                buf.append(block)
                if speech:
                    speech_blocks += 1
                    silent_run = 0
                else:
                    silent_run += 1
                    if silent_run >= silence_blocks:
                        self._commit(buf, speech_blocks * BLOCK_SECONDS)
                        buf, state, silent_run, speech_blocks = [], "silent", 0, 0
                        continue
                # 超时强制切段（连续说话不停顿也按时长切）
                if len(buf) * BLOCK_SECONDS >= self.max_seconds:
                    self._commit(buf, speech_blocks * BLOCK_SECONDS)
                    buf, state, silent_run, speech_blocks = [], "silent", 0, 0

    def _commit(self, buf: "list[np.ndarray]", speech_seconds: float) -> None:
        """语音段收尾：校验长度/能量 → 写 WAV → 转写线程。"""
        if not buf:
            return
        audio = np.concatenate(buf) if len(buf) > 1 else buf[0]
        if audio.size < int(SAMPLE_RATE * _MIN_SPEECH_SECONDS):
            return
        if _rms(audio) < _EMPTY_RMS:
            return
        path = self._write_wav(audio)
        if path:
            threading.Thread(
                target=self._transcribe_worker, args=(path, speech_seconds),
                daemon=True,
            ).start()

    @staticmethod
    def _write_wav(audio: np.ndarray) -> "str | None":
        """把 int16 音频写入系统临时目录 WAV，返回路径。"""
        try:
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="vtuber_stt_")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)  # int16 = 2 字节
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio.astype(np.int16).tobytes())
            return path
        except Exception as e:
            console.error(f"[STT] 写临时音频失败：{e}")
            return None

    # ---------- 转写 ----------

    def _transcribe_worker(self, wav_path: str, speech_seconds: float) -> None:
        try:
            text = self.transcribe(wav_path)
        except Exception as e:
            console.error(f"[STT] 转写失败：{e}")
            text = ""
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass
        if text and self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver, text, speech_seconds)

    def transcribe(self, wav_path: str) -> str:
        """按引擎配置转写：local 走本地 qwen3_asr 模型，cloud 走 SiliconFlow 云端。"""
        if self.engine == "local":
            return self._transcribe_local(wav_path)
        return self._transcribe_cloud(wav_path)

    def _transcribe_local(self, wav_path: str) -> str:
        """提交本地 ASR 服务（asr_server.py 独立进程，8487 端口）转写。

        服务未启动 / 连接失败时抛异常，由 _transcribe_worker 捕获并告警。
        """
        import requests

        resp = requests.post(
            f"{self.server_url}/transcribe", json={"path": wav_path}, timeout=60)
        resp.raise_for_status()
        return str(resp.json().get("text") or "").strip()

    def _transcribe_cloud(self, wav_path: str) -> str:
        """上传音频到 SiliconFlow 转写，返回识别文本（失败抛异常）。"""
        import requests

        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with open(wav_path, "rb") as f:
            files = {
                "file": (os.path.basename(wav_path), f, "audio/wav"),
                "model": (None, self.model),
            }
            resp = requests.post(url, headers=headers, files=files, timeout=60)
            resp.raise_for_status()
        data = resp.json()
        return str(data.get("text") or "").strip()
