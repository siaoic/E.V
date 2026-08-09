"""TTS 引擎：HTTP 流式客户端（连接 TTS启动.bat 的 :8000 服务端）+ 官方 AudioQueue 无缝播放 + 字级时间戳字幕。

主程序不加载任何模型（省显存、启动快）：合成由服务端 /tts/stream 完成，
token 级流式返回 base64 wav 块 + 字级时间戳；本地仅用 gsv_tts.Player 的
AudioQueue / AudioClip 做同一输出流无缝播放，字幕锚定到播放线程真实开播时刻。

播放/字幕/口型时序与进程内版完全一致：
- 每块 wav 解码后 clip.play() 入队，播放线程逐块 write，块间由设备缓冲衔接；
- 播放钩子在「句首块真正写入设备前」写入字幕锚点，字幕线程以 t0+start_s
  （服务端返回的全局时间戳）逐字推进；
- 口型回调在每块真正开播前触发，临时 wav 即写即删。

兼容旧模块接口（main.py / stream.py / cleaner.py 引用）：
start/stop/drain/speak/interrupt/clear_interrupt/set_on_play_callback/
set_subtitle_callback/apply_ref + _wav_cache / _cleanup_output。
"""

import asyncio
import base64
import io
import itertools
import json
import os
import queue
import re
import tempfile
import threading
import time
import types
from typing import Callable, Optional, Tuple

import soundfile as sf

# gsv_tts/TTS.py 在模块顶层调 logging.basicConfig(INFO) 并触发 Config.py
# 的 choose_attention_backend() 打印 "SDPBackend: xxx"——主程序导入 engine
# 时间接触发。先于 gsv_tts 设根 logger 级别为 WARNING：basicConfig 幂等，
# TTS.py 的 basicConfig 不再覆盖；Config 的 INFO 日志被过滤不输出。
import logging
logging.basicConfig(level=logging.WARNING)

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from gsv_tts.Player import AudioQueue, AudioClip

from src.utils import console

# 流式参数：与服务端 /tts/stream 默认一致（sovits_cache=[50,55] 匹配 chunk*2 与 +overlap）
# _STREAM_CHUNK：每块生成的 token 数，直接决定「打断尾音」粒度（正在播放的块
# 会自然播完）。25 token → 最长 ~2s 尾音；8 token → ~1.2s 且首字更快。
_STREAM_CHUNK = 8
_OVERLAP_LEN = 5

# 纯符号/无实质内容碎片：直接丢弃，防 GPT-SoVITS 合成退化（"啊——"长音怪叫）
_HAS_CONTENT_RE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 兼容 cleaner.py：新引擎临时 wav 播放即删，无持久缓存可清
_wav_cache: dict = {}


def _cleanup_output() -> Tuple[int, int]:
    """兼容 cleaner.py 的旧清理入口：新引擎无 output 目录残留。"""
    return (0, 0)


