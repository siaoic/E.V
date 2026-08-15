"""语音识别（STT）：麦克风录音 → 流式转写（本地 FunASR 服务 / SiliconFlow 云端）。

流程：sounddevice 采集 16kHz 单声道 → fsmn-vad 流式语音活动检测（funasr，
200ms 一块喂入，开始/结束事件驱动静音分割：开口开始 / VAD 判定结束 /
超时强制切段）→ 本地引擎在说话过程中每 600ms 把音频块流式提交给 ASR 服务
（src/asr/asr_server.py，由 启动asr.bat 启动、监听 127.0.0.1:8487），服务端
增量推理返回 partial 文本，VAD 判定说话结束立即拿到完整文本——推理在说话
过程中已完成，无需等整句话说完再识别；云端引擎保持整段上传（POST
/v1/audio/transcriptions multipart：file + model=FunAudioLLM/SenseVoiceSmall，
Bearer 认证）
→ 识别文本经 asyncio future 投递给主循环，与键盘输入并存（谁先到谁生效）。

依赖：sounddevice + numpy（录音），转写需 requests，语音检测需 funasr
（fsmn-vad 模型，存放于 src/asr/models，缺失自动从 ModelScope 下载）。
任一缺失时启动引擎会报错并优雅降级——主程序捕获后仅告警，不影响对话。
"""

import asyncio
import os
import queue
import tempfile
import threading
import time
import wave

import numpy as np

from src.utils import console
from src.adapter.input import BaseInputAdapter

# 录音参数（与 STT_SILENCE_SECONDS 等 .env 配置区分：这些是硬件级常量）
SAMPLE_RATE = 16000          # SiliconFlow/SenseVoice 标准采样率
CHANNELS = 1
DTYPE = "int16"
BLOCK_SECONDS = 0.1          # 每块 100ms（sounddevice 采集粒度，仅用于硬件回调）
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_SECONDS)  # 1600 样本
_MIN_SPEECH_SECONDS = 0.5    # 最短有效语音：低于此长度视为环境杂音，不提交
_EMPTY_RMS = 1.0             # 转写前忽略的能量下限（防纯静音段上传）

# =============================================================================
# FunASR 官方 demo 原文（严格按此模式调用，禁止修改调用形态）
# =============================================================================
#
# VAD（fsmn-vad，chunk_size=200 ms）：
# from funasr import AutoModel
#
# chunk_size = 200 # ms
# model = AutoModel(model="fsmn-vad", model_revision="v2.0.4")
#
# import soundfile
#
# wav_file = f"{model.model_path}/example/vad_example.wav"
# speech, sample_rate = soundfile.read(wav_file)
# chunk_stride = int(chunk_size * sample_rate / 1000)
#
# cache = {}
# total_chunk_num = int(len((speech)-1)/chunk_stride+1)
# for i in range(total_chunk_num):
#     speech_chunk = speech[i*chunk_stride:(i+1)*chunk_stride]
#     is_final = i == total_chunk_num - 1
#     res = model.generate(input=speech_chunk, cache=cache, is_final=is_final, chunk_size=chunk_size)
#     if len(res[0]["value"]):
#         print(res)
#
# ASR（paraformer-zh-streaming，chunk_size=[0,10,5]）：
# from funasr import AutoModel
#
# chunk_size = [0, 10, 5] #[0, 10, 5] 600ms, [0, 8, 4] 480ms
# encoder_chunk_look_back = 4 #number of chunks to lookback for encoder self-attention
# decoder_chunk_look_back = 1 #number of encoder chunks to lookback for decoder cross-attention
#
# model = AutoModel(model="paraformer-zh-streaming", model_revision="v2.0.4")
#
# import soundfile
# import os
#
# wav_file = os.path.join(model.model_path, "example/asr_example.wav")
# speech, sample_rate = soundfile.read(wav_file)
# chunk_stride = chunk_size[1] * 960 # 600ms
#
# cache = {}
# total_chunk_num = int(len((speech)-1)/chunk_stride+1)
# for i in range(total_chunk_num):
#     speech_chunk = speech[i*chunk_stride:(i+1)*chunk_stride]
#     is_final = i == total_chunk_num - 1
#     res = model.generate(input=speech_chunk, cache=cache, is_final=is_final, chunk_size=chunk_size, encoder_chunk_look_back=encoder_chunk_look_back, decoder_chunk_look_back=decoder_chunk_look_back)
#     print(res)
# =============================================================================

