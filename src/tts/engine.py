"""TTS 引擎：本地加载 GSV-TTS-Lite 模型（进程内推理）+ 无缝播放 + 词级时间戳字幕。

严格参考 gsv-tts/API/test_async_performance.py 的用法：
- 直接实例化 gsv_tts.TTS（models_dir + gpt_cache + sovits_cache），进程内批量
  推理，不再连接外部 GPT-SoVITS HTTP 服务端（移除官方 api_v2.py 流式客户端）；
- LLM 产句积攒成批后调用 infer_batched_async 批量合成，返回与输入同序的
  AudioClip 列表，逐句按固定时长块入队 _AudioQueue 无缝播放；
- 合成参数逐一显式传递、与 gsv_tts.TTS.infer_batched_async 签名对齐
  （text_languages / prompt_languages / 采样参数等，默认值与 TTS.py 一致）；
- GSV-TTS-Lite 返回词级时间戳字幕（return_subtitles=True），逐词锚定到播放
  线程真实开播时刻渐进输出；字幕只用真实时间戳，绝不估算（缺时间戳的
  句子不输出字幕）。

本地用自实现的 _AudioQueue（单条 sounddevice OutputStream + 常驻消费线程）
做无缝播放，字幕锚定到播放线程真实开播时刻。

兼容旧模块接口（main.py / stream.py / cleaner.py 引用）：
start/stop/drain/speak/interrupt/clear_interrupt/set_on_play_callback/
set_subtitle_callback/apply_ref(_extras) + _wav_cache / _cleanup_output。
"""

import asyncio
import gc
import itertools
import os
import queue
import re
import sys
import tempfile
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

from src.utils import console

# ---- GSV-TTS-Lite 本地推理参数（参考 test_async_performance.py） ----
# 静态 CUDA graph 缓存：GPT 覆盖常用 (batch, seq_len) 组合，SoVITS 固定 50
_GPT_CACHE = [(b, c) for b in (1, 4, 8) for c in (512, 1024)]
_SOVITS_CACHE = [50]
# infer_batched_async 单批最大句子数（LLM 流同时积攒句子的上限）
_BATCH_MAX = 8
# 推理文本语言：auto 走 LangSegment 自动识别（中日英混合均适用）
_TEXT_LANG = "auto"
# 参考音频文本语言
_REF_LANG = "zh"

# 段间静音（秒）：长文本被标点切段后，段与段拼接处插入的停顿。
# GSV 默认 cut_mute=0.4 过长（句号处 0.6s），听感像一字一顿地读；
# 0.0 即不插人工静音，纯靠模型自带停顿。
_CUT_MUTE = 0.3
# 文本切段最短长度（GSV 默认值，低于此长度与前后段合并）
_CUT_MINLEN = 10
# 各标点对段间静音的倍率（相对 _CUT_MUTE；句末长停稍长、顿号稍短）
_CUT_MUTE_SCALE_MAP = {
    "…": 2.0, ".": 1.5, "。": 1.5, "?": 1.5, "？": 1.5, "!": 1.5, "！": 1.5,
    ",": 1.0, "，": 1.0, ":": 1.0, "：": 1.0, ";": 1.0, "；": 1.0, "~": 1.0,
    "、": 0.8, "・": 0.8,
}

# gsv_tts 包目录 / 默认模型目录（相对项目根；模型目录可被 GPTSOVITS_MODELS_DIR 覆盖）
_GSV_TTS_DIR = "gsv-tts"
_MODELS_DIR_REL = os.path.join(_GSV_TTS_DIR, "API", "models")

# 本地播放入队块长（秒）：整段音频按固定时长分块，块间由设备缓冲衔接无缝播放。
# 过大延迟首块出声，过小口型回调过于频繁，0.5s 为平衡值。
_PLAY_CHUNK_SEC = 0.5

# 多参考音频分隔符：GPTSOVITS_REF_AUDIOS 可配置多个路径，以 | 连接
# （控制中心支持拖拽多个音频文件，落地即 | 连接写入 .env）
_REF_AUDIO_SEP = "|"

# 纯符号/无实质内容碎片：直接丢弃，防 GPT-SoVITS 合成退化（"啊——"长音怪叫）
_HAS_CONTENT_RE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 兼容 cleaner.py：新引擎临时 wav 播放即删，无持久缓存可清
_wav_cache: dict = {}


