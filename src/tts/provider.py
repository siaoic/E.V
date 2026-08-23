"""TTS 后端抽象（3.14）：TTSProvider ABC + GPTSoVITSProvider 包装。

对标 hermes `agent/tts_provider.py`：为"换云端 TTS / 换音色"提供可插拔后端
接口，默认后端仍是 GPT-SoVITS（包装现有 `src/tts/engine.py`，**不改 engine
内部实现**，只在其外包一层统一接口），行为与现状完全一致。
"""

import abc
from typing import Optional

from src.tts.engine import TTSEngine


class TTSProvider(abc.ABC):
    """文本转语音后端抽象基类。子类必须实现 name；其余方法有默认实现。"""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """稳定短标识（小写、无空格），用于配置选择 provider。"""

    def is_available(self) -> bool:
        """当前是否可用（如服务已连接 / 密钥就绪）。默认 True。"""
        return True

    @property
    def voice_compatible(self) -> bool:
        """输出是否适合语音气泡等即时交付场景。默认 False（保守）。"""
        return False

    async def speak(self, text: str, sfx: str = "") -> None:
        """把一句话送入合成播放队列（立即返回，不阻塞调用方）。"""
        raise NotImplementedError(f"TTS provider {self.name!r} 未实现 speak()")

    async def drain(self) -> None:
        """等待全部已提交句子合成播放完成。"""
        raise NotImplementedError(f"TTS provider {self.name!r} 未实现 drain()")

    async def synthesize(
        self, text: str, output_path: Optional[str] = None, **extra
    ) -> Optional[bytes]:
        """合成整段文本为音频（写文件或返回 wav 字节）。可选能力。

        output_path 提供时把 wav 写入该路径并返回路径（str）；否则返回
        字节流。不支持文件型合成的后端保持 NotImplementedError（默认）。
        """
        raise NotImplementedError(f"TTS provider {self.name!r} 未实现 synthesize()")

    async def stream(self, text: str, **extra):
        """流式产出音频块。可选能力，不支持时保持 NotImplementedError（默认）。"""
        raise NotImplementedError(f"TTS provider {self.name!r} 未实现 stream()")


class GPTSoVITSProvider(TTSProvider):
    """GPT-SoVITS 后端：包装现有 TTSEngine（引擎负责 HTTP 合成 + 播放）。

    引擎实例在 Application 初始化完成后注入（attach_engine），注入前调用
    任一方法均为安全空操作（is_available 返回 False）。
    """

    name = "gpt-sovits"

    def __init__(self, engine: Optional[TTSEngine] = None) -> None:
        self._engine = engine

    def attach_engine(self, engine: TTSEngine) -> None:
        """注入引擎实例（Application 构建 TTS 后调用）。"""
        self._engine = engine

    def is_available(self) -> bool:
        return self._engine is not None and getattr(self._engine, "_ready", False)

    @property
    def voice_compatible(self) -> bool:
        # E.V 为本地实时播放链路，天然适合语音即时交付
        return True

    async def speak(self, text: str, sfx: str = "") -> None:
        if self._engine is None:
            return
        await self._engine.speak(text, sfx)

    async def drain(self) -> None:
        if self._engine is None:
            return
        await self._engine.drain()

    async def synthesize(
        self, text: str, output_path: Optional[str] = None, **extra
    ) -> Optional[bytes]:
        """复用引擎的批量合成路径（含退化重试兜底）产出 wav。

        依赖引擎内部接口（_synth_one / _gen / _client）：仅包装不修改，
        引擎未就绪或合成失败（退化兜底后仍失败）时返回 None。
        """
        engine = self._engine
        if engine is None or engine._client is None:
            return None
        import io

        import soundfile as _sf

        audio = await engine._synth_one(text, engine._gen)
        if audio is None:
            return None
        audio_data, sr = audio
        buf = io.BytesIO()
        _sf.write(buf, audio_data, sr, format="WAV")
        content = buf.getvalue()
        if output_path:
            with open(output_path, "wb") as f:
                f.write(content)
            return content  # 调用方只关心字节/成功与否，路径由调用方持有
        return content
