"""TTS 引擎：连接外部 GPT-SoVITS HTTP 服务端（tools/gsv_tts/API/fastapi_server_example.py）。

主程序不再进程内加载模型，改由 tts.bat 启动的外部服务负责合成，本引擎作为
客户端使用：
- 待合成句子入队，串行泵按句 POST /tts/stream Token 级流式合成（服务端
  复用官方 infer_stream，GPT 按 stream_chunk 累积即解码出块），边收 int16
  PCM 边播放——首块到达即出声，首字延迟只受「首块合成」限制；
- 词级时间戳字幕随流按块返回（相对整句起点），逐块入队锚定真实开播时刻
  逐词推进（官方示例 subtitlesqueue.add 的 1:1 语义）；
- 流式失败或音频退化时回退 /tts/batch 整句合成（_synth_one 重试兜底），
  磁盘缓存命中时直接整句播放（缓存无词级时间戳，字幕降级整句显示）；
- 未连接服务时 start() 返回 False，speak/drain/stop 静默降级（主程序只走字幕）。

兼容旧模块接口（main.py / stream.py / cleaner.py 引用）：
start/stop/drain/speak/interrupt/clear_interrupt/set_on_play_callback/
set_subtitle_callback/apply_ref(_extras) + _wav_cache / _cleanup_output。
"""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
from typing import Optional, Tuple

import httpx
import numpy as np
import soundfile as sf

from src.utils import console
from src.tts.player import TTSPlayer
from src.adapter.tts import BaseTTSAdapter

# 多参考音频分隔符：GPTSOVITS_REF_AUDIOS 可配置多个路径，以 | 连接
# （控制中心支持拖拽多个音频文件，落地即 | 连接写入 .env）
_REF_AUDIO_SEP = "|"

# 实质内容字符集合：\w 匹配字母/数字/下划线（Python3 Unicode 模式下含
# 中文等文字），显式补 CJK/日/韩范围兜底。判断一句是否含可合成内容——
# 含任一有效字符即保留（"啊——"含"啊"会保留）；只有纯符号/空白碎片
# （如"……""——"）才丢弃，防 GPT-SoVITS 合成退化（长音怪叫）。
_HAS_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 防合成退化的文本清洗正则：
# 1) URL：GPT-SoVITS 会逐个念出协议/域名符号，拖出怪叫
_URL_RE = re.compile(r"https?://[^\s，。！？、；：]+", re.IGNORECASE)
# 2) 颜文字/纯符号括号块（如 (￣▽￣)、(=ω=)）：GSV 服务端合成直接 500
#    报错；括号内含中文/数字/字母的（如 （狗头））不删，保留语义
_KAOMOJI_RE = re.compile(r"[（(][^（）()0-9A-Za-z\u4e00-\u9fff]*[)）]")
# 3) 重复标点（！！！/。。。/～～）：连续符号易被合成为拖长音
_REPEAT_PUNCT_RE = re.compile(r"([！？。，、；：～~])\1+")
# 4) 连续相同字母/数字/中文 ≥4（哈哈哈哈、66666666、hhhhhh）：\w 在
#    Unicode 模式下同时匹配中文与字母数字，统一折叠为两个——连续同音素
#    是 GSV 拖长音怪叫的主要诱因（长数字串还会被逐位念成拖音）
_REPEAT_ALNUM_RE = re.compile(r"(\w)\1{3,}")
# 5) 重复音节块 ≥3（hahaha、hehehe）：交替式重复（"ha"×3）也易合成
#    循环怪音，折叠为两个音节块；英文实词几乎不存在 3 连重复块，不误伤
_REPEAT_SYLLABLE_RE = re.compile(r"([A-Za-z]{1,3})\1{2,}")

# 合成退化兜底检测（防随机怪叫的最后一环）：
# GPT 采样偶发崩坏时（实测约 1/4~1/8 概率）会合成出 ~28s 的循环怪音，
# 与文本内容无关、清洗无法根治。下载后按「文本短但音频超长」判据拦截，
# 重试一次（GSV 采样随机，重试大概率正常）仍异常则丢弃该句。
_MAX_DEGRADED_SEC = 25.0  # 绝对上限：正常句几乎不可能超过（~80 字才 ~20s）
_MAX_SEC_PER_CHAR = 1.0  # 相对判据：正常语速 ~0.3s/字，1s/字+ 必为拖长音
_MIN_DEGRADED_SEC = 8.0  # 短文本相对判据的最小绝对时长（防误杀短句）