def _cleanup_output() -> Tuple[int, int]:
    """兼容 cleaner.py 的旧清理入口：新引擎无 output 目录残留。"""
    return (0, 0)


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
                 on_block: Optional[Callable[[], None]] = None) -> None:
        self.samplerate = samplerate
        self.q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue()
        self.playback_finished = threading.Event()
        self.playback_finished.set()
        self._stopped = False       # close() 后线程退出
        self._on_block = on_block
        try:
            self.stream = sd.OutputStream(
                samplerate=samplerate, channels=1, dtype="float32")
            self.stream.start()
        except Exception:
            self.stream = None      # 无声卡/设备占用：仅消费队列不写设备
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def put(self, data: np.ndarray) -> None:
        """把一块 1D float32 音频入队，由常驻线程写入声卡。"""
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        self.q.put(data)
        self.playback_finished.clear()

    def _run(self) -> None:
        while not self._stopped:
            try:
                data = self.q.get(timeout=0.25)
            except queue.Empty:
                # 队列空：标记空闲（drain 可返回），继续等待新数据
                self.playback_finished.set()
                continue
            self.playback_finished.clear()
            try:
                if self._on_block is not None:
                    self._on_block()
                if self.stream is not None:
                    self.stream.write(data)
            except Exception:
                pass  # 打断等瞬态错误：丢弃该块，等待下一块
        self.playback_finished.set()

    def clear(self) -> None:
        """丢弃所有未播块并标记空闲（不碰 stream，避免打断卡死播放线程）。"""
        with self.q.mutex:
            self.q.queue.clear()
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
    """按时间戳渐进输出字幕（对齐真实播放时刻）。

    GSV-TTS-Lite 本地推理返回词级时间戳（subtitles: [{text, start_s, end_s,
    orig_idx_start, orig_idx_end}]，相对音频起点），逐词锚定到播放线程真实
    开播时刻推进：
    - 锚点 t0 = 句首块真实开播墙钟时刻（播放线程在首块 write 前写入）；
    - 每个词在 t0 + start_s 时刻把「原文累积文本」（前 orig_idx_end+1 个
      字符）交给 sink（本项目为 sub.push("text", ...)），气泡/网页随之
      逐字浮现；
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
        self._closed = False          # 打断：终止当前字幕线程（下次 add 重建）

    def process(self) -> None:
        """字幕处理主循环：按词级时间戳逐词渐进输出（追赶式，不丢字）。"""
        while True:
            item = self.q.get()
            if item is None:
                break
            text, subtitles, sent_id = item
            self._run_timestamps(text, subtitles, sent_id)
        self.t = None

    def _run_timestamps(self, text: str, subtitles: list, sent_id: int) -> None:
        """按词级时间戳推进：每个词在 t0+start_s 推送原文累积文本。"""
        if not text:
            return
        # 句首块：等待播放线程真正开播（write 前触发 anchor）
        while sent_id not in self._anchors:
            if self._closed:
                self.t = None
                return
            time.sleep(0.005)
        with self._anchors_lock:
            t0 = self._anchors[sent_id]
        idx = 0
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
                time.sleep(min(0.02, target - time.time()))
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

    def add_clip(self, text: str, subtitles: list, sent_id: int) -> None:
        """添加一句字幕：只接受 GSV-TTS-Lite 的真实词级时间戳（相对音频起点）。

        缺少时间戳的句子不输出字幕（宁缺毋假），绝不按时长估算。
        """
        if not text or not subtitles:
            return
        self.q.put((text, subtitles, sent_id))
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
        with self.q.mutex:
            self.q.queue.clear()
        self.q.put(None)  # 终止信号：process 收到后退出


class TTSEngine:
    """GSV-TTS-Lite 本地推理 TTS 引擎（进程内加载模型，批量合成 + 无缝播放）。

    参考 test_async_performance.py：TTS(models_dir, gpt_cache, sovits_cache)
    一次构造即完成模型加载，合成走 infer_batched_async（内部持线程锁互斥，
    阻塞推理自动放 executor），进程内推理不依赖任何外部 TTS 服务端。
    """

    def __init__(self) -> None:
        from src.utils import config as _config
        # 保留主/辅助参考的原始配置串：热更新任一侧时用另一侧重算合成
        self._ref_main = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIO", "") or "").strip()
        self._ref_extras = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIOS", "") or "").strip()
        self.ref_text = str(
            getattr(_config.cfg, "GPTSOVITS_PROMPT_TEXT", "") or "").strip()
        # 本地模型目录：GPTSOVITS_MODELS_DIR 可覆盖，缺省 gsv-tts/API/models
        models_dir = str(
            getattr(_config.cfg, "GPTSOVITS_MODELS_DIR", "") or "").strip()
        if models_dir and not os.path.isabs(models_dir):
            models_dir = os.path.join(_config.cfg.PROJECT_ROOT, models_dir)
        self._models_dir = models_dir or os.path.join(
            _config.cfg.PROJECT_ROOT, _MODELS_DIR_REL)

        self._tts: Optional[object] = None  # gsv_tts.TTS 实例（start() 时在 executor 中加载）
        self._queue: Optional[_AudioQueue] = None  # 无缝播放队列（按首个音频采样率懒创建）
        self._on_play: Optional[Callable[[str, str, float], None]] = None
        self._sub_sink: Optional[Callable[[str], None]] = None
        self._sub_q: Optional[_SubtitlesQueue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._gen = 0                        # 代次号：interrupt 递增，合成循环检测后提前退出
        self._interrupted = False            # interrupt 标志（clear_interrupt 复位）
        self._sub_mute = False               # 打断后抑制字幕线程的残留推送
        self._play_metas: "queue.Queue[Tuple[str, str, float, int, int]]" = queue.Queue()
        self._sent_seq = itertools.count(1)  # 句子序号：字幕锚点/游标按句隔离
        self._pending: Optional[asyncio.Queue] = None  # 待合成句子队列
        self._pump_task: Optional[asyncio.Task] = None  # 串行合成泵
        self._working = False                # 当前是否正在合成（drain 等待用）
        self._tmp_dir = tempfile.mkdtemp(prefix="vtuber_tts_")

    # ---------- 参考音频（主参考 + 辅助参考多音频混合） ----------

    def _ref_params(self) -> Tuple[object, str, str]:
        """GSV-TTS-Lite 参考音频参数：(spk_audio_path, prompt_audio_path, prompt_text)。

        多参考时 prompt 发音/文本参考固定取第一条（主参考），其余作为
        多说话人音色融合的辅助参考：spk 传 {路径: 权重} 字典（GSV-TTS-Lite
        按权重融合说话人音色，与旧官方 API 的 aux_ref_audio_paths 等价）。
        """
        paths = [p.strip() for p in
                 [self._ref_main] + str(self._ref_extras).split(_REF_AUDIO_SEP)
                 if p.strip()]
        if not paths:
            return "", "", ""
        spk = paths[0] if len(paths) == 1 else {p: 1.0 for p in paths}
        return spk, paths[0], self.ref_text

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """加载 GSV-TTS-Lite 本地模型 + 参考音频预编码 + 初始化字幕管线。"""
        if not self._ref_params()[0]:
            console.warn("TTS：未配置 GPTSOVITS_REF_AUDIO，语音合成关闭")
            return False
        self._loop = asyncio.get_running_loop()
        # 模型加载 / 参考音频编码都是阻塞操作（CUDA graph 编译等），放
        # executor 执行避免卡住事件循环（对齐 test_async_performance.py：
        # TTS(models_dir, gpt_cache, sovits_cache) 一次构造即完成加载）。
        try:
            self._tts = await asyncio.get_running_loop().run_in_executor(
                None, self._load_tts)
        except Exception as e:
            console.warn(
                f"TTS：GSV-TTS-Lite 本地模型加载失败（{e}）——"
                f"请确认 {self._models_dir} 下模型完整（s1v3.ckpt / s2Gv2ProPlus.pth / chinese-hubert-base）")
            return False
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._warmup_ref)
        except Exception as e:
            console.warn(
                f"TTS：参考音频预编码失败（{e}）——"
                f"请检查 GPTSOVITS_REF_AUDIO(s) / GPTSOVITS_PROMPT_TEXT 配置")
            self._tts = None
            return False
        self._sub_q = _SubtitlesQueue(self._push_subtitle)
        self._pending = asyncio.Queue()
        console.ok("TTS 引擎就绪（GSV-TTS-Lite 本地推理 + 无缝播放 + 词级时间戳字幕）")
        return True

    def _load_tts(self) -> object:
        """在 executor 中加载 GSV-TTS-Lite 模型（构造 + 显式加载 GPT/SoVITS 权重）。"""
        # 与 test_async_performance.py 一致的导入方式：先注入包路径再
        # from gsv_tts import TTS（该包不在默认 sys.path 上，必须懒加载）
        from src.utils import config as _config
        gsv_pkg = os.path.join(_config.cfg.PROJECT_ROOT, _GSV_TTS_DIR)
        if gsv_pkg not in sys.path:
            sys.path.insert(0, gsv_pkg)
        from gsv_tts import TTS
        # use_bert=True：中文走真实 BERT 语义特征（s1v3/s2Gv2ProPlus 模型带
        # BERT 训练；缺省 False 时中文 BERT 特征填全零，语调发平像机械朗读）。
        # GPU 下首次启动自动从 ModelScope 下载 chinese-roberta-wwm-ext-large
        # （约 1.2GB，models_dir/chinese-roberta-wwm-ext-large）。
        tts = TTS(models_dir=self._models_dir,
                  gpt_cache=_GPT_CACHE, sovits_cache=_SOVITS_CACHE,
                  use_bert=True)
        # 显式加载默认权重（models_dir/s1v3.ckpt、models_dir/s2Gv2ProPlus.pth）
        tts.load_gpt_model()
        tts.load_sovits_model()
        console.ok("TTS 模型加载完成（GPT + SoVITS + CUDA graph 已编译）")
        return tts

    def _warmup_ref(self) -> None:
        """预编码参考音频（CNHubert/ERes2Net 一次性加载），首句即出声。"""
        spk, prompt, text = self._ref_params()
        self._tts.cache_prompt_audio(
            prompt_audio_paths=prompt, prompt_audio_texts=text,
            prompt_language=_REF_LANG)
        if isinstance(spk, dict):
            self._tts.cache_spk_audio(*spk.keys())
        else:
            self._tts.cache_spk_audio(spk)
        console.ok("TTS 参考音频预编码完成")

    def _ensure_queue(self, sr: int) -> None:
        """按服务端实际采样率懒创建播放队列（GSV-TTS-Lite 固定 32000）。

        首个音频块到达时创建并挂载常驻播放线程；之后所有块共用同一输出流
        无缝播放。
        """
        if self._queue is not None:
            return
        self._queue = _AudioQueue(sr, self._on_block_play)

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

    async def stop(self) -> None:
        """停止：打断播放/合成、释放本地模型与输出流（进程退出前调用）。"""
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
                self._queue.close()  # 停消费线程 + 关输出流
            except Exception:
                pass
            self._queue = None
        # 释放本地模型 + 显存缓存
        self._tts = None
        try:
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
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

        常驻播放线程下用 playback_finished（队列空 0.25s 后置位）判定播完，
        天然覆盖尾部静默——比「队列空 && _active 归零」更稳：不会在最后一块
        刚出队、_active 尚未自增的瞬间误判播完而提前返回。
        """
        # 等串行泵消费完所有待合成句子（播放队列懒创建，先等合成再判队列）
        while self._working or (
                self._pending is not None and not self._pending.empty()):
            # speak() 与泵「空闲退出」存在竞态：句子入队的瞬间泵刚好判空退出，
            # speak 侧看到泵未 done 不再重启 → 句子永远无人合成。检测到
            # 「还有句子但泵已死」立即重启泵，杜绝 drain 死等 + 回复尾句丢失。
            if (self._pending is not None and not self._pending.empty()
                    and (self._pump_task is None or self._pump_task.done())):
                self._pump_task = asyncio.create_task(self._pump())
            await asyncio.sleep(0.05)
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=300)
            except Exception:
                pass
        # 等播放线程把队列全部写完（playback_finished 在队列空 0.25s 后置位）
        aq = self._queue
        if aq is not None:
            for _ in range(6000):  # 最长等 300s
                if aq.playback_finished.is_set():
                    break
                await asyncio.sleep(0.05)
        # 字幕队列与播放基本同步，给残留尾字推送一点收尾时间
        await asyncio.sleep(0.3)

    # ---------- 对外接口（main.py / stream.py 调用） ----------

    def set_on_play_callback(self, cb: Optional[Callable[[str, str, float], None]]) -> None:
        """设置音频开始播放回调（wav 路径, 文本, 时长秒）——口型同步用。"""
        self._on_play = cb

    def set_subtitle_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        """设置字幕回调：逐字推进时调用（入参为累积文本）。"""
        self._sub_sink = cb

    def apply_ref(self, audio: str, text: str) -> None:
        """热更新参考音频/文本（控制中心 !tts_audio / !tts_text 热调用）。

        仅更新主参考，辅助参考沿用当前值，互不覆盖。
        """
        self._ref_main = (audio or "").strip()
        self.ref_text = (text or "").strip()

    def apply_ref_extras(self, extras: str) -> None:
        """热更新辅助参考音频（控制中心 !tts_audios 热调用，多条 | 分隔）。"""
        self._ref_extras = (extras or "").strip()

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

        sounddevice 的 abort()/stream.stop() 会令「正阻塞在 stream.write() 上
        的常驻播放线程」永久卡死：设备层还在播放当前块（MME 错误），write
        永不返回、start 必然失败，打断后的新块全部堆积无人播放。因此打断只
        清队列——正在写的那块自然播完（约 0.9s 尾音），线程回到 q.get()
        等待，新句块随后正常续播。
        """
        if self._queue is not None:
            self._queue.clear()

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志（main.py 每轮调用）。"""
        self._interrupted = False
        self._sub_mute = False

    # ---------- 合成（GSV-TTS-Lite 本地批量推理，串行泵） ----------

    async def speak(self, text: str) -> None:
        """把一句话送入合成队列（立即返回，不阻塞 LLM 流）。

        串行泵按句顺序批量合成；infer_batched_async 内部持线程锁互斥、
        阻塞推理自动放 executor，天然保序，首句延迟仅为首批合成耗时。
        """
        if (self._tts is None or self._pending is None
                or self._interrupted):
            return
        text = (text or "").strip()
        if not text or not _HAS_CONTENT_RE.search(text):
            return  # 纯符号碎片：防 GPT-SoVITS 合成退化
        await self._pending.put(text)
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """串行消费待合成句子；收拢当前积攒的句子成批合成，空闲时退出。"""
        while True:
            text = await self._pending.get()
            if self._interrupted:
                continue  # 丢弃打断后的残留句子
            gen = self._gen
            # 收拢当前待合成句子（最多 _BATCH_MAX 条一批，走批量推理）
            texts = [text]
            while len(texts) < _BATCH_MAX:
                try:
                    texts.append(self._pending.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._working = True
            try:
                await self._synth_local(texts, gen)
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

    async def _synth_local(self, texts: list, gen: int) -> None:
        """infer_batched_async 批量合成（返回与输入同序的 AudioClip），逐句入队播放。

        参数逐一显式传递、与 gsv_tts.TTS.infer_batched_async 签名对齐（默认值
        与 TTS.py 一致；仅 return_subtitles / cut_mute / 语言按本引擎需要调整）。
        对齐 test_async_performance.py：一次批量推理覆盖多句，GPU 批次化
        摊薄每句的固定开销；打断（gen 变化）时放弃剩余音频。
        """
        spk, prompt, text = self._ref_params()
        clips = await self._tts.infer_batched_async(
            spk_audio_paths=spk,
            prompt_audio_paths=prompt,
            prompt_audio_texts=text,
            texts=texts,
            text_languages=_TEXT_LANG,
            prompt_languages=_REF_LANG,
            return_subtitles=True,
            is_cut_text=True,
            cut_minlen=_CUT_MINLEN,
            cut_mute=_CUT_MUTE,
            cut_mute_scale_map=_CUT_MUTE_SCALE_MAP,
            # 以下采样/批参数均为 TTS.py 默认值（显式写出，便于按需调整）
            top_k=15, top_p=1.0, temperature=1.0, repetition_penalty=1.35,
            noise_scale=0.5, speed=1.0,
            bert_batch_size=20, sovits_batch_size=10,
        )
        for clip_text, clip in zip(texts, clips):
            if gen != self._gen:
                return  # 打断：放弃剩余音频
            self._emit_clip(clip, clip_text, gen)

    def _emit_clip(self, clip, text: str, gen: int) -> None:
        """单个 AudioClip：按固定时长块入队无缝播放 + 口型临时 wav + 字幕。

        播放元信息（_play_metas）无条件入队：播放线程靠它给句首块锚定字幕
        时间轴（anchor），即使未设口型回调（_on_play 为 None）也必须入队，
        否则字幕线程在 process() 等不到锚点会一直死等。
        """
        sr = int(getattr(clip, "samplerate", 0)) or 32000
        try:
            self._ensure_queue(sr)
        except Exception as e:
            console.dim(f"TTS 播放队列初始化失败（口型/字幕可能不可用）：{e}")
            return
        sent_id = next(self._sent_seq)
        audio = np.asarray(clip.audio_data, dtype=np.float32).reshape(-1)
        frames_per_chunk = max(1, int(_PLAY_CHUNK_SEC * sr))
        chunk_idx = 0
        for start in range(0, len(audio), frames_per_chunk):
            if gen != self._gen:
                return  # 打断：放弃剩余音频
            chunk = audio[start:start + frames_per_chunk]
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
                return
            chunk_idx += 1
        # 字幕：GSV-TTS-Lite 返回的真实词级时间戳，锚定播放时刻逐词推进
        if self._sub_q is not None:
            try:
                subtitles = list(getattr(clip, "subtitles", None) or [])
                self._sub_q.add_clip(text, subtitles, sent_id)
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
