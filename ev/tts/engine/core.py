"""TTSEngine：GSV-TTS-Lite 进程内 Token 级流式合成引擎。

链路：LLM 文本（final 段）→ speak 入队 → pump 线程串行消费 →
``infer_stream(stream_mode="token", stream_chunk=25, overlap_len=5)`` 逐块
yield AudioClip → ``player._emit_block`` 写入 GSV AudioQueue 出声（首块即
触发 on_play 回调，口型同步）→ 字幕丢给 SubtitlesQueue 按字级时间戳推送。

不复制声卡逻辑：实际出声由 GSV-TTS-Lite 的 AudioQueue（sounddevice）驱动。
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional

from ev.adapter.tts import BaseTTSAdapter
from ev.utils import config, console
from ev.tts.player import AsyncAudioPlayer
from ev.tts.subtitles import SubtitlesQueue
from ev.tts.echo import record_spoken

# ── pump 线程调度优化（Windows）────────────────────────────────────────
# GPT token 循环是 Python 密集型，与 LLM 流式解析争抢 CPU 时片；
# 提升线程优先级 + 1ms 系统定时器粒度，实测可显著缓解并发下的首块延迟。
try:  # 全局只需一次；无 winmm（非 Windows）则跳过
    import ctypes

    _winmm = ctypes.WinDLL("winmm")
    _winmm.timeBeginPeriod(1)
except Exception:
    pass


def _boost_thread_priority() -> None:
    """把当前线程提到 HIGH 优先级（失败静默，不影响功能）。"""
    try:
        import ctypes

        THREAD_SET_INFORMATION = 0x0020
        THREAD_PRIORITY_HIGHEST = 2
        k32 = ctypes.WinDLL("kernel32")
        handle = k32.OpenThread(
            THREAD_SET_INFORMATION, False, threading.get_native_id()
        )
        if handle:
            k32.SetThreadPriority(handle, THREAD_PRIORITY_HIGHEST)
            k32.CloseHandle(handle)
    except Exception:
        pass

# ── 模块级导出（cleaner / bench 脚本引用）─────────────────────────────
# GPT 累积 25 token 即解码一块音频（首块快速产出），5 token 重叠用于块间平滑
_STREAM_CHUNK = 25
_STREAM_OVERLAP = 5
_SYNTH_PARAMS = {
    "top_k": 5,
    "top_p": 0.9,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "noise_scale": 0.5,
    "speed": 1.0,
}
_WARMUP_TEXTS = (
    "你好。",
    "我是一名虚拟主播，很高兴今天能和大家见面！",
    "嗯……让我想想怎么回答比较好呢？",
    "好耶，我们开始吧~今天要玩点什么游戏呢？",
)

# 参考音频 / 文本回退（cfg 未配置时，用项目自带示例；源为 GSV-TTS-Lite examples）
_SPEAKER_AUDIO = "assets/tts/laffey.mp3"
_PROMPT_AUDIO = "assets/tts/AnAn.ogg"
_PROMPT_TEXT = "ちが……ちがう。レイア、貴様は間違っている。"

# 兼容旧接口：本引擎流式直出到声卡、不落盘，缓存恒空
_wav_cache: dict = {}


def evict_tts_cache() -> None:
    """清理 TTS 磁盘/内存缓存（旧接口：清空 wav 缓存）。"""
    _wav_cache.clear()


def _cleanup_output() -> tuple:
    """删除 output 目录下旧 TTS 临时 wav，返回 (删除数, 释放字节)。"""
    project_root = Path(config.cfg.PROJECT_ROOT)
    out_dir = project_root / "output"
    files = freed = 0
    try:
        if out_dir.is_dir():
            for path in out_dir.glob("*.wav"):
                try:
                    freed += path.stat().st_size
                    path.unlink()
                    files += 1
                except OSError:
                    pass
    except Exception:
        pass
    return files, freed


class _PlaybackWriteClock:
    """声卡消费进度时钟（字幕同步用，包装 gsv AudioQueue 统计样本数）。

    为什么需要：GSV AudioQueue 的播放是「声卡实时消费 + 合成间隙空转」
    （_run_playback 队列空即退出线程，下一块 put 时重开）。字幕时间戳是
    纯音频时长（t0 起连续），墙钟对齐会在合成慢于实时时把空档也走完 →
    字幕比语音越来越抢跑。本时钟以「已被设备接收的样本数 / 采样率」为
    播放进度：``sd.OutputStream.write`` 以实时节奏阻塞放行（设备消费才
    腾出缓冲），写入计数 ≈ 播放进度（误差 ≈ 设备缓冲容量，数十 ms），
    且间隙自动冻结——字幕等进度到位才上屏，从机制上消除抢跑。

    句锚定（sentence_clock）：以「调用时已投喂总样本数」为本句 0 点，
    上一句尾音还在播 / 本句首块还在合成时，时钟值保持 0 不抢跑。
    """

    def __init__(self, audio_queue) -> None:
        self.ok = False
        self.samplerate = int(getattr(audio_queue, "samplerate", 0) or 0)
        self.put_total = 0   # 已投喂样本总数（句起点锚定）
        self.written = 0     # 已被设备接收样本数（≈已播放）
        if self.samplerate <= 0:
            return
        orig_put = audio_queue.put

        def _put(data, *args, **kwargs):
            try:
                self.put_total += int(
                    data.shape[0] if hasattr(data, "shape") else len(data))
            except Exception:
                pass
            return orig_put(data, *args, **kwargs)

        stream = getattr(audio_queue, "stream", None)
        if not hasattr(stream, "write"):
            return
        orig_write = stream.write

        def _write(data, *args, **kwargs):
            result = orig_write(data, *args, **kwargs)
            try:
                self.written += int(
                    data.shape[0] if hasattr(data, "shape") else len(data))
            except Exception:
                pass
            return result

        # 设备输出缓冲补偿：write 完成只代表样本进入声卡缓冲，实际发声
        # 还要晚约一个输出延迟（stream.latency，常见 20~200ms）。不补偿
        # 则字幕恒定抢跑一个缓冲量——「说的字和显示的字对不上」的常数
        # 残差。取不到或离谱（>0.5s）时按 0 处理。
        latency_s = 0.0
        try:
            lat = getattr(stream, "latency", None)
            if isinstance(lat, tuple):
                lat = lat[-1]
            if isinstance(lat, (int, float)) and 0.0 < float(lat) <= 0.5:
                latency_s = float(lat)
        except Exception:
            latency_s = 0.0
        self.latency_s = latency_s

        try:
            audio_queue.put = _put
            stream.write = _write
            self.ok = True
        except Exception:
            pass

    def sentence_clock(self):
        """取本句播放进度时钟（须在首块 play() 之前调用）；不可用返回 None。"""
        if not self.ok:
            return None
        base = self.put_total
        samplerate = self.samplerate
        latency_s = self.latency_s

        def _clock() -> float:
            # 已接收样本 → 秒，再扣掉设备缓冲延迟 = 实际发声进度
            return max(0.0, (self.written - base) / samplerate - latency_s)

        return _clock


class TTSEngine(BaseTTSAdapter):
    """进程内 GSV-TTS-Lite 流式合成引擎（兼容 HTTP 客户端接口）。"""

    name = "tts"

    def __init__(self) -> None:
        self._server_url = "local://gsv-tts-lite"  # 兼容 bench 打印
        self._player = AsyncAudioPlayer()
        self._subtitle_queue = SubtitlesQueue()
        self._speak_queue: Queue = Queue(maxsize=50)
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_lock = threading.Lock()
        self._interrupted = threading.Event()
        # P2-4 修复：合成代次号——interrupt 时 +1 作废所有在跑合成循环，
        # 旧句不再因新轮 clear_interrupt 复位标志而「幽灵复播」
        self._session = 0
        # P1-7：连续跳句计数（成功合成一句即清零）
        self._skip_streak = 0
        self._on_play_done: Optional[Callable] = None
        self._tts = None      # gsv_tts.TTS 实例（start() 时懒加载）
        self._play_clock = None  # 声卡消费进度时钟（_load_model 时挂载）
        self._ready = False
        # 参考音频 / 文本（start 时从 cfg 读取，apply_ref 可热更新）
        self._speaker = ""
        self._prompt_audio = ""
        self._prompt_text = ""
        # 角色专训权重（cfg 配置，None = 官方底模）
        self._role_gpt: Optional[str] = None
        self._role_sovits: Optional[str] = None

    # ─────────────────────────── 生命周期 ───────────────────────────

    async def start(self) -> bool:
        """加载本地 GSV 模型（后台线程，约 20s）；成功返回 True。"""
        try:
            return await asyncio.to_thread(self._load_model)
        except Exception as e:
            console.warn(f"TTS 模型加载失败：{e}")
            return False

    def _load_model(self) -> bool:
        """（后台线程）加载 gsv-tts-lite pip 包并实例化 TTS。"""
        project_root = Path(config.cfg.PROJECT_ROOT)
        try:
            from gsv_tts import TTS  # 解析至 site-packages（gsv-tts-lite>=0.4.7）
        except ImportError as e:
            console.warn(f"无法导入 gsv_tts：{e}（请先 pip install gsv-tts-lite）")
            return False

        # GSV 库 import 时对 root logger 执行 basicConfig(level=INFO)，其推理
        # 过程用 logging.info 刷屏（Starting Stream inference / Using GPT model
        # 等，%(filename)s 格式显示为 TTS.py/Config.py）。主程序输出全走
        # console 不受影响，这里把 root 压到 WARNING 一次性静音库内 INFO
        import logging
        logging.getLogger().setLevel(logging.WARNING)

        models_dir = str(
            config.cfg.GPTSOVITS_MODELS_DIR or (project_root / "ev" / "tts" / "models")
        )

        def _resolve_weight(name: str) -> Optional[str]:
            """解析角色权重 cfg：纯文件名在 models_dir 下查找，含目录的相对路径基于项目根；缺失回退 None（底模）。"""
            name = (name or "").strip()
            if not name:
                return None
            path = Path(name)
            if not path.is_absolute():
                path = (
                    Path(models_dir) / path if len(path.parts) == 1
                    else project_root / path
                )
            if not path.exists():
                console.warn(f"GPTSOVITS 角色权重不存在（回退官方底模）：{path}")
                return None
            return str(path)

        self._role_gpt = _resolve_weight(config.cfg.GPTSOVITS_ROLE_GPT)
        self._role_sovits = _resolve_weight(config.cfg.GPTSOVITS_ROLE_SOVITS)
        ref = (config.cfg.GPTSOVITS_REF_AUDIO or "").strip()
        self._speaker = ref or str(project_root / _SPEAKER_AUDIO)
        self._prompt_audio = self._speaker  # 单参考音频：说话人即音色
        self._prompt_text = (
            (config.cfg.GPTSOVITS_PROMPT_TEXT or "").strip() or _PROMPT_TEXT
        )

        self._tts = TTS(
            use_bert=True,
            sovits_cache=[_STREAM_CHUNK * 2, _STREAM_CHUNK * 2 + _STREAM_OVERLAP],
            models_dir=models_dir,
        )
        # 每次合成结尾默认会 gc.collect()+torch.cuda.empty_cache()：
        # 实测每句额外 ~200ms，且会洗掉 CUDA 分配器形状池（多样短句
        # 首块 p50 由 ~290ms 恶化到 ~1055ms）。运行期改为不清理，
        # 保留缓存与对象池；stop() 时统一做一次真实清理。
        self._tts._orig_empty_cache = self._tts._empty_cache
        self._tts._empty_cache = lambda: None
        # 字幕同步：包装 audio_queue.put / stream.write 统计声卡消费进度
        # （失败降级 None → 字幕回退墙钟对齐）
        self._play_clock = _PlaybackWriteClock(self._tts.audio_queue)
        # cuDNN attention 对每个新输入形状需冷编译执行计划（新句子音素长度
        # 不同 ⇒ 每条新句首块 +~600ms）。decode 路径已被 CUDA Graph 冻结，
        # 此补丁只影响 eager prefill；换成 mem-efficient 后端冷热同速
        # （bench_shape_cold 实测：冷首块 719→119ms）。
        try:
            from torch.nn.attention import SDPBackend
            import gsv_tts.GPT_SoVITS.GPT.t2s_model as _t2s_mod
            _t2s_mod.SDPBACKEND = SDPBackend.EFFICIENT_ATTENTION
        except Exception as e:
            console.warn(f"SDPA 后端切换失败（保持默认）：{e}")
        self._ready = True
        console.ok(f"TTS 模型加载完成：{models_dir}")
        return True

    async def warmup(self) -> None:
        """启动预热：跑一遍完整流式合成链路（产物只走推理、不播放）。"""
        if not self._ready or self._tts is None:
            return
        try:
            await asyncio.to_thread(self._warmup_sync)
        except Exception as e:
            console.dim(f"TTS 预热失败（不影响后续合成）：{e}")

    def _warmup_sync(self) -> None:
        """多样化预热：覆盖常见句长/标点形态，建立 cudnn 规划与分配器形状池。

        只用单一短句预热时，真实对话的多样句子仍会走冷路径
        （GPT 首 yield 可恶化到 ~600ms/句）。
        """
        for _ in range(2):
            for text in _WARMUP_TEXTS:
                for _ in self._stream(text):
                    pass

    def preheat(self) -> None:
        """每轮对话起始调用：本地进程内推理，链路已由 start/warmup 激活，立即返回。"""
        return

    # ─────────────────────────── 合成 / 播放 ───────────────────────────

    async def speak(self, text: str, sfx: str = None) -> None:
        """把一段文本送入合成队列并立即返回（不阻塞 LLM 流）。

        未就绪时静默丢弃：上层 init_tts_async 失败会把 tts 置 None 走纯字幕降级；
        这里再兜一层（如就绪前的瞬时调用）。
        """
        text = (text or "").strip()
        if not text:
            return
        record_spoken(text)  # 回声防护：记录播报文本
        if self._tts is None or not self._ready:
            return
        self._speak_queue.put((text, sfx))
        self._ensure_pump()

    def _ensure_pump(self) -> None:
        """确保 pump 线程存在（空闲 0.5s 自动退出，需要时重建）。"""
        with self._pump_lock:
            if self._pump_thread is None or not self._pump_thread.is_alive():
                self._pump_thread = threading.Thread(
                    target=self._pump, daemon=True, name="TTSPump"
                )
                self._pump_thread.start()

    def _pump(self) -> None:
        """串行消费待合成句子：按入队顺序逐句流式合成并出声。"""
        _boost_thread_priority()
        while not self._interrupted.is_set():
            try:
                item = self._speak_queue.get(timeout=0.5)
            except Empty:
                return  # 空闲 0.5s 退出
            if item is None:
                return
            self._synth_one(item[0], item[1])

    def _stream(self, text: str):
        """创建一条 Token 级流式合成生成器（infer_stream）。

        配置了 GPTSOVITS_ROLE_GPT/SOVITS 时透传给 infer_stream：gsv-tts-lite
        首次合成时按需加载进权重缓存（预热阶段即完成），之后命中内存。
        """
        tts = self._tts
        return tts.infer_stream(
            spk_audio_path=self._speaker,
            prompt_audio_path=self._prompt_audio,
            prompt_audio_text=self._prompt_text,
            text=text,
            stream_mode="token",
            stream_chunk=_STREAM_CHUNK,
            overlap_len=_STREAM_OVERLAP,
            boost_first_chunk=True,
            return_subtitles=True,
            debug=False,
            gpt_model=self._role_gpt,        # None = 官方底模 s1v3.ckpt
            sovits_model=self._role_sovits,  # None = s2Gv2ProPlus.pth
            **_SYNTH_PARAMS,
        )

    def _synth_one(self, text: str, sfx: str = None) -> None:
        """（pump 线程）流式合成单句：每块写入 GSV AudioQueue 播放 + 字幕推送。"""
        tts = self._tts
        if tts is None:
            return
        # P2-4：快照代次号——被打断后 session 变化，本循环立即作废退出，
        # 不再依赖 _interrupted 标志（新轮 clear 会复位它导致旧句复播）
        session = self._session
        try:
            gen = self._stream(text)
            sid = self._player.begin_stream(tts.samplerate, text, gen)
            # 本句播放进度时钟：在首块 play()（投喂声卡）之前取锚，
            # 时钟 0 点 = 本句首样本开始被设备消费（上一句尾音/合成
            # 间隙不计入）→ 字幕按真实播放进度上屏，不抢跑
            clock = self._play_clock.sentence_clock() if self._play_clock else None
            chunk_idx = 0
            for audio in gen:
                if self._interrupted.is_set() or self._session != session:
                    break
                sr = int(getattr(audio, "samplerate", 0) or tts.samplerate)
                self._player.emit(audio, sr, text, getattr(audio, "subtitles", None), gen)
                self._player._emit_block(audio, sr, sid, chunk_idx, gen)
                chunk_idx += 1
                subs = getattr(audio, "subtitles", None)
                if subs:
                    self._subtitle_queue.add(subs, text, clock)
            else:
                self._skip_streak = 0  # 完整合成成功：清零连续跳句计数
        except Exception as e:
            # P1-7 修复：跳句必须醒目告警（原 console.dim 一闪而过，OOM 也静默）
            self._skip_streak += 1
            streak = self._skip_streak
            if streak >= 3:
                console.error(
                    f"⚠️ TTS 已连续 {streak} 句合成失败！本句跳过：{e}\n"
                    "  请检查 GPU 显存 / gsv-tts-lite 服务状态；"
                    "持续失败将导致直播全程无声")
                try:
                    self._subtitle_queue.push_text("（抱歉，我的语音连续出了点问题…）")
                except Exception:
                    pass
            else:
                console.warn(f"TTS 单句合成异常（跳过该句）：{e}")

    async def drain(self) -> None:
        """等待队列内全部句子合成完成，并等声卡把已入队音频播完。

        P1-6 修复：原实现只等合成不等播放——音频还在声卡队列里时
        字幕/状态已提前收尾（「无声剧」）。改用 gsv 原生 audio_queue.wait()
        （threading.Event，不触碰 sounddevice），并加 120s 超时兜底。
        """
        with self._pump_lock:
            pump = self._pump_thread
        if pump is not None and pump.is_alive():
            await asyncio.to_thread(pump.join, 120.0)
        with self._pump_lock:
            if self._pump_thread is pump:
                self._pump_thread = None
        # 等声卡播完（打断后 stop() 已置位 finished 事件，wait 立即返回）
        tts = self._tts
        if tts is not None and not self._interrupted.is_set():
            aq = getattr(tts, "audio_queue", None)
            if aq is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(aq.wait), timeout=120.0)
                except Exception:
                    pass
        # 整段说话结束：触发复原回调
        cb = self._on_play_done
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # ─────────────────────────── 打断 / 关闭 ───────────────────────────

    def interrupt(self) -> None:
        """打断当前播放：停合成 + 清空待合成队列 + 清空已入队音频。"""
        # P2-4：先作废所有在跑合成循环（session +1），再设标志——
        # 保证任何在块间检查点上的旧句合成都会退出，不复播
        self._session += 1
        self._interrupted.set()
        self._skip_streak = 0
        try:
            while True:
                self._speak_queue.get_nowait()
        except Empty:
            pass
        tts = self._tts
        if tts is not None:
            try:
                tts.audio_queue.stop()
            except Exception:
                pass
        self._subtitle_queue.flush()

    def clear_interrupt(self) -> None:
        """复位打断标志（新一轮输出前调用）。"""
        self._interrupted.clear()

    async def stop(self) -> None:
        """关闭引擎：打断、等待 pump 退出、清空声卡队列。"""
        self.interrupt()
        with self._pump_lock:
            pump = self._pump_thread
        if pump is not None and pump.is_alive():
            try:
                await asyncio.wait_for(asyncio.to_thread(pump.join, 10.0), timeout=15)
            except Exception:
                pass
        self._subtitle_queue.stop()
        # 运行期挂起的真实清理（见 _load_model 注释）：退出前归还显存
        if self._tts is not None:
            self._tts._empty_cache = self._tts.__dict__.get(
                "_orig_empty_cache", lambda: None
            )
            try:
                self._tts._orig_empty_cache()
            except Exception:
                pass
        self._ready = False

    # ─────────────────────────── 回调配置 ───────────────────────────

    def set_on_play_callback(self, cb: Optional[Callable]) -> None:
        """设置出声回调 on_play(wav, text, dur_s)（口型同步用）。"""
        self._player.set_on_play_callback(cb)

    def set_on_play_done_callback(self, cb: Optional[Callable]) -> None:
        """设置整段播放结束回调（说话结束复原用）。"""
        self._on_play_done = cb

    def set_subtitle_callback(self, cb: Optional[Callable]) -> None:
        """设置字幕推送回调（按字级时间戳逐字推送）。"""
        self._subtitle_queue.set_subtitle_callback(cb)

    # ─────────────────────────── 参考音频热更新 ───────────────────────────

    def apply_ref(self, audio: str, text: str) -> None:
        """热更新主参考音频 / 文本（下一句合成生效）。"""
        audio = (audio or "").strip()
        if audio:
            self._speaker = audio
            self._prompt_audio = audio
        text = (text or "").strip()
        if text:
            self._prompt_text = text

    def apply_ref_extras(self, extras: str) -> None:
        """热更新辅助参考音频（多条 | 分隔）。

        当前为最小实现：仅记录备用，多音色融合（spk_audio_path 传 dict）后续再做。
        """
        return


__all__ = [
    "TTSEngine",
    "_STREAM_CHUNK", "_STREAM_OVERLAP", "_SYNTH_PARAMS",
    "_wav_cache", "_cleanup_output", "evict_tts_cache",
]