# 合成退化兜底检测之二（尖峰噪声）：
# GPT 流式采样偶发崩坏还会产出「交替满幅振荡」的高频噪声段（相邻采样 ±1
# 快速翻转，即 16kHz 方波嘶声）——时长正常但波形崩坏，纯时长判据查不出。
# 判据：相邻采样差 >0.5 的采样占比 >1% 即判退化。正常语音 32kHz 下该占比
# <0.1%；崩坏段几乎全部采样满足（实测整段占比 9%+），有量级余量。
_BURST_DIFF_THRESH = 0.5  # 相邻采样差阈值：正常语音几乎不可能超过
_BURST_RATE_MAX = 0.01  # 跳变采样占比上限，超过即判退化


def _is_degraded_audio(dur_s: float, text_len: int) -> bool:
    """判断合成音频是否退化（拖长音怪叫）：时长异常地远超文本长度。"""
    return dur_s > _MAX_DEGRADED_SEC or (
        dur_s > _MIN_DEGRADED_SEC and dur_s / max(1, text_len) > _MAX_SEC_PER_CHAR
    )


def _has_burst_noise(audio: np.ndarray) -> bool:
    """判断音频是否含交替满幅振荡崩坏段（高频嘶声）：跳变采样占比超限。

    audio：1D float32。过短（<100 采样）不判，避免小块误报。
    """
    if audio.size < 100:
        return False
    d = np.abs(np.diff(audio))
    return float((d > _BURST_DIFF_THRESH).mean()) > _BURST_RATE_MAX


def _collapse_tts_text(text: str) -> str:
    """合成前压缩易致怪叫的文本：去 URL/颜文字、折叠重复标点/语气字/音节。"""
    if not text:
        return text
    text = _URL_RE.sub(" ", text)
    text = _KAOMOJI_RE.sub("", text)
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)
    text = _REPEAT_SYLLABLE_RE.sub(r"\1\1", text)
    text = _REPEAT_ALNUM_RE.sub(r"\1\1", text)
    return text


def _decode_wav_bytes(content: bytes) -> Tuple[np.ndarray, int]:
    """线程池执行的 wav 解码：从字节流还原 (1D ndarray, 采样率)。

    soundfile.read 在长 wav 上 ~5-15ms，是阻塞 syscall + numpy 解析，
    必须放 to_thread 避免卡住 asyncio 事件循环。
    """
    data, sr = sf.read(io.BytesIO(content), dtype="float32")
    return data, int(sr)


def _encode_wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    """把 1D float32 音频编码为 wav 字节流（流式整句写磁盘缓存用）。"""
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    return buf.getvalue()


# 服务端默认地址（fastapi_server_example.py 监听 0.0.0.0:8000），可用
# TTS_SERVER_URL 覆盖
_SERVER_DEFAULT_URL = "http://127.0.0.1:8000"

# ---------- 磁盘音频缓存（相同文本+参考+参数只合成一次） ----------
#
# 直播中同一句高频话（打招呼/口头禅）会反复触发合成，服务端 GPU 推理
# 是主要开销。以「文本+参考音频+参考文本+合成参数」的 md5 为键缓存 wav
# 到 data/tts_cache/，命中直接解码播放，跳过 HTTP 合成。
# 缓存是优化不是依赖：读写/清理任何失败都静默降级，不影响正常合成。
_TTS_CACHE_SUBDIR = "tts_cache"
_TTS_CACHE_TTL_SEC = 7 * 24 * 3600  # 7 天过期（参考音频变更由 key 自然失效）
_TTS_CACHE_MAX_BYTES = 512 * 1024 * 1024  # 容量上限 512MB，超出按 mtime 淘汰最旧
_CACHE_EVICT_EVERY = 64  # 每写入 64 次触发一次容量清理

_cache_write_count = 0


