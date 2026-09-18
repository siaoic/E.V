"""麦克风语音世界插件（``maibot-live.world-asr``）。

麦克风常驻采集 → 能量 VAD 分段 → 主程序语音识别转写 → 世界事件。

三重开关（任一关闭时插件拒绝加载并明确提示，不做静默降级）：
1. 主配置 ``worlds.enabled``（世界框架总开关）；
2. 本插件 ``asr.enabled``；
3. 主配置 ``voice.enable_asr``（语音识别本身）。

转写文本以 ``debounce`` 语义上报（``world.event``），即「你听到：……」；
语音是易碎输入，单段转写失败只记日志并回传失败事件，不终止采集循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any, Dict, Optional

from maibot_sdk import API, Field as PluginField, MaiBotPlugin, PluginConfigBase
from pydantic import Field

from .capture import EnergyVadMic, resolve_input_device

# 主程序世界框架能力名（基座实现在 src/worlds/，插件通过能力通道调用）。
WORLD_REGISTER_CAPABILITY = "world.register"
WORLD_UNREGISTER_CAPABILITY = "world.unregister"
WORLD_EVENT_CAPABILITY = "world.event"

# 主程序配置读取能力（用于三重开关判定，键为全局配置的点路径）。
CONFIG_GET_CAPABILITY = "config.get"

DEFAULT_ENV_PROMPT = (
    "你的直播间旁有一支打开的麦克风，主播（用户）对着麦克风说的话会被语音识别转写成文本，"
    "以「你听到：……」的形式作为世界事件出现在你的上下文里；这是主播在直接对你说话。"
)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件通用配置。"""

    enabled: bool = PluginField(default=True, description="是否启用插件")
    config_version: str = PluginField(default="0.1.0", description="配置版本，由宿主用于配置迁移")


class AsrConfig(PluginConfigBase):
    """麦克风采集与转写参数。"""

    enabled: bool = PluginField(default=True, description="ASR 世界开关（三重开关之一）")
    device: str = PluginField(
        default="",
        description="麦克风输入设备名（模糊匹配）；留空用系统默认输入设备",
    )
    silence_seconds: float = PluginField(
        default=1.2, ge=0.2, le=10.0, description="安静多久判定一句说完（秒）"
    )
    silence_threshold_rms: float = PluginField(
        default=320.0,
        ge=1.0,
        le=10000.0,
        description="语音能量门限（int16 RMS）；环境噪声大时调高",
    )
    min_speech_seconds: float = PluginField(
        default=0.3, ge=0.05, le=5.0, description="短于该时长的声响按噪声丢弃（秒）"
    )
    max_segment_seconds: float = PluginField(
        default=15.0, ge=2.0, le=60.0, description="单段语音最长时长，超过强制切分（秒）"
    )
    sample_rate: int = PluginField(
        default=16000, ge=8000, le=48000, description="采集采样率；语音识别模型通常为 16000"
    )
    name: str = PluginField(default="asr", description="世界机器名，全局唯一")
    display_name: str = PluginField(default="麦克风", description="世界显示名，出现在注入文本与事件前缀中")
    env_prompt: str = PluginField(
        default=DEFAULT_ENV_PROMPT, description="注入给模型的静态世界说明，只在世界集合变化时注入一次"
    )


class WorldAsrConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


