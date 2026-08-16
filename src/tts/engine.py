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

# 实质内容字符集合：\w 匹配字母/数字/下划线（Python3 Unicode 模式下含
# 中文等文字），显式补 CJK/日/韩范围兜底。判断一句是否含可合成内容——
# 含任一有效字符即保留（"啊——"含"啊"会保留）；只有纯符号/空白碎片
# （如"……""——"）才丢弃，防 GPT-SoVITS 合成退化（长音怪叫）。
_HAS_CONTENT_RE = re.compile(
    r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 防合成退化的文本清洗正则：
# 1) URL：GPT-SoVITS 会逐个念出协议/域名符号，拖出怪叫
_URL_RE = re.compile(r"https?://[^\s，。！？、；：]+", re.IGNORECASE)
# 2) 颜文字/纯符号括号块（如 (￣▽￣)、(=ω=)）：GSV 服务端合成直接 500
#    报错；括号内含中文/数字/字母的（如 （狗头））不删，保留语义
_KAOMOJI_RE = re.compile(
    r"[（(][^（）()0-9A-Za-z\u4e00-\u9fff]*[)）]")
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
_MAX_DEGRADED_SEC = 25.0          # 绝对上限：正常句几乎不可能超过（~80 字才 ~20s）
_MAX_SEC_PER_CHAR = 1.0           # 相对判据：正常语速 ~0.3s/字，1s/字+ 必为拖长音
_MIN_DEGRADED_SEC = 8.0           # 短文本相对判据的最小绝对时长（防误杀短句）


def _is_degraded_audio(dur_s: float, text_len: int) -> bool:
    """判断合成音频是否退化（拖长音怪叫）：时长异常地远超文本长度。"""
    return (dur_s > _MAX_DEGRADED_SEC
            or (dur_s > _MIN_DEGRADED_SEC
                and dur_s / max(1, text_len) > _MAX_SEC_PER_CHAR))


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

# 服务端默认地址（fastapi_server_example.py 监听 0.0.0.0:8000），可用
# TTS_SERVER_URL 覆盖
_SERVER_DEFAULT_URL = "http://127.0.0.1:8167"

# 每批最多合成句数：LLM 流式产句远快于服务端合成，批量收拢攒不满反而
# 拖累首声（服务端 infer_batched_async 整批合成完才返回，8 句可能等
# 20-40s，听感变成「LLM 发完才开始合成」）。设为 1 = 每句立即合成播放，
# 首声延迟仅第 1 句合成耗时，实现真正的边说边合成边播。
_BATCH_MAX = 1

# 合成参数（服务端 /tts/batch 透传到 infer_batched_async）：
# 采用 GSV-TTS-Lite 官方 API 文档推荐值——top_k=5/top_p=0.9 比库默认
# top_k=15/top_p=1.0 采样更收敛，配合 repetition_penalty 抑制循环怪音。
# 文本切段/语言参数（cut_mute 等）服务端接口不接受，保持服务端默认即可。
_SYNTH_PARAMS = {
    "top_k": 5, "top_p": 0.9, "temperature": 1.0,
    "repetition_penalty": 1.35, "noise_scale": 0.5, "speed": 1.0,
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
        self._preheat_task: Optional[asyncio.Task] = None  # 每轮预热任务（运行中且未完成则去重）
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

    def preheat(self) -> None:
        """每轮对话起始预热：HTTP 连接 + wav 解码链路（不阻塞 LLM）。

        设计动机：启动时 warmup() 把服务端 CUDA graph / 缓存烫好了，但客户端
        路径（HTTP 连接池拿连接 → 下载 wav → 线程池 sf.read → emit 到播放器）
        在长时间不发声后会变冷。30s+ 没合成时首句延迟会多 ~100-300ms。

        这里在 converse() 启动后立刻 `create_task` 跑一个「空响」——
        真实合成一个「.」并完整走通下载 + 解码，但**不发声**：
        - HTTP 路径：keep-alive 连接重新活跃，下次真实合成命中现成连接；
        - 解码路径：soundfile / numpy 缓存进页，真实首句解码省 ~5-15ms；
        - 不入 player 队列：不会被听到，也不会被 drain 误判未排空。

        行为契约：
        - 立即返回（仅 spawn task，不等合成完成）
        - 失败静默：HTTP 抖动时丢掉预热，真实首句照常
        - 与 pump 串行：客户端只有一个 _client，避免争抢；预热在真实句之前
          排队，真实首句只会更快不会更慢
        """
        if not self._ready or self._client is None or self._interrupted:
            return
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        if not speaker_audio:
            return
        # 已在预热中：去重，避免每轮反复起 N 个
        if self._preheat_task is not None and not self._preheat_task.done():
            return
        self._preheat_task = asyncio.create_task(
            self._preheat_silent(speaker_audio, prompt_audio, prompt_text))

    async def _preheat_silent(self, speaker_audio: str,
                              prompt_audio: str, prompt_text: str) -> None:
        """预热实现：合成「.」→ 下载 → 解码 → 丢弃。失败静默。"""
        try:
            resp = await self._client.post(
                f"{self._server_url}/tts/batch",
                json={"texts": ["。"], "speaker_audio": speaker_audio,
                      "prompt_audio": prompt_audio, "prompt_text": prompt_text,
                      **_SYNTH_PARAMS})
            resp.raise_for_status()
            data = resp.json()
            filenames = data.get("filenames") or []
            if not filenames:
                return
            # 真实「解码」也跑一遍——把 soundfile 缓存烧热；
            # wav 不入 player.emit()，所以不会被听到。
            await self._download_audio(filenames[0])
        except Exception:
            pass  # 预热失败 → 真实首句照常走，零风险

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
        # 打断时也要取消预热：HTTP 请求挂着浪费服务端资源
        if self._preheat_task is not None and not self._preheat_task.done():
            try:
                self._preheat_task.cancel()
            except Exception:
                pass
        self._preheat_task = None
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
        时放弃剩余音频下载。单句音频退化（时长异常=拖长音怪叫）时走
        _synth_one 重试一次，仍异常则丢弃该句（宁缺毋怪）。整批请求
        失败（服务端偶发 500/网络抖动）时转逐句重试，不再整批丢弃。
        """
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        payload = {
            "texts": texts,
            "speaker_audio": speaker_audio,
            "prompt_audio": prompt_audio,
            "prompt_text": prompt_text,
            **_SYNTH_PARAMS,
        }
        try:
            resp = await self._client.post(
                f"{self._server_url}/tts/batch", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # 整批失败（偶发 500）：逐句重试（_synth_one 内部带 2 次尝试）
            console.warn(f"TTS 批量合成失败（{e}），逐句重试…")
            for text in texts:
                if gen != self._gen:
                    return  # 打断：放弃剩余
                retried = await self._synth_one(text, gen)
                if retried is None:
                    continue  # 重试仍失败：放弃该句
                audio_data, sr = retried
                # 重试的音频无词级时间戳，字幕回退整句显示
                self._player.emit(audio_data, sr, text,
                                  _fallback_subtitles(text), gen)
            return
        subtitles_list = data.get("subtitles") or []
        for i, (text, filename) in enumerate(zip(texts, data.get("filenames", []))):
            if gen != self._gen:
                return  # 打断：放弃剩余音频
            audio = await self._download_audio(filename)
            if audio is None:
                continue
            audio_data, sr = audio
            degraded = _is_degraded_audio(len(audio_data) / sr, len(text))
            if degraded:
                retried = await self._synth_one(text, gen)
                if retried is None:
                    continue  # 重试仍退化/失败：放弃该句，不播怪叫
                audio_data, sr = retried
            # 重试的音频无词级时间戳，字幕回退整句显示
            subs = (subtitles_list[i] if not degraded
                    and i < len(subtitles_list) else None)
            self._player.emit(audio_data, sr, text,
                              subs if subs else _fallback_subtitles(text), gen)

    async def _synth_one(self, text: str, gen: int):
        """单独合成一句并下载解码，返回 (audio_data, sr)。

        合成失败（服务端偶发 500/网络抖动）或音频退化（时长异常 =
        拖长音怪叫，与文本无关的 GPT 采样随机崩坏）时都重试一次——GSV
        采样随机，重试大概率正常；两次仍失败返回 None，由调用方放弃
        该句，绝不把怪叫播出去。
        """
        speaker_audio, prompt_audio, prompt_text = self._ref_params()
        payload = {
            "texts": [text],
            "speaker_audio": speaker_audio,
            "prompt_audio": prompt_audio,
            "prompt_text": prompt_text,
            **_SYNTH_PARAMS,
        }
        for _ in range(2):
            if gen != self._gen:
                return None
            try:
                resp = await self._client.post(
                    f"{self._server_url}/tts/batch", json=payload)
                resp.raise_for_status()
            except Exception as e:
                console.dim(f"TTS 合成失败（{e}），重试…")
                continue
            filenames = resp.json().get("filenames") or []
            if not filenames:
                return None
            for filename in filenames:
                audio = await self._download_audio(filename)
                if audio is None:
                    continue
                audio_data, sr = audio
                if not _is_degraded_audio(len(audio_data) / sr, len(text)):
                    return audio_data, sr
                console.warn(
                    f"TTS 检测到异常合成（文本 {len(text)} 字、音频 "
                    f"{len(audio_data) / sr:.1f}s），重试…")
                break  # 当前句退化：重新合成
        return None

    async def _download_audio(self, filename: str) -> Optional[Tuple[np.ndarray, int]]:
        """下载 wav 字节流并解码为 (1D float32, 采样率)。失败返回 None。

        sf.read 是同步 IO，单个 16k 浮点 wav 解码 ~5-15ms，期间会阻塞整个
        asyncio 事件循环（LLM 流、弹幕处理、字幕推送全部停摆）。改走
        to_thread 把解码扔进默认 executor，事件循环继续派发其它协程。
        """
        try:
            resp = await self._client.get(
                f"{self._server_url}/audio/{filename}")
            resp.raise_for_status()
            content = resp.content
            # soundfile.read 是阻塞 syscall + numpy 解码，必须放线程池
            data, sr = await asyncio.to_thread(
                _decode_wav_bytes, content)
            return np.asarray(data, dtype=np.float32).reshape(-1), int(sr)
        except Exception as e:
            console.dim(f"TTS 下载音频失败（{filename}）：{e}")
            return None


def _fallback_subtitles(text: str) -> list:
    """服务端不返回词级时间戳：构造整句时间戳，句首播放时一次性推送整句。

    保持「有 TTS 时字幕交给字幕管线」的行为，仅由逐字降级为整句显示。
    """
    return [{"start_s": 0.0, "text": text, "orig_idx_end": len(text) - 1}]