# fsmn-vad 流式 VAD（用户指定 chunk_size=200，单位 ms，事件驱动）
# （[beg,-1] 语音开始 / [-1,end] 语音结束 / [beg,end] 完整段 / [] 无事件）
vad_chunk_size = 200
vad_chunk_stride = int(vad_chunk_size * SAMPLE_RATE / 1000)  # 3200 样本 = 200ms

# paraformer-zh-streaming 流式转写（用户指定 chunk_size=[0,10,5]）
asr_chunk_size = [0, 10, 5]               # [0,10,5] = 600ms 块粒度
encoder_chunk_look_back = 4               # encoder self-attention 回看块数
decoder_chunk_look_back = 1               # decoder cross-attention 回看 encoder 块数
asr_chunk_stride = asr_chunk_size[1] * 960  # 9600 样本 = 600ms


def _rms(audio: np.ndarray) -> float:
    """整段音频的均方根能量（float32→float64 防溢出）。"""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


class STTEngine(BaseInputAdapter):
    """麦克风监听 + 语音识别引擎。

    线程模型：
    - 采集线程：sounddevice InputStream 回调只把块塞进队列（轻量，不阻塞音频回调）
    - 处理线程：VAD 状态机消费块，说话中每 600ms 把音频块入队（feed）；切段时入队 end
    - 流式转写线程（本地引擎）：消费 feed/end，向 ASR 服务增量推理，交付最终文本
      （推理在说话过程中完成，切段后立即可得完整文本，不阻塞 VAD）
    - 转写线程（云端引擎）：每段语音独立线程整段上传转写（不阻塞 VAD）
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
        self.silence_seconds = float(
            getattr(cfg, "STT_SILENCE_SECONDS", None) or 0.6)
        self.max_seconds = float(
            getattr(cfg, "STT_MAX_SECONDS", None) or 10.0)
        # fsmn-vad 流式 VAD（用户指定只用这个）：模型统一存放 src/asr/models，
        # 处理线程首次使用时懒加载；cache 跨块常驻（VAD 只被处理线程独占调用）
        self._vad_model = None
        self._vad_cache = {}
        self._vad_lock = threading.Lock()  # 预热线程与处理线程并发加载保护
        self._preheat_done = False         # 预热只做一次（模型/链路常驻进程）

        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop_event = threading.Event()
        self._blocks: "queue.Queue[np.ndarray]" = queue.Queue()
        # 识别结果交付：future 队列 + 无等待者时的文本缓冲
        self._futures: "list[asyncio.Future]" = []
        self._pending_texts: "list[str]" = []
        self._listener: "threading.Thread | None" = None
        self._processor: "threading.Thread | None" = None
        # 流式转写（本地引擎）：feed/end 指令队列 + 消费线程 + ASR 侧未满
        # asr_chunk_stride 样本的残余块（说话中持续累积到 asr_chunk_stride 才入队）
        self._stream_queue: "queue.Queue[tuple]" = queue.Queue()
        self._streamer: "threading.Thread | None" = None
        self._feed_buf: "list[np.ndarray]" = []
        self._feed_acc_len: int = 0

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
        if self.engine == "local":
            self._streamer = threading.Thread(
                target=self._stream_loop, name="stt-streamer", daemon=True)
            self._streamer.start()
        self.preheat()

    def preheat(self) -> None:
        """预热语音识别链路（VAD 模型 + 本地 ASR 空会话），失败静默。

        动机：fsmn-vad 是处理线程首次说话才懒加载（funasr 导入 + 模型
        加载可能卡数百 ms）；本地 ASR 服务端的首次推理也有初始化开销。
        启动时后台预热，用户开口即用，首句识别延迟最低。

        只做一次（模型与链路常驻进程）；任何一步失败都只丢预热，
        不影响后续正常识别。
        """
        if self._preheat_done:
            return
        self._preheat_done = True
        threading.Thread(
            target=self._preheat_worker, name="stt-preheat",
            daemon=True).start()

    def _preheat_worker(self) -> None:
        try:
            self._load_vad_model()
            if self.engine != "local":
                return
            # 本地 ASR 服务：跑一个空会话（600ms 静音 feed + end），
            # 烧热服务端首次推理路径，真实首块 feed 不再有初始化开销
            import base64

            import requests

            resp = requests.post(
                f"{self.server_url}/stream/start", json={}, timeout=10)
            resp.raise_for_status()
            session_id = str(resp.json().get("session_id") or "")
            silence = np.zeros(asr_chunk_stride, dtype=np.float32)
            payload = {
                "session_id": session_id,
                "audio": base64.b64encode(silence.tobytes()).decode("ascii"),
            }
            requests.post(
                f"{self.server_url}/stream/feed", json=payload, timeout=10)
            requests.post(
                f"{self.server_url}/stream/end",
                json={"session_id": session_id}, timeout=10)
            console.dim("[STT] 识别链路预热完成")
        except Exception as e:
            console.dim(f"[STT] 预热失败（不影响使用）：{e}")

    def stop(self) -> None:
        """停止监听/处理/流式线程（转写中的线程自然结束）。"""
        self._stop_event.set()
        for t in (self._listener, self._processor, self._streamer):
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

    # ---------- 语音活动检测（fsmn-vad 流式） ----------

    def _load_vad_model(self) -> bool:
        """懒加载 fsmn-vad 模型（仅处理线程首次需要时调用）。

        模型存放在 src/asr/models（缺失自动从 ModelScope 下载）；加载失败
        返回 False，STT 静默降级（不触发语音段），不引入其它检测手段。
        """
        if self._vad_model is not None:
            return True
        with self._vad_lock:
            if self._vad_model is not None:  # 预热线程可能已抢先加载
                return True
            try:
                from funasr import AutoModel
                from src.asr import models

                self._vad_model = AutoModel(
                    model=str(models.ensure_vad_model()),
                    model_revision=models.MODEL_REVISION,
                    device="cpu", disable_update=True, disable_pbar=True)
                self._vad_cache = {}
                return True
            except Exception as e:
                console.error(f"[STT] fsmn-vad 加载失败，语音检测不可用：{e}")
                self._vad_model = None
                return False

    def _vad_events(self, audio: np.ndarray) -> "list[list[int]]":
        """喂 200ms 音频给 fsmn-vad，返回段事件列表。

        事件语义：`[beg,-1]` 语音开始 / `[-1,end]` 语音结束 /
        `[beg,end]` 完整段（块内说完） / `[]` 无事件。输入需 float32 [-1,1]
        （采集为 int16，先 /32768 归一化）；麦克风无流末尾故 is_final 恒 False，
        cache 常驻；max_end_silence_time 在 cache 首次初始化时生效，用于对齐
        STT_SILENCE_SECONDS 控制静音切段延迟。
        """
        # fsmn-vad 要求一维波形：sounddevice 单声道块是 (frames,1)，2D 直接喂会被
        # pad 成 3D，frontend 判定 0 帧 → batch_data_time=0，funasr 除零崩溃
        audio = np.asarray(audio).reshape(-1).astype(np.float32) / 32768.0
        res = self._vad_model.generate(
            input=audio,
            cache=self._vad_cache,
            is_final=False,
            chunk_size=vad_chunk_size,
            max_end_silence_time=int(self.silence_seconds * 1000),
        )
        value = (res[0].get("value") if res else None) or []
        return [list(ev) for ev in value]

    def _process_loop(self) -> None:
        """处理线程：fsmn-vad 流式事件驱动状态机。

        每攒满 vad_chunk_stride（3200）样本就喂一次 VAD（事件驱动 silent/
        speaking 切换）；说话中每攒满 asr_chunk_stride（9600）样本就把音频
        块流式提交给本地 ASR（self._feed_buf），切段时残余随 end 一起提交。
        """
        if not self._load_vad_model():
            # VAD 不可用（funasr 缺失/模型加载失败）：仅消费音频块不触发
            # 语音段，STT 静默降级
            while not self._stop_event.is_set():
                try:
                    self._blocks.get(timeout=0.2)
                except queue.Empty:
                    continue
            return
        silence_blocks = max(1, int(self.silence_seconds / BLOCK_SECONDS))
        state = "silent"
        buf: "list[np.ndarray]" = []
        silent_run = 0            # 麦克风停摆（无数据）时静音兜底计数
        seg_start_ms = 0          # 当前语音段开始时刻（VAD 毫秒，段内递增更新）
        vad_buf: "list[np.ndarray]" = []  # 凑 vad_chunk_stride 喂 VAD 的块缓冲
        vad_acc_len = 0           # vad_buf 已累积的样本数

        while not self._stop_event.is_set():
            try:
                block = self._blocks.get(timeout=0.2)
            except queue.Empty:
                # 麦克风没给数据：VAD 无法判定，静音兜底切段（防末尾段永不切出）
                if state == "speaking":
                    silent_run += 1
                    if silent_run >= silence_blocks:
                        self._commit(buf, len(buf) * BLOCK_SECONDS)
                        buf, state, silent_run, seg_start_ms = [], "silent", 0, 0
                continue

            if state == "speaking":
                buf.append(block)
                # 本地引擎：说话中每攒满 asr_chunk_stride 样本流式提交一块
                # （对齐流式模型的 chunk 粒度），切段时残余随 end 一起提交
                if self.engine == "local":
                    self._feed_buf.append(block)
                    self._feed_acc_len += len(block)
                    if self._feed_acc_len >= asr_chunk_stride:
                        # feed 无切段时间（speech_seconds 仅 end 携带）
                        self._stream_queue.put(
                            ("feed", np.concatenate(self._feed_buf), None))
                        self._feed_buf = []
                        self._feed_acc_len = 0

            # 攒满 vad_chunk_stride 样本就喂 VAD（与 demo chunk_stride 语义一致）
            vad_buf.append(block)
            vad_acc_len += len(block)
            if vad_acc_len < vad_chunk_stride:
                continue
            audio = np.concatenate(vad_buf)
            events = self._vad_events(audio)
            for beg, end in events:
                if beg >= 0 and state == "silent":
                    # 语音开始（含 [beg,-1] 与完整段 [beg,end] 的开始）：
                    # 段缓冲重设为当前块（丢弃此前静音），记录开始时刻
                    state = "speaking"
                    seg_start_ms = beg
                    buf = [audio]
                    if self.engine == "local":
                        self._feed_buf = [audio]
                        self._feed_acc_len = len(audio)
                if end >= 0 and state == "speaking":
                    # 语音结束（含 [-1,end] 与完整段 [beg,end] 的结束）：
                    # 时长用 VAD 真实起止时刻（不含尾部静音）
                    self._commit(buf, max(0.0, (end - seg_start_ms) / 1000.0))
                    buf, state, silent_run, seg_start_ms = [], "silent", 0, 0
                    if self.engine == "local":
                        self._feed_buf = []
                        self._feed_acc_len = 0
            vad_buf = []
            vad_acc_len = 0

            # 超时强制切段（连续说话不停顿也按时长切）
            if state == "speaking" and len(buf) * BLOCK_SECONDS >= self.max_seconds:
                self._commit(buf, len(buf) * BLOCK_SECONDS)
                buf, state, silent_run, seg_start_ms = [], "silent", 0, 0

    def _commit(self, buf: "list[np.ndarray]", speech_seconds: float) -> None:
        """语音段收尾：校验长度/能量 → 本地流式 end / 云端写 WAV 上传。"""
        if not buf:
            return
        audio = np.concatenate(buf) if len(buf) > 1 else buf[0]
        if audio.size < int(SAMPLE_RATE * _MIN_SPEECH_SECONDS):
            return
        if _rms(audio) < _EMPTY_RMS:
            return
        if self.engine == "local":
            # 流式：说话中已 feed 的块无需重复提交，残余块随 end 收尾，
            # 服务端立即返回完整文本（推理已完成）
            if self._feed_buf:
                tail = np.concatenate(self._feed_buf)
                self._feed_buf.clear()
            else:
                tail = None
            self._stream_queue.put(("end", tail, speech_seconds))
            return
        path = self._write_wav(audio)
        if path:
            threading.Thread(
                target=self._transcribe_worker, args=(path, speech_seconds),
                daemon=True,
            ).start()

    # ---------- 流式转写（本地引擎） ----------

    def _stream_loop(self) -> None:
        """流式转写线程：消费 feed/end 指令，向 ASR 服务增量推理。

        会话懒创建（首次指令时 start）；说话中每 600ms feed 一块，服务端
        返回该块增量文本（partial），本线程累积全部 partial；end 收尾时
        残余块增量拼接到尾部，得到完整文本 → 交付主循环。推理阻塞在独立
        线程，不影响 VAD。
        """
        import base64

        import requests

        session_id = None
        parts: "list[str]" = []   # 当前语音段已累积的 partial 片段
        streaming_shown = False   # 本段是否已开始流式显示（识别中文本同一行追加）
        try:
            while not self._stop_event.is_set():
                try:
                    cmd, audio, speech_seconds = self._stream_queue.get(
                        timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if session_id is None:
                        resp = requests.post(
                            f"{self.server_url}/stream/start", json={},
                            timeout=10)
                        resp.raise_for_status()
                        session_id = str(resp.json().get("session_id") or "")
                    payload: "dict[str, str]" = {"session_id": session_id}
                    if audio is not None:
                        # int16 采样值 → float32 [-1,1]（与 soundfile 读取一致）
                        audio = audio.astype(np.float32) / 32768.0
                        payload["audio"] = base64.b64encode(
                            audio.tobytes()).decode("ascii")
                    if cmd == "feed":
                        resp = requests.post(
                            f"{self.server_url}/stream/feed", json=payload,
                            timeout=10)
                        resp.raise_for_status()
                        partial = str(resp.json().get("text") or "").strip()
                        if partial:
                            parts.append(partial)
                            # 流式显示：与 LLM 输出一致，同一行增量追加
                            # （打字机效果），段结束（end）时补换行
                            if not streaming_shown:
                                print(console.paint(
                                    "[STT] 流式识别中：", console.GRAY),
                                    end="", flush=True)
                                streaming_shown = True
                            print(console.paint(partial, console.GRAY),
                                  end="", flush=True)
                    else:  # end
                        resp = requests.post(
                            f"{self.server_url}/stream/end", json=payload,
                            timeout=10)
                        resp.raise_for_status()
                        text = str(resp.json().get("text") or "").strip()
                        if text:
                            parts.append(text)
                        full_text = "".join(parts)
                        session_id = None
                        parts = []
                        if streaming_shown:
                            print()  # 流式行结束补换行（对齐下一行日志）
                            streaming_shown = False
                        if full_text and self._loop is not None:
                            console.info(
                                f"[STT] 流式转写完成（语音 {speech_seconds:.1f}s）："
                                f"{full_text}")
                            self._loop.call_soon_threadsafe(
                                self._deliver, full_text, speech_seconds)
                except Exception as e:
                    console.error(f"[STT] 流式转写失败：{e}")
                    session_id = None
                    parts = []
                    streaming_shown = False
        finally:
            if session_id is not None:
                try:
                    requests.post(
                        f"{self.server_url}/stream/end",
                        json={"session_id": session_id}, timeout=10)
                except Exception:
                    pass

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
        start = time.time()
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
            elapsed_ms = int((time.time() - start) * 1000)
            console.info(
                f"[STT] 转写完成 {elapsed_ms} ms（{self.engine}，"
                f"语音 {speech_seconds:.1f}s）")
            self._loop.call_soon_threadsafe(self._deliver, text, speech_seconds)

    def transcribe(self, wav_path: str) -> str:
        """按引擎配置转写：local 走本地 FunASR 服务（一次性兜底），cloud 走云端。"""
        if self.engine == "local":
            return self._transcribe_local(wav_path)
        return self._transcribe_cloud(wav_path)

    def _transcribe_local(self, wav_path: str) -> str:
        """提交本地 ASR 服务（asr_server.py 独立进程，8487 端口）一次性转写。

        主流程已走流式接口（_stream_loop），此方法仅作 /transcribe 兜底
        （如外部一次性调用）。服务未启动 / 连接失败时抛异常。
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
