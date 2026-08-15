"""TTS 引擎：连接外部 GPT-SoVITS HTTP 服务端（tools/gsv_tts/API/fastapi_server_example.py）。

主程序不再进程内加载模型，改由 tts.bat 启动的外部服务负责合成，本引擎作为
客户端使用：
- 待合成句子入队，串行泵按句收拢成批调用 /tts/batch 合成，再逐句
  /audio/{filename} 拉取 wav 字节流，解码后交给 src.tts.player 无缝播放；
- 播放/口型/字幕复用 TTSPlayer（真实开播时刻锚定 + 词级时间戳字幕）；
- 服务端接口不返回词级时间戳，字幕降级为整句显示（句首播放时一次性推送）；
- 未连接服务时 start() 返回 False，speak/drain/stop 静默降级（主程序只走字幕）。

兼容旧模块接口（main.py / stream.py / cleaner.py 引用）：
start/stop/drain/speak/interrupt/clear_interrupt/set_on_play_callback/
set_subtitle_callback/apply_ref(_extras) + _wav_cache / _cleanup_output。
"""

import asyncio
import io
import os
import re
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

# 纯符号/无实质内容碎片：直接丢弃，防 GPT-SoVITS 合成退化（"啊——"长音怪叫）
_HAS_CONTENT_RE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 服务端默认地址（fastapi_server_example.py 监听 0.0.0.0:8000），可用
# TTS_SERVER_URL 覆盖
_SERVER_DEFAULT_URL = "http://127.0.0.1:8000"

# 单批最大句子数（LLM 流同时积攒句子的上限，对齐本地引擎）
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

# 合成参数（逐项显式传给服务端接口）：采样参数 + 文本/参考语言 +
# 切段静音调参（服务端 /tts/batch 逐项透传到 infer_batched_async）
_SYNTH_PARAMS = {
    "top_k": 15, "top_p": 1.0, "temperature": 1.0,
    "repetition_penalty": 1.35, "noise_scale": 0.5, "speed": 1.0,
    "text_language": _TEXT_LANG,
    "prompt_language": _REF_LANG,
    "cut_minlen": _CUT_MINLEN,
    "cut_mute": _CUT_MUTE,
    "cut_mute_scale_map": _CUT_MUTE_SCALE_MAP,
}

# 兼容 cleaner.py：新引擎临时 wav 播放即删，无持久缓存可清
_wav_cache: dict = {}


def _cleanup_output() -> Tuple[int, int]:
    """兼容 cleaner.py 的旧清理入口：新引擎无 output 目录残留。"""
    return (0, 0)