def _tts_cache_dir() -> str:
    """磁盘缓存目录（<DATA_ROOT>/tts_cache），懒创建；失败返回空串（禁用缓存）。"""
    from src.utils import config as _config

    root = getattr(_config.cfg, "DATA_ROOT", "") or os.getcwd()
    d = os.path.join(root, _TTS_CACHE_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return d


def _tts_cache_key(
    text: str, speaker_audio: str, prompt_audio: str, prompt_text: str
) -> str:
    """缓存键：文本 + 参考参数 + 合成参数（排序序列化，保证确定性）。"""
    payload = {
        "text": text,
        "speaker_audio": speaker_audio,
        "prompt_audio": prompt_audio,
        "prompt_text": prompt_text,
        **_SYNTH_PARAMS,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_load(key: str) -> Optional[bytes]:
    """读取缓存 wav 字节；TTL 过期 / 读取失败视为未命中（过期即删）。"""
    d = _tts_cache_dir()
    if not d:
        return None
    path = os.path.join(d, f"{key}.wav")
    try:
        if time.time() - os.path.getmtime(path) > _TTS_CACHE_TTL_SEC:
            os.remove(path)
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _cache_delete(key: str) -> None:
    """删除单条缓存（退化产物 / 命中后检测异常时清理）。"""
    d = _tts_cache_dir()
    if not d:
        return
    try:
        os.remove(os.path.join(d, f"{key}.wav"))
    except OSError:
        pass


def _cache_save(key: str, content: bytes) -> None:
    """写入缓存：先写临时文件再 os.replace（原子，防半截 wav 被读到）；
    周期性触发容量清理，防长会话缓存无限膨胀。失败静默。"""
    global _cache_write_count
    d = _tts_cache_dir()
    if not d or not content:
        return
    tmp = os.path.join(d, f"{key}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, os.path.join(d, f"{key}.wav"))
        _cache_write_count += 1
        if _cache_write_count % _CACHE_EVICT_EVERY == 0:
            evict_tts_cache()
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def evict_tts_cache() -> Tuple[int, int]:
    """清理磁盘音频缓存：删 TTL 过期文件；超容量按 mtime 淘汰最旧。

    幂等、失败静默（缓存是优化不是依赖）。启动后台调用 + 写入周期检查共用。
    Returns: (删除文件数, 释放字节数)。
    """
    d = _tts_cache_dir()
    if not d:
        return (0, 0)
    entries: list[Tuple[float, str, int]] = []
    try:
        for name in os.listdir(d):
            if not name.endswith(".wav"):
                continue
            path = os.path.join(d, name)
            try:
                entries.append((os.path.getmtime(path), path, os.path.getsize(path)))
            except OSError:
                continue
    except OSError:
        return (0, 0)
    now = time.time()
    removed: list[str] = []
    freed = 0
    # 1) TTL 过期
    keep: list[Tuple[float, str, int]] = []
    for mtime, path, size in entries:
        if now - mtime > _TTS_CACHE_TTL_SEC:
            removed.append(path)
            freed += size
        else:
            keep.append((mtime, path, size))
    # 2) 超容量：按最旧优先淘汰，直到低于上限
    total = sum(size for _, _, size in keep)
    if total > _TTS_CACHE_MAX_BYTES:
        for mtime, path, size in sorted(keep):  # mtime 升序 = 最旧在前
            if total <= _TTS_CACHE_MAX_BYTES:
                break
            removed.append(path)
            freed += size
            total -= size
    for path in removed:
        try:
            os.remove(path)
        except OSError:
            pass
    return (len(removed), freed)


# 泵单次收拢句数：流式合成按句独立请求（/tts/stream 单句），收拢多句
# 无意义，固定 1 = 每句独立合成，一句失败只影响自身（重试/丢弃兜底）。
_BATCH_MAX = 1

# 流式合成参数（服务端 /tts/stream 透传到 infer_stream）：与官方示例一致
# 的 stream_chunk=25/overlap_len=5——GPT 累积 25 个 token 即解码出一块音频，
# 首块快速产出降低首字延迟；overlap_len 个 token 重叠用于块间平滑混音。
_STREAM_CHUNK = 25
_STREAM_OVERLAP = 5

# 合成参数（服务端 /tts/batch 透传到 infer_batched_async）：
# 采用 GSV-TTS-Lite 官方 API 文档推荐值——top_k=5/top_p=0.9 比库默认
# top_k=15/top_p=1.0 采样更收敛，配合 repetition_penalty 抑制循环怪音。
_SYNTH_PARAMS = {
    "top_k": 5,
    "top_p": 0.9,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "noise_scale": 0.5,
    "speed": 1.0,
}

# 兼容 cleaner.py：新引擎临时 wav 播放即删，无持久缓存可清
_wav_cache: dict = {}


def _cleanup_output() -> Tuple[int, int]:
    """兼容 cleaner.py 的旧清理入口：新引擎无 output 目录残留。"""
    return (0, 0)


class TTSEngine(BaseTTSAdapter):
    """GPT-SoVITS HTTP 服务端客户端引擎（合成在独立进程，本引擎只播放）。

    服务端为 tools/gsv_tts/API/fastapi_server_example.py（tts.bat 启动）：
    - /tts/stream Token 级流式合成（官方 infer_stream），SSE 返回音频块
      + 词级时间戳，首块到达即播放；
    - /tts/batch 整句批量合成，返回 output 目录下的文件名列表；
    - /audio/{filename} 下载 wav 字节流（批量路径兜底用）。
    未连接服务时 start() 返回 False，speak 等接口静默降级（不报错）。
    """

    def __init__(self) -> None:
        from src.utils import config as _config

        # 保留主/辅助参考的原始配置串：热更新任一侧时用另一侧重算合成
        self._ref_main = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIO", "") or ""
        ).strip()
        self._ref_extras = str(
            getattr(_config.cfg, "GPTSOVITS_REF_AUDIOS", "") or ""
        ).strip()
        self.ref_text = str(
            getattr(_config.cfg, "GPTSOVITS_PROMPT_TEXT", "") or ""
        ).strip()
        self._server_url = (
            str(getattr(_config.cfg, "TTS_SERVER_URL", "") or "").strip()
            or _SERVER_DEFAULT_URL
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._ready = False  # start() 探测服务成功后置位
        self._player = TTSPlayer()  # 播放/口型/字幕公共逻辑
        self._gen = 0  # 代次号：interrupt 递增，泵循环检测后提前退出
        self._interrupted = False  # interrupt 标志（clear_interrupt 复位）
        self._pending: Optional[asyncio.Queue] = None  # 待合成句子队列
        self._pump_task: Optional[asyncio.Task] = None  # 串行合成泵
        self._working = False  # 当前是否正在合成（drain 等待用）

    # ---------- 参考音频（主参考 + 辅助参考多音频混合） ----------

    def _ref_params(self) -> Tuple[str, str, str]:
        """参考参数：(speaker_audio, prompt_audio, prompt_text)。

        fastapi_server_example.py 服务端仅支持单说话人（speaker_audio +
        prompt_audio + prompt_text），主参考同时作为音色与发音参考；
        辅助参考（多说话人融合）服务端不支持，此处忽略。
        """
        main = (self._ref_main or "").strip()
        return main, main, (self.ref_text or "").strip()

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """探测服务端可用性（未配置参考音频或服务未启动时返回 False）。"""
        if not self._ref_params()[0]:
            console.warn("TTS：未配置 GPTSOVITS_REF_AUDIO，语音合成关闭")
            return False
        self._player.set_loop(asyncio.get_running_loop())
        # 连接池显式调小：TTS 客户端固定串行泵 + 单下载协程（_BATCH_MAX=1），
        # 不需要大池；反而 keep-alive 太长会让服务端 TCP 表膨胀、长时间无
        # 通信时首包延迟抖动。max_keepalive=4 + keepalive_expiry=30s：
        # - 短轮转内（30s）连接复用 → 省 TCP/TLS 握手；
        # - 30s+ 没合成就放掉 → 长时间不播后首句延迟可控。
        # max_connections=8 兜底：极端弹幕 burst 下 pump/preheat 并发拿连接不阻塞。
        self._client = httpx.AsyncClient(
            timeout=120.0,
            limits=httpx.Limits(
                max_connections=8,
                max_keepalive_connections=4,
                keepalive_expiry=30.0,
            ),
        )
        try:
            resp = await self._client.get(f"{self._server_url}/")
            resp.raise_for_status()
        except Exception as e:
            console.warn(
                f"TTS：无法连接服务端 {self._server_url}（{e}）——"
                f"请先运行 tts.bat 启动 GPT-SoVITS 服务，语音将降级为纯字幕"
            )
            await self._close_client()
            return False
        self._pending = asyncio.Queue()
        self._ready = True
        console.ok(f"TTS 服务端已连接（{self._server_url}）")
        # 启动后后台清理过期/超容量磁盘音频缓存（不阻塞启动，失败静默）
        asyncio.create_task(asyncio.to_thread(evict_tts_cache))
        # 【客户端播放链路预热】懒创建的 sd.OutputStream 首次初始化要
        # 50-200ms（声卡驱动握手），让首句不必再等 stream 冷启动。
        # 用一段 0.05s 静音触发：player 链路全部激活，但用户听不到。
        # ⚠️ sr 必须用 GSV-TTS-Lite 真实输出采样率（32000）——_ensure_queue
        # 首次调用按此 sr 锁死 stream，之后所有音频按此 sr 播放，错了会变调/变速。
        try:
            import numpy as _np

            sr_warm = (
                32000  # GSV-TTS-Lite 输出采样率（player.py 注释也写了「固定 32000」）
            )
            self._player._ensure_queue(sr_warm)
            warmup = _np.zeros(int(0.05 * sr_warm), dtype=_np.float32)
            self._player._queue.put(warmup.reshape(-1, 1))
        except Exception:
            pass  # 预热失败静默：首句照常走（多 50-200ms 冷启动）
        return True

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def warmup(self) -> None:
        """预热：启动时一次性完成 服务端流式合成 链路。

        服务端模型在 startup 只加载权重，首次合成才编译推理图（流式走
        infer_stream 路径单独建图）；预热把这份开销提前消化，之后主播
        第一次说话不必再等（顺带烫热 HTTP 连接池）。失败静默降级。
        """
        if not self._ready or self._client is None:
            return
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        if not speaker_audio:
            return
        try:
            payload = {
                "text": "你好呀",
                "speaker_audio": speaker_audio,
                "prompt_audio": prompt_audio,
                "prompt_text": prompt_text,
                "stream_chunk": _STREAM_CHUNK,
                "overlap_len": _STREAM_OVERLAP,
                **_SYNTH_PARAMS,
            }
            async with self._client.stream(
                "POST", f"{self._server_url}/tts/stream", json=payload
            ) as resp:
                resp.raise_for_status()
                async for _ in resp.aiter_lines():
                    pass  # 消费完所有块即完成预热（不播放）
            console.dim("[TTS] 服务端预热完成（流式合成链路已就绪）")
        except Exception as e:
            console.dim(f"[TTS] 预热失败（不影响使用）：{e}")

    def preheat(self) -> None:
        """每轮对话起始预热：仅本地播放链路，不触网、不合成。

        旧版会后台合成「。」与 LLM 第一句并发抢服务端资源，导致首句
        偶发 500；HTTP 合成/下载/解码链路已收敛到启动时 warmup() 一次性
        预热。这里只做 sounddevice 播放链路的轻量激活（本地零并发风险），
        立即返回不阻塞 LLM。
        """
        if not self._ready or self._client is None or self._interrupted:
            return
        try:
            # 本地播放链路预热：确保 OutputStream/播放线程已激活。
            # 首次创建要 50-200ms（声卡驱动握手），之后为廉价 no-op；
            # 静音块照常写入设备（听不到），不触发字幕/口型回调。
            import numpy as _np

            self._player._ensure_queue(32000)  # GSV-TTS-Lite 输出采样率
            warmup = _np.zeros(int(0.05 * 32000), dtype=_np.float32)
            self._player._queue.put(warmup.reshape(-1, 1))
        except Exception:
            pass  # 预热失败静默：首句照常走（多 50-200ms 冷启动）

    async def stop(self) -> None:
        """停止：打断播放/合成并关闭 HTTP 客户端（进程退出前调用）。"""
        self.interrupt()
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=5)
            except Exception:
                try:
                    self._pump_task.cancel()
                except Exception:
                    pass
        self._pump_task = None
        self._player.close()
        await self._close_client()
        self._ready = False

    async def drain(self) -> None:
        """等待全部已提交句子合成完成 + 全部音频播完。"""
        # 等串行泵消费完所有待合成句子（播放队列懒创建，先等合成再判队列）
        while self._working or (
            self._pending is not None and not self._pending.empty()
        ):
            # speak() 与泵「空闲退出」存在竞态：句子入队的瞬间泵刚好判空退出，
            # speak 侧看到泵未 done 不再重启 → 句子永远无人合成。检测到
            # 「还有句子但泵已死」立即重启泵，杜绝 drain 死等 + 回复尾句丢失。
            if (
                self._pending is not None
                and not self._pending.empty()
                and (self._pump_task is None or self._pump_task.done())
            ):
                self._pump_task = asyncio.create_task(self._pump())
            await asyncio.sleep(0.05)
        if self._pump_task is not None and not self._pump_task.done():
            try:
                await asyncio.wait_for(self._pump_task, timeout=300)
            except Exception:
                pass
        await self._player.drain()

    # ---------- 对外接口（main.py / stream.py 调用） ----------

    def set_on_play_callback(self, cb: Optional[object]) -> None:
        """设置音频开始播放回调（wav 路径, 文本, 时长秒）——口型同步用。"""
        self._player.set_on_play_callback(cb)

    def set_on_play_done_callback(self, cb: Optional[object]) -> None:
        """设置整段播放结束回调（队列排空/打断清空后触发）——说话结束复原用。"""
        self._player.set_on_play_done_callback(cb)

    def set_subtitle_callback(self, cb: Optional[object]) -> None:
        """设置字幕回调：逐字推进时调用（入参为累积文本）。"""
        self._player.set_subtitle_callback(cb)

    def apply_ref(self, audio: str, text: str) -> None:
        """热更新参考音频/文本（控制中心 !tts_audio / !tts_text 热调用）。

        仅更新主参考，辅助参考沿用当前值，互不覆盖。服务端每次合成都读取
        最新配置，热更新即时生效。
        """
        self._ref_main = (audio or "").strip()
        self.ref_text = (text or "").strip()

    def apply_ref_extras(self, extras: str) -> None:
        """热更新辅助参考音频（控制中心 !tts_audios 热调用，多条 | 分隔）。

        服务端仅支持主参考，辅助参考只保留配置串（合成时忽略）。
        """
        self._ref_extras = (extras or "").strip()

    def interrupt(self) -> None:
        """立即闭嘴：停播 + 放弃当前/待合成 + 丢弃未播字幕。

        必须取消合成泵并清空待合成队列：泵若挂起在 await _pending.get()，
        打断后的 drain 会 wait_for(pump, 300s) 卡死；残留句子也会在下一轮
        被误合成。三者缺一不可。
        """
        self._gen += 1
        self._interrupted = True
        if self._pump_task is not None and not self._pump_task.done():
            try:
                self._pump_task.cancel()  # 放弃当前 HTTP 合成请求
            except Exception:
                pass
        if self._pending is not None:
            while True:
                try:
                    self._pending.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._player.interrupt()

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志（main.py 每轮调用）。"""
        self._interrupted = False
        self._player.clear_interrupt()

    # ---------- 合成（/tts/stream 流式 + /tts/batch 兜底，串行泵） ----------

    async def speak(self, text: str) -> None:
        """把一句话送入合成队列（立即返回，不阻塞 LLM 流）。

        串行泵按句 Token 级流式合成；首块到达即出声（首字延迟只受首块
        合成+传输耗时限制），服务端 _infer_lock 串行推理天然保序。
        """
        if not self._ready or self._pending is None or self._interrupted:
            return
        text = (text or "").strip()
        if not text or not _HAS_CONTENT_RE.search(text):
            return  # 纯符号碎片：防 GPT-SoVITS 合成退化
        text = _collapse_tts_text(text)
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
            # 收拢当前待合成句子（最多 _BATCH_MAX 条一批，按句顺序流式合成）
            texts = [text]
            while len(texts) < _BATCH_MAX:
                try:
                    texts.append(self._pending.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._working = True
            try:
                await self._synth_remote(texts, gen)
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

    async def _synth_remote(self, texts: list, gen: int) -> None:
        """逐句合成并播放（优先 Token 级流式，退化/失败回退整句批量）。

        流式路径 POST /tts/stream（官方 infer_stream）：首块到达即出声，
        首字延迟只受首块合成+传输耗时限制。退化（拖长音怪叫/尖峰噪声，
        与文本无关的 GPT 采样随机崩坏）或网络失败时回退 _synth_one 重试
        （内部带重试 + 退化丢弃兜底），绝不把怪叫播完整段。
        """
        for text in texts:
            if gen != self._gen:
                return  # 打断：放弃剩余
            await self._synth_stream(text, gen)

    async def _synth_stream(self, text: str, gen: int) -> None:
        """Token 级流式合成一句（POST /tts/stream，SSE 边收边播）。

        对齐官方示例语义：每块音频到达即播放（audio.play()），每块随带的
        词级时间戳字幕增量立即入队（subtitlesqueue.add），句首块起即与
        播放逐词同步。缓存命中直接播放；否则流式合成，块间无重叠可直接
        拼接，整句写磁盘缓存（保持高频句只合成一次）。流式失败/无输出/
        产物退化时回退 _synth_fallback 整句批量合成兜底。
        """
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        key = _tts_cache_key(text, speaker_audio, prompt_audio, prompt_text)
        cached = await asyncio.to_thread(_cache_load, key)
        if cached is not None:
            if gen != self._gen:
                return
            audio = await asyncio.to_thread(_decode_wav_bytes, cached)
            audio_data, sr = audio
            if _is_degraded_audio(len(audio_data) / sr, len(text)) or _has_burst_noise(
                audio_data
            ):
                # 缓存里存了退化产物（拖长音怪叫/尖峰噪声）：删掉并重合成
                await asyncio.to_thread(_cache_delete, key)
            else:
                # 缓存音频无词级时间戳，字幕回退整句显示
                self._player.emit(audio_data, sr, text, _fallback_subtitles(text), gen)
                return
        payload = {
            "text": text,
            "speaker_audio": speaker_audio,
            "prompt_audio": prompt_audio,
            "prompt_text": prompt_text,
            "stream_chunk": _STREAM_CHUNK,
            "overlap_len": _STREAM_OVERLAP,
            **_SYNTH_PARAMS,
        }
        collected = bytearray()
        sent_id = None
        try:
            async with self._client.stream(
                "POST", f"{self._server_url}/tts/stream", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if gen != self._gen:
                        self._player.abort_stream()  # 打断：放弃剩余喂入
                        return
                    if not line.startswith("data: "):
                        continue
                    evt = json.loads(line[6:])
                    if evt.get("done"):
                        break
                    pcm = base64.b64decode(evt["audio"])
                    if sent_id is None:
                        # 首块到达：建立增量播放会话（Token 级流式，边收边播）
                        sent_id = self._player.begin_stream(32000, text, gen)
                        if sent_id is None:
                            return  # 播放器不可用/已打断
                    # 词级时间戳字幕增量逐块入队（官方示例逐块 add 的 1:1 语义）
                    if evt.get("subtitles"):
                        self._player.feed_subtitles(evt["subtitles"], gen)
                    collected += pcm
                    if not self._player.feed_stream(bytes(pcm), gen):
                        return  # 打断：调用方已放弃本句
            if sent_id is None:
                raise RuntimeError("流式无音频块")
            self._player.end_stream(gen)
            # 整句音频（块间无重叠可直接拼接）写缓存 + 退化兜底
            audio_data = (
                np.frombuffer(bytes(collected), dtype="<i2").astype(np.float32)
                / 32768.0
            )
            if audio_data.size:
                if _is_degraded_audio(
                    len(audio_data) / 32000, len(text)
                ) or _has_burst_noise(audio_data):
                    # 拖长音怪叫/尖峰噪声（采样随机崩坏）：回退整句重合成重播
                    console.warn(
                        f"TTS 流式检测到异常合成（文本 {len(text)} 字、音频 "
                        f"{len(audio_data) / 32000:.1f}s），回退整句合成…"
                    )
                    await self._synth_fallback(text, gen)
                else:
                    wav_bytes = await asyncio.to_thread(
                        _encode_wav_bytes, audio_data, 32000
                    )
                    if wav_bytes:
                        await asyncio.to_thread(_cache_save, key, wav_bytes)
        except Exception as e:
            self._player.abort_stream()  # 失败：终止增量会话
            if gen == self._gen:
                console.dim(f"TTS 流式合成失败（{e}），回退整句合成…")
                await self._synth_fallback(text, gen)

    async def _synth_fallback(self, text: str, gen: int) -> None:
        """流式失败/退化兜底：整句批量合成（_synth_one 带重试+退化丢弃）后整体播放。"""
        retried = await self._synth_one(text, gen)
        if retried is not None and gen == self._gen:
            audio_data, sr2 = retried
            self._player.emit(audio_data, sr2, text, _fallback_subtitles(text), gen)

    async def _synth_one(self, text: str, gen: int):
        """回退路径：单独批量合成一句并下载解码，返回 (audio_data, sr)。

        合成失败（服务端偶发 500/网络抖动）或音频退化（时长异常 = 拖长音
        怪叫，与文本无关的 GPT 采样随机崩坏）时的兜底。合成失败或退化都
        重试一次——GSV 采样随机，重试大概率正常；两次仍失败返回 None，由
        调用方放弃该句，绝不把怪叫播出去。命中磁盘缓存时跳过合成直接解码。
        """
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        payload = {
            "texts": [text],
            "speaker_audio": speaker_audio,
            "prompt_audio": prompt_audio,
            "prompt_text": prompt_text,
            **_SYNTH_PARAMS,
        }
        key = _tts_cache_key(text, speaker_audio, prompt_audio, prompt_text)
        cached = await asyncio.to_thread(_cache_load, key)
        if cached is not None:
            audio = await asyncio.to_thread(_decode_wav_bytes, cached)
            audio_data, sr = audio
            if not (
                _is_degraded_audio(len(audio_data) / sr, len(text))
                or _has_burst_noise(audio_data)
            ):
                return audio_data, sr
            await asyncio.to_thread(_cache_delete, key)  # 退化缓存作废
        for _ in range(2):
            if gen != self._gen:
                return None
            try:
                resp = await self._client.post(
                    f"{self._server_url}/tts/batch", json=payload
                )
                resp.raise_for_status()
            except Exception as e:
                console.dim(f"TTS 合成失败（{e}），重试…")
                continue
            filenames = resp.json().get("filenames") or []
            if not filenames:
                return None
            for filename in filenames:
                audio = await self._download_audio(filename, key)
                if audio is None:
                    continue
                audio_data, sr = audio
                if not (
                    _is_degraded_audio(len(audio_data) / sr, len(text))
                    or _has_burst_noise(audio_data)
                ):
                    return audio_data, sr
                await asyncio.to_thread(_cache_delete, key)  # 退化产物不入缓存
                console.warn(
                    f"TTS 检测到异常合成（文本 {len(text)} 字、音频 "
                    f"{len(audio_data) / sr:.1f}s），重试…"
                )
                break  # 当前句退化：重新合成
        return None

    async def _download_audio(
        self, filename: str, cache_key: str = ""
    ) -> Optional[Tuple[np.ndarray, int]]:
        """下载 wav 字节流并解码为 (1D float32, 采样率)。失败返回 None。

        sf.read 是同步 IO，单个 16k 浮点 wav 解码 ~5-15ms，期间会阻塞整个
        asyncio 事件循环（LLM 流、弹幕处理、字幕推送全部停摆）。改走
        to_thread 把解码扔进默认 executor，事件循环继续派发其它协程。
        传入 cache_key 时把原始 wav 字节写入磁盘缓存（命中后续合成）。
        """
        try:
            resp = await self._client.get(f"{self._server_url}/audio/{filename}")
            resp.raise_for_status()
            content = resp.content
            if cache_key:
                await asyncio.to_thread(_cache_save, cache_key, content)
            # soundfile.read 是阻塞 syscall + numpy 解码，必须放线程池
            data, sr = await asyncio.to_thread(_decode_wav_bytes, content)
            return np.asarray(data, dtype=np.float32).reshape(-1), int(sr)
        except Exception as e:
            console.dim(f"TTS 下载音频失败（{filename}）：{e}")
            return None


def _fallback_subtitles(text: str) -> list:
    """服务端不返回词级时间戳：构造整句时间戳，句首播放时一次性推送整句。

    保持「有 TTS 时字幕交给字幕管线」的行为，仅由逐字降级为整句显示。
    """
    return [{"start_s": 0.0, "text": text, "orig_idx_end": len(text) - 1}]