class WorldAsrPlugin(MaiBotPlugin):
    """麦克风语音世界插件入口。"""

    config_model = WorldAsrConfig

    def __init__(self) -> None:
        super().__init__()
        self._mic: Optional[EnergyVadMic] = None
        self._transcribe_task: Optional[asyncio.Task[None]] = None
        self._segment_queue: Optional[asyncio.Queue] = None
        self._registered_name = ""
        self._transcribe = None  # 惰性导入的转写函数（src.common.utils.utils_voice.get_voice_text）
        self._last_text = ""
        self._last_text_at = 0.0
        self._last_error = ""

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """三重开关判定 → 注册世界 → 启动麦克风采集与转写循环。

        Raises:
            RuntimeError: 开关未打开、设备不存在或麦克风打不开时抛出，
                让加载失败并把原因暴露出来，而不是安静地空转。
        """

        settings = self.config.asr
        if not settings.enabled:
            raise RuntimeError("ASR 世界被插件配置关闭（asr.enabled = false），如需启用请改配置后重新加载")

        worlds_enabled = await self._read_host_config("worlds.enabled", False)
        if not worlds_enabled:
            raise RuntimeError(
                "主配置 worlds.enabled = false：世界框架未开启，ASR 世界的事件无处投递。"
                "请在 config/bot_config.toml 打开 [worlds] enabled 后重新加载插件"
            )
        enable_asr = await self._read_host_config("voice.enable_asr", False)
        if not enable_asr:
            raise RuntimeError(
                "主配置 voice.enable_asr = false：语音识别未启用。"
                "请在 config/bot_config.toml 打开 [voice] enable_asr 后重新加载插件"
            )

        device_index = resolve_input_device(settings.device, self.ctx.logger)
        await self._register_to_core()

        self._segment_queue = asyncio.Queue()
        self._mic = EnergyVadMic(
            device=device_index,
            sample_rate=settings.sample_rate,
            silence_seconds=settings.silence_seconds,
            silence_threshold_rms=settings.silence_threshold_rms,
            min_speech_seconds=settings.min_speech_seconds,
            max_segment_seconds=settings.max_segment_seconds,
            on_segment=self._on_segment_from_audio_thread,
            loop=asyncio.get_running_loop(),
            logger=self.ctx.logger,
        )
        try:
            await self._mic.start()
        except Exception:
            await self._unregister_from_core()
            raise

        self._transcribe_task = asyncio.create_task(self._transcribe_loop())
        self.ctx.logger.info(
            f"ASR 世界已接入框架：world_name={settings.name} "
            f"silence={settings.silence_seconds:g}s threshold_rms={settings.silence_threshold_rms:g}"
        )

    async def on_unload(self) -> None:
        """停止采集与转写循环，并从框架基座注销本世界。"""

        await self._stop_transcribe_task()
        mic = self._mic
        self._mic = None
        if mic is not None:
            await mic.stop()
        await self._unregister_from_core()
        self.ctx.logger.info("ASR 世界已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置变更后整体重建采集（设备与节拍都是构造期参数）。"""

        del config_data
        await self.on_unload()
        await self.on_load()
        self.ctx.logger.info(f"ASR 世界配置已更新（scope={scope} version={version}）")

    # ------------------------------------------------------------------ 框架约定 API

    @API(
        "world_observe",
        description="按需拉取麦克风的即时状态（设备、采集节拍、最近转写）。",
        version="1",
        public=True,
    )
    async def handle_world_observe(self, **_kwargs: Any) -> Dict[str, Any]:
        """供 world_observe 工具按需拉取。"""

        return {"snapshot": self._render_snapshot()}

    # ------------------------------------------------------------------ 事件循环

    def _on_segment_from_audio_thread(self, wav_bytes: bytes, started_at: float) -> None:
        """音频线程 → 事件循环的分段投递入口（经 call_soon_threadsafe 调度）。"""

        queue = self._segment_queue
        if queue is None:
            return
        queue.put_nowait((wav_bytes, started_at))

    async def _transcribe_loop(self) -> None:
        """逐段转写并上报世界事件。

        单段失败记录日志并继续：麦克风是持续输入，一次识别抖动不应该让
        整条链路从此静音；连续失败会体现在 ``_last_error`` 与快照里。
        """

        queue = self._segment_queue
        if queue is None:
            return

        while True:
            wav_bytes, started_at = await queue.get()
            try:
                await self._transcribe_segment(wav_bytes, started_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单段失败不应终止转写链
                self._last_error = f"{time.strftime('%H:%M:%S')} 转写失败: {exc!r}"
                self.ctx.logger.exception(f"语音分段转写失败: {exc!r}")

    async def _transcribe_segment(self, wav_bytes: bytes, started_at: float) -> None:
        """把一段 WAV 喂给主程序语音识别，转写结果作为世界事件上报。"""

        get_voice_text = self._require_transcribe()
        text = (await get_voice_text(wav_bytes) or "").strip()
        if not text:
            self.ctx.logger.info("语音分段转写结果为空，跳过上报")
            return

        self._last_text = text
        self._last_text_at = started_at
        self.ctx.logger.info(f"[转写] {text}")

        response = await self.ctx.call_capability(
            WORLD_EVENT_CAPABILITY,
            world_name=self.config.asr.name,
            event_type="voice",
            text=f"你听到：{text}",
            # 语音直接唤醒对话，但仍走合批与频率门控，避免连续说话时刷屏
            trigger="debounce",
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"上报转写世界事件失败：{error}")

    def _require_transcribe(self):
        """惰性导入主程序的语音转写入口。

        主程序与插件运行时共用同一解释器与 ``src`` 包，直接复用
        ``utils_voice.get_voice_text``（内部含 ``voice.enable_asr`` 判定与
        ``LLMServiceClient`` 请求链路），避免在插件里另起一套识别客户端。

        Raises:
            RuntimeError: 导入失败时抛出——没有识别能力这个世界就没有意义。
        """

        if self._transcribe is None:
            from src.common.utils.utils_voice import get_voice_text

            self._transcribe = get_voice_text
        return self._transcribe

    # ------------------------------------------------------------------ 内部实现

    async def _read_host_config(self, key: str, default):
        """经 ``config.get`` 能力读主程序全局配置的点路径字段。"""

        response = await self.ctx.call_capability(CONFIG_GET_CAPABILITY, key=key, default=default)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"读取主程序配置 {key} 失败：{error}")
        return response.get("value", default)

    async def _register_to_core(self) -> None:
        """把本世界注册进主程序的世界框架。

        Raises:
            RuntimeError: 能力调用失败时抛出——能力已写在 manifest 中，
                注册失败说明契约被破坏，必须暴露而不是带着半个世界继续跑。
        """

        settings = self.config.asr
        name = settings.name.strip()
        if not name:
            raise RuntimeError("ASR 世界配置缺少 asr.name")

        response = await self.ctx.call_capability(
            WORLD_REGISTER_CAPABILITY,
            name=name,
            display_name=settings.display_name,
            env_prompt=settings.env_prompt,
            # 语音事件由采集侧主动推送，无需框架轮询
            polls_changes=False,
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"向世界框架注册 ASR 世界失败：{error}")

        self._registered_name = name

    async def _unregister_from_core(self) -> None:
        """把本世界从世界框架注销；未注册时直接返回。"""

        registered = self._registered_name
        if not registered:
            return
        self._registered_name = ""

        response = await self.ctx.call_capability(WORLD_UNREGISTER_CAPABILITY, name=registered)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"从世界框架注销 ASR 世界失败：world={registered} error={error}")

    async def _stop_transcribe_task(self) -> None:
        """取消转写循环任务。"""

        task = self._transcribe_task
        self._transcribe_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _render_snapshot(self) -> str:
        """渲染一份中文状态快照。"""

        settings = self.config.asr
        mic = self._mic
        if mic is None:
            return "麦克风未在采集（插件未加载完成或已被关停）。"
        if self._last_error:
            return f"麦克风状态：{mic.status_text()}；最近一次异常：{self._last_error}。"
        parts = [f"麦克风状态：{mic.status_text()}（采样率 {settings.sample_rate}Hz）"]
        if self._last_text:
            clock = datetime.fromtimestamp(time.time()).strftime("%H:%M:%S")
            parts.append(f"最近一次听到的话（{clock}）：「{self._last_text}」")
        else:
            parts.append("还没有听到过有效语音。")
        return "；".join(parts)


def create_plugin() -> WorldAsrPlugin:
    """插件工厂函数。"""

    return WorldAsrPlugin()