class TTSEngine(BaseTTSAdapter):
    """GPT-SoVITS HTTP 服务端客户端引擎（合成在独立进程，本引擎只播放）。

    服务端为 tools/gsv_tts/API/fastapi_server_example.py（tts.bat 启动）：
    - /tts/batch 批量合成，返回 output 目录下的文件名列表；
    - /audio/{filename} 下载 wav 字节流，本进程解码后无缝播放。
    未连接服务时 start() 返回 False，speak 等接口静默降级（不报错）。
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
        self._server_url = str(
            getattr(_config.cfg, "TTS_SERVER_URL", "") or "").strip() \
            or _SERVER_DEFAULT_URL

        self._client: Optional[httpx.AsyncClient] = None
        self._ready = False                  # start() 探测服务成功后置位
        self._player = TTSPlayer()           # 播放/口型/字幕公共逻辑
        self._gen = 0                        # 代次号：interrupt 递增，泵循环检测后提前退出
        self._interrupted = False            # interrupt 标志（clear_interrupt 复位）
        self._pending: Optional[asyncio.Queue] = None  # 待合成句子队列
        self._pump_task: Optional[asyncio.Task] = None  # 串行合成泵
        self._working = False                # 当前是否正在合成（drain 等待用）

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
        self._client = httpx.AsyncClient(timeout=120.0)
        try:
            resp = await self._client.get(f"{self._server_url}/")
            resp.raise_for_status()
        except Exception as e:
            console.warn(
                f"TTS：无法连接服务端 {self._server_url}（{e}）——"
                f"请先运行 tts.bat 启动 GPT-SoVITS 服务，语音将降级为纯字幕")
            await self._close_client()
            return False
        self._pending = asyncio.Queue()
        self._ready = True
        console.ok(f"TTS 服务端已连接（{self._server_url}）")
        return True

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def warmup(self) -> None:
        """预热服务端：合成一句短文本，让 CUDA graph 首次编译 / 参考音频
        缓存提前就绪，降低真实首句的合成延迟（启动后台调用，不阻塞流程）。

        服务端模型在 startup 只加载权重，首次合成才编译推理图；预热
        把这份开销提前消化，之后主播第一次说话不必再等。失败静默降级。
        """
        if not self._ready or self._client is None:
            return
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        if not speaker_audio:
            return
        try:
            resp = await self._client.post(
                f"{self._server_url}/tts/batch",
                json={"texts": ["你好呀"], "speaker_audio": speaker_audio,
                      "prompt_audio": prompt_audio, "prompt_text": prompt_text,
                      **_SYNTH_PARAMS})
            resp.raise_for_status()
            console.dim("[TTS] 服务端预热完成（合成管线已就绪）")
        except Exception as e:
            console.dim(f"[TTS] 预热失败（不影响使用）：{e}")

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
        await self._player.drain()

    # ---------- 对外接口（main.py / stream.py 调用） ----------

    def set_on_play_callback(self, cb: Optional[object]) -> None:
        """设置音频开始播放回调（wav 路径, 文本, 时长秒）——口型同步用。"""
        self._player.set_on_play_callback(cb)

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
        """立即闭嘴：停播 + 放弃当前/待合成 + 丢弃未播字幕。"""
        self._gen += 1
        self._interrupted = True
        self._player.interrupt()

    def clear_interrupt(self) -> None:
        """新一轮输出前复位打断标志（main.py 每轮调用）。"""
        self._interrupted = False
        self._player.clear_interrupt()

    # ---------- 合成（HTTP 批量，串行泵） ----------

    async def speak(self, text: str) -> None:
        """把一句话送入合成队列（立即返回，不阻塞 LLM 流）。

        串行泵按句顺序批量合成；服务端阻塞推理天然保序，首句延迟仅为首批
        合成耗时。
        """
        if (not self._ready or self._pending is None
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
            # 收拢当前待合成句子（最多 _BATCH_MAX 条一批，走批量合成）
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
        """POST /tts/batch 批量合成 → 逐句下载 wav 解码入队播放。

        服务端返回 output 目录文件名列表（与输入同序）；打断（gen 变化）
        时放弃剩余音频下载。
        """
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        payload = {
            "texts": texts,
            "speaker_audio": speaker_audio,
            "prompt_audio": prompt_audio,
            "prompt_text": prompt_text,
            **_SYNTH_PARAMS,
        }
        resp = await self._client.post(
            f"{self._server_url}/tts/batch", json=payload)
        resp.raise_for_status()
        data = resp.json()
        for text, filename in zip(texts, data.get("filenames", [])):
            if gen != self._gen:
                return  # 打断：放弃剩余音频
            audio = await self._download_audio(filename)
            if audio is None:
                continue
            audio_data, sr = audio
            self._player.emit(audio_data, sr, text,
                              _fallback_subtitles(text), gen)

    async def _download_audio(self, filename: str) -> Optional[Tuple[np.ndarray, int]]:
        """下载 wav 字节流并解码为 (1D float32, 采样率)。失败返回 None。"""
        try:
            resp = await self._client.get(
                f"{self._server_url}/audio/{filename}")
            resp.raise_for_status()
            data, sr = sf.read(io.BytesIO(resp.content), dtype="float32")
            return np.asarray(data, dtype=np.float32).reshape(-1), int(sr)
        except Exception as e:
            console.dim(f"TTS 下载音频失败（{filename}）：{e}")
            return None


def _fallback_subtitles(text: str) -> list:
    """服务端不返回词级时间戳：构造整句时间戳，句首播放时一次性推送整句。

    保持「有 TTS 时字幕交给字幕管线」的行为，仅由逐字降级为整句显示。
    """
    return [{"start_s": 0.0, "text": text, "orig_idx_end": len(text) - 1}]