class _SubtitlesQueue:
    """按字级时间戳渐进输出字幕（对齐真实播放时刻）。

    服务端返回的字级时间戳是「相对整句起点的全局时间轴」，而 AudioQueue 是
    同一输出流逐块连续播放——因此只要把时间轴锚定到「本句第一块音频真正
    开始写入设备的墙钟时刻」，后续所有块的 start_s/end_s 就能精确对齐语音。

    anchor 由播放钩子（播放线程每块 write 前）写入；字幕线程处理句首块时
    等待锚点就绪后以其为基准，逐字把「累积文本」交给 sink（本项目为
    sub.push("text", ...)），气泡/网页随之逐字浮现。
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        self.q: "queue.Queue[Optional[Tuple[list, str, int]]]" = queue.Queue()
        self.t: Optional[threading.Thread] = None
        self._sink = sink
        self._anchors: dict = {}      # sent_id -> 句首块实际开播墙钟时刻
        self._anchors_lock = threading.Lock()
        self._closed = False          # 打断：终止当前字幕线程（下次 add 重建）

    def _wait(self, until: float, sent_id: int) -> bool:
        """轮询等待到 until 时刻；打断时返回 False。"""
        while time.time() < until:
            if self._closed or sent_id not in self._anchors:
                return False
            time.sleep(0.005)
        return True

    def process(self) -> None:
        last_i = 0
        last_sent: Optional[int] = None
        t0: Optional[float] = None

        while True:
            item = self.q.get()
            if item is None:
                break

            subtitles, text, sent_id = item

            # 新句子：重置字符游标与时间基准
            if sent_id != last_sent:
                last_i = 0
                last_sent = sent_id
                t0 = None

            # 句首块：等待播放线程真正开播（write 前触发 anchor）
            if t0 is None:
                while sent_id not in self._anchors:
                    if self._closed:
                        self.t = None
                        return
                    time.sleep(0.005)
                with self._anchors_lock:
                    t0 = self._anchors[sent_id]

            for subtitle in subtitles:
                start = t0 + subtitle["start_s"]   # 全局时间戳 → 墙钟
                if not self._wait(start, sent_id):
                    self.t = None
                    return
                if subtitle["end_s"] and t0 + subtitle["end_s"] > time.time():
                    if subtitle["orig_idx_end"] > last_i:
                        try:
                            self._sink(text[:subtitle["orig_idx_end"]])
                        except Exception:
                            pass
                        last_i = subtitle["orig_idx_end"]
                        if not self._wait(t0 + subtitle["end_s"], sent_id):
                            self.t = None
                            return

        self.t = None

    def add(self, subtitles, text, sent_id: int) -> None:
        self.q.put((subtitles, text, sent_id))
        if self.t is None or not self.t.is_alive():
            self._closed = False  # 重建线程：复位打断终止标志
            self.t = threading.Thread(target=self.process, daemon=True)
            self.t.start()

    def anchor(self, sent_id: int) -> None:
        """播放线程在句首块真正写入设备前调用：记录该句开播墙钟时刻。"""
        with self._anchors_lock:
            self._anchors[sent_id] = time.time()

    def clear(self) -> None:
        """打断：丢弃未播字幕并结束当前字幕线程（下一次 add 自动重建）。"""
        self._closed = True
        with self._anchors_lock:
            self._anchors.clear()
        with self.q.mutex:
            self.q.queue.clear()
        self.q.put((None, None, 0))


class TTSEngine:
    """HTTP 流式 TTS 引擎（连接服务端 /tts/stream，本地不加载模型）。"""

    def __init__(self) -> None:
        from src.utils import config as _config
        self.ref_audio = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIO", "") or "").strip()
        self.ref_text = str(
            getattr(_config.cfg, "GPTSOVITS_PROMPT_TEXT", "") or "").strip()
        self.url = str(
            getattr(_config.cfg, "GPTSOVITS_URL", "") or "").strip().rstrip("/")

        self._client: Optional[httpx.AsyncClient] = None
        self._queue: Optional[AudioQueue] = None     # 官方无缝播放队列
        self._on_play: Optional[Callable[[str, str, float], None]] = None
        self._sub_sink: Optional[Callable[[str], None]] = None
        self._sub_q: Optional[_SubtitlesQueue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False                # stop() 后常驻播放线程退出标志
        self._gen = 0                        # 代次号：interrupt 递增，合成循环检测后提前退出
        self._interrupted = False            # interrupt 标志（clear_interrupt 复位）
        self._sub_mute = False               # 打断后抑制字幕线程的残留推送
        self._play_metas: "queue.Queue[Tuple[str, str, float, int, int]]" = queue.Queue()
        self._sent_seq = itertools.count(1)  # 句子序号：字幕锚点/游标按句隔离
        self._pending: Optional[asyncio.Queue] = None  # 待合成句子队列
        self._pump_task: Optional[asyncio.Task] = None  # 串行合成泵
        self._working = False                # 当前是否正在合成（drain 等待用）
        self._tmp_dir = tempfile.mkdtemp(prefix="vtuber_tts_")

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """探测服务端可用性 + 预热 + 初始化播放/字幕管线（不加载任何模型）。"""
        if httpx is None:
            console.warn("TTS：缺少 httpx 依赖，HTTP 流式客户端不可用")
            return False
        if not self.ref_audio:
            console.warn("TTS：未配置 GPTSOVITS_REF_AUDIO，语音合成关闭")
            return False
        self._loop = asyncio.get_running_loop()
        try:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(300, connect=10, pool=10))
            r = await client.get(self.url + "/", timeout=10)
            r.raise_for_status()
            self._client = client
        except Exception as e:
            console.warn(
                f"TTS：无法连接服务端 {self.url}（{e}）——请先运行 TTS启动.bat")
            return False
        try:
            self._queue = AudioQueue(32000)
            self._install_play_hook()
        except Exception as e:
            console.dim(f"TTS 播放队列初始化失败（口型/字幕可能不可用）：{e}")
        self._sub_q = _SubtitlesQueue(self._push_subtitle)
        self._pending = asyncio.Queue()
        # 预热：触发服务端参考音频编码 + CUDA graph 编译，消除首句合成延迟。
        # 一次性的（约数秒），完成后第一次真实说话立即出声。
        try:
            await self._warmup()
        except Exception as e:
            console.dim(f"TTS 预热失败（不影响使用）：{e}")
        console.ok("TTS 引擎就绪（HTTP 流式合成 + 无缝播放 + 字级字幕）")
        return True

    async def _warmup(self) -> None:
        """向服务端发送一句短文本，让参考音频编码 / CUDA graph 编译落在启动期。"""
        payload = {
            "text": "你好。",
            "speaker_audio": self.ref_audio,
            "prompt_audio": self.ref_audio,
            "prompt_text": self.ref_text,
            "stream_chunk": _STREAM_CHUNK,
            "overlap_len": _OVERLAP_LEN,
            "boost_first_chunk": True,
        }
        async with self._client.stream(
                "POST", self.url + "/tts/stream", json=payload) as resp:
            if resp.status_code != 200:
                return
            async for _line in resp.aiter_lines():
                if self._interrupted:
                    break
        console.ok("TTS 预热完成（参考音频编码 + CUDA graph 已编译）")

    def _install_play_hook(self) -> None:
        """把口型/字幕锚点回调挂到 AudioQueue 播放线程。

        播放线程改为「常驻循环」：打断（stop 清空队列）后线程不退出，等待
        下一块数据自动继续消费——修复「打断后合成了却无人播放」的竞态
        （旧一次性线程在打断瞬间退出，新块 put 时因线程仍在收尾而不会重启）。
        队列空时置 playback_finished（空闲）；有数据时清空并计数，drain 据此
        判断「最后一块真正写完」。
        """
        aq = self._queue
        engine = self
        aq._active = 0  # 正在 write 的块数（drain 等待用）

        def _hooked_run_playback(aq_self):
            try:
                while True:
                    try:
                        data = aq_self.q.get(timeout=0.25)
                    except queue.Empty:
                        # 队列空：标记空闲（wait/drain 可返回），继续等待新数据
                        aq_self.playback_finished.set()
                        continue
                    aq_self.playback_finished.clear()
                    aq_self._active += 1
                    meta = None
                    if not engine._play_metas.empty():
                        try:
                            meta = engine._play_metas.get_nowait()
                        except Exception:
                            meta = None
                    if meta is not None:
                        wav, text, dur, sent_id, chunk_idx = meta
                        # 字幕时间轴锚点：句首块真正开播（write 前）的墙钟时刻
                        if chunk_idx == 0:
                            try:
                                engine._sub_q.anchor(sent_id)
                            except Exception:
                                pass
                        try:
                            engine._fire_play(wav, text, dur)
                        finally:
                            if wav:
                                try:
                                    os.remove(wav)  # 曲线已同步加载完毕，wav 即时删除
                                except Exception:
                                    pass
                    if aq_self.stream:
                        try:
                            aq_self.stream.write(data)
                        except Exception:
                            # 打断（stream.stop/start）等瞬态错误：丢弃该块，等待下一块
                            pass
                    aq_self._active -= 1
                    if engine._stopped:
                        break
            finally:
                aq_self.playback_finished.set()

        aq._run_playback = types.MethodType(_hooked_run_playback, aq)
        # 常驻播放线程：启动后不随队列空退出
        aq.playback_finished.clear()
        aq.t = threading.Thread(target=aq._run_playback, daemon=True)
        aq.t.start()

    def _fire_play(self, wav: str, text: str, dur_s: float) -> None:
        """音频真正开始播放时触发回调（口型曲线）。"""
        cb = self._on_play
        if cb is None:
            return
        try:
            cb(wav, text, dur_s)
        except Exception as e:
            console.dim(f"TTS 口型回调异常：{e}")

    async def stop(self) -> None:
        """停止：打断播放/合成、关闭 HTTP 客户端与输出流（进程退出前调用）。"""
        self._stopped = True  # 常驻播放线程据此退出
        self.interrupt()
        # 等串行泵退出
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=5)
            except Exception:
                try:
                    self._pump_task.cancel()
                except Exception:
                    pass
        self._pump_task = None
        if self._sub_q is not None:
            try:
                self._sub_q.clear()
            except Exception:
                pass
            self._sub_q = None
        if self._queue is not None:
            try:
                self._abort_playback_now()
            except Exception:
                pass
            try:
                stream = self._queue.stream
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            self._queue = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
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

    async def drain(self) -> None:
        """等待全部已提交句子合成完成 + 全部音频播完。

        常驻播放线程下用「队列空 + 正在 write 的块数归零」判断播完
        （playback_finished 已被常驻线程用于空闲标记，不再表示批次结束）。
        """
        if self._queue is None:
            return
        # 等串行泵消费完所有待合成句子
        while self._working or (
                self._pending is not None and not self._pending.empty()):
            await asyncio.sleep(0.05)
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=300)
            except Exception:
                pass
        # 等播放队列清空且最后一块真正写完
        aq = self._queue
        if aq is not None:
            for _ in range(6000):  # 最长等 300s
                if aq.q.empty() and getattr(aq, "_active", 0) <= 0:
                    break
                await asyncio.sleep(0.05)
        # 字幕队列与播放基本同步，给残留尾字推送一点收尾时间
        await asyncio.sleep(0.3)

    # ---------- 对外接口（main.py / stream.py 调用） ----------

    def set_on_play_callback(self, cb: Optional[Callable[[str, str, float], None]]) -> None:
        """设置音频开始播放回调（wav 路径, 文本, 时长秒）——口型同步用。"""
        self._on_play = cb

    def set_subtitle_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        """设置字幕回调：官方字级时间戳推进时调用（入参为累积文本）。"""
        self._sub_sink = cb

    def apply_ref(self, audio: str, text: str) -> None:
        """热更新参考音频/文本（控制中心 !tts_audio / !tts_text 热调用）。"""
        self.ref_audio = (audio or "").strip()
        self.ref_text = (text or "").strip()

    def interrupt(self) -> None:
        """立即闭嘴：停播 + 放弃当前/待合成 + 丢弃未播字幕。"""
        self._gen += 1
        self._interrupted = True
        self._sub_mute = True
        if self._queue is not None:
            try:
                self._abort_playback_now()
            except Exception:
                pass
        try:
            while not self._play_metas.empty():
                self._play_metas.get_nowait()
        except Exception:
            pass
        if self._sub_q is not None:
            try:
                self._sub_q.clear()
            except Exception:
                pass

    def _abort_playback_now(self) -> None:
        """立即闭嘴：丢弃所有未播块（不碰 stream）。

        官方 AudioQueue.stop() 的 stream.stop()+start()，以及 sounddevice 的
        abort()，都会令「正阻塞在 stream.write() 上的常驻播放线程」永久卡死：
        设备层还在播放当前块（MME 错误），write 永不返回、start 必然失败，
        打断后的新块全部堆积无人播放。因此打断只清队列——正在写的那块自然
        播完（约 0.9s 尾音），线程回到 q.get() 等待，新句块随后正常续播。
        """
        aq = self._queue
        if aq is None:
            return
        with aq.q.mutex:
            aq.q.queue.clear()
        aq.playback_finished.set()

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志（main.py 每轮调用）。"""
        self._interrupted = False
        self._sub_mute = False

    # ---------- 合成（HTTP 流式，串行泵） ----------

    async def speak(self, text: str) -> None:
        """把一句话送入合成队列（立即返回，不阻塞 LLM 流）。

        串行泵按句顺序 POST /tts/stream；服务端 infer_stream_async 内部
        有 _infer_lock，天然保序，首字延迟仅为首块合成 + 网络传输。
        """
        if (self._client is None or self._pending is None
                or self._interrupted):
            return
        text = (text or "").strip()
        if not text or not _HAS_CONTENT_RE.search(text):
            return  # 纯符号碎片：防 GPT-SoVITS 合成退化
        await self._pending.put(text)
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """串行消费待合成句子；空闲（队列空）时退出，下次 speak 自动重启。"""
        while True:
            text = await self._pending.get()
            if self._interrupted:
                continue  # 丢弃打断后的残留句子
            gen = self._gen
            self._working = True
            try:
                await self._synth_remote(text, gen)
            except asyncio.CancelledError:
                self._working = False
                raise
            except Exception as e:
                if gen == self._gen:
                    console.error(f"TTS 合成失败：{e}")
            finally:
                self._working = False
            if self._pending.empty():
                return  # 空闲退出

    async def _synth_remote(self, text: str, gen: int) -> None:
        """POST /tts/stream，逐行解析 NDJSON：播放 + 口型 + 字级字幕。"""
        payload = {
            "text": text,
            "speaker_audio": self.ref_audio,
            "prompt_audio": self.ref_audio,
            "prompt_text": self.ref_text,
            "stream_chunk": _STREAM_CHUNK,
            "overlap_len": _OVERLAP_LEN,
            "boost_first_chunk": True,
            # 模型由服务端预加载（默认 Neuro），无需请求级指定
        }
        sent_id = next(self._sent_seq)
        chunk_idx = 0
        try:
            async with self._client.stream(
                    "POST", self.url + "/tts/stream", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if gen != self._gen:
                        break  # 打断：放弃剩余合成
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if "error" in obj:
                        raise RuntimeError(obj["error"])
                    if obj.get("done"):
                        break
                    await self._emit_remote(obj, text, sent_id, chunk_idx)
                    chunk_idx += 1
        except (json.JSONDecodeError, KeyError) as e:
            raise RuntimeError(f"流式响应解析失败：{e}") from e

    async def _emit_remote(self, obj: dict, text: str,
                           sent_id: int, chunk_idx: int) -> None:
        """单块音频：解码 wav → 入队无缝播放 → 口型临时 wav → 字级字幕入队。

        subtitles 时间戳是服务端返回的「相对整句起点的全局时间轴」
        （infer_stream_async 内部用 last_end_s 累积），字幕线程锚定到句首块
        实际开播时刻后直接按全局时间戳推进即可，不做任何偏移。
        """
        audio, sr = sf.read(io.BytesIO(base64.b64decode(obj["audio"])),
                            dtype="float32")
        if audio.ndim > 1:
            audio = audio[:, 0]  # 单声道
        dur_s = len(audio) / sr if sr else 0.0
        audio_len_s = float(obj.get("audio_len") or dur_s)
        subtitles = obj.get("subtitles") or []
        orig_text = obj.get("orig_text") or text

        clip = AudioClip(self._queue, audio, sr, audio_len_s, subtitles, orig_text)

        wav_path = ""
        try:
            if self._on_play is not None:
                fd, wav_path = tempfile.mkstemp(
                    suffix=".wav", prefix="vtuber_tts_", dir=self._tmp_dir)
                os.close(fd)
                sf.write(wav_path, audio, sr)
                self._play_metas.put((wav_path, "", dur_s, sent_id, chunk_idx))
        except Exception as e:
            console.dim(f"TTS 写临时音频失败：{e}")
            wav_path = ""
        try:
            clip.play()  # 官方无缝播放：同一 OutputStream 逐块 write，块间由设备缓冲衔接
        except Exception as e:
            console.error(f"TTS 播放失败：{e}")
            return
        if subtitles and self._sub_q is not None:
            try:
                self._sub_q.add(subtitles, orig_text, sent_id)
            except Exception:
                pass

    def _push_subtitle(self, text: str) -> None:
        """字幕线程 → 事件循环线程（Qt 线程）转发，保证 GUI 安全。"""
        if (not text or self._sub_sink is None or self._loop is None
                or self._sub_mute):
            return
        try:
            self._loop.call_soon_threadsafe(self._sub_sink, text)
        except Exception:
            pass
