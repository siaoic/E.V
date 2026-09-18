"""B站直播（bilibili_live）语音配置管理 API。

B站直播的弹幕接入已由 ``world-bilibili`` 插件承担：房间号、SESSDATA 等在其插件配置里
维护，启停也在「插件管理」页完成。本页因此只负责直播语音朗读
（config/bilibili_live.toml 的 ``[voice]`` 节）的读写，以及当前麦麦对直播间生效的
相关设置（发言频率规则、直播间专属提示词、专注模式等）的只读汇总。

直播间发言频率、提示词等属于 bot_config，统一由标准配置页维护，本页只读展示。
"""

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import tomlkit

from src.common.logger import get_logger
from src.config.config import PROJECT_ROOT, global_config
from src.webui.dependencies import require_auth

logger = get_logger("webui")

PLATFORM = "bilibili_live"
LIVE_CONFIG_PATH = PROJECT_ROOT / "config" / "bilibili_live.toml"

DEFAULT_TTS_BASE_URL = "http://127.0.0.1:8095"

router = APIRouter(prefix="/bilibili_live", tags=["bilibili_live"], dependencies=[Depends(require_auth)])


class LiveVoiceConfigPayload(BaseModel):
    """直播语音配置（config/bilibili_live.toml 的 [voice] 节）。"""

    enabled: bool = Field(default=False, description="是否启用直播语音朗读")
    base_url: str = Field(default=DEFAULT_TTS_BASE_URL, description="GSV-TTS-Lite 流式服务地址")
    spk_audio: str = Field(default="", description="主参考音频（决定音色与说话风格）")
    spk_audio_text: str = Field(default="", description="主参考音频对应的文本")
    spk_audio_additional: List[str] = Field(default_factory=list, description="追加音色参考（可多个）")
    output_device: str = Field(default="", description="音频输出设备")


def _read_live_config() -> dict[str, Any]:
    """读取直播配置文件；文件不存在时返回空内容。"""
    if not LIVE_CONFIG_PATH.exists():
        return {}
    with open(LIVE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return {key: value for key, value in tomlkit.loads(f.read()).items()}


def _voice_table() -> Any:
    """取 [voice] 节；缺失或类型异常时给一个空表，避免后续取值都要判空。"""
    voice = _read_live_config().get("voice")
    return voice if voice is not None else tomlkit.table()


def _voice_config() -> dict[str, Any]:
    """把 [voice] 节整理成前端表单所需的形状（缺项回落到默认值）。"""
    voice = _voice_table()

    def text(name: str, default: str = "") -> str:
        return str(voice.get(name) or default)

    # 追加音色参考允许写成单个字符串，统一成列表交给前端编辑
    additional = voice.get("spk_audio_additional") or []
    if isinstance(additional, str):
        additional = [additional]

    return {
        "enabled": bool(voice.get("enabled", False)),
        "base_url": text("base_url", DEFAULT_TTS_BASE_URL),
        "spk_audio": text("spk_audio"),
        "spk_audio_text": text("spk_audio_text"),
        "spk_audio_additional": [str(item) for item in additional],
        "output_device": text("output_device"),
    }


def _live_talk_value_rule() -> Optional[Any]:
    """返回在 bot_config 中针对 bilibili_live 平台配置的发言频率规则。"""
    reply_timing = global_config.chat.reply_timing
    for rule in reply_timing.talk_value_rules:
        if str(getattr(rule, "platform", "") or "") == PLATFORM:
            return rule
    return None


def _live_chat_prompt() -> Optional[str]:
    """返回直播间专属提示词（chat.reply_style.chat_prompts 中 bilibili_live 平台的条目）。"""
    for item in global_config.chat.reply_style.chat_prompts:
        if str(getattr(item, "platform", "") or "") == PLATFORM:
            return str(getattr(item, "prompt", "") or "")
    return None


def _bot_account() -> str:
    """从 bot.platforms 中解析直播间账号（形如 bilibili_live:uid）。"""
    for entry in global_config.bot.platforms:
        text = str(entry or "").strip()
        if text.startswith(f"{PLATFORM}:"):
            return text.split(":", 1)[1]
    return ""


@router.get("")
async def get_bilibili_live() -> dict[str, Any]:
    """返回直播语音配置与当前麦麦在直播间的相关设置汇总。"""
    rule = _live_talk_value_rule()

    return {
        "success": True,
        "voice": _voice_config(),
        "bot": {
            "platform_registered": any(
                str(entry or "").startswith(f"{PLATFORM}:") for entry in global_config.bot.platforms
            ),
            "bot_account": _bot_account(),
            "focus_mode": bool(global_config.experimental.focus_mode),
            "enable_talk_value_rules": bool(
                global_config.chat.reply_timing.enable_talk_value_rules
            ),
            "rule_item_id": str(getattr(rule, "item_id", "") or "") if rule else "",
            "live_talk_value": float(getattr(rule, "value", 0) or 0) if rule else None,
            "live_prompt": _live_chat_prompt(),
        },
    }


@router.post("/voice")
async def update_live_voice_config(payload: LiveVoiceConfigPayload) -> dict[str, Any]:
    """保存直播语音配置到 config/bilibili_live.toml 的 [voice] 节（保留注释与其他键）。"""
    try:
        if LIVE_CONFIG_PATH.exists():
            with open(LIVE_CONFIG_PATH, "r", encoding="utf-8") as f:
                doc = tomlkit.loads(f.read())
        else:
            LIVE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            doc = tomlkit.document()

        voice = doc.get("voice")
        if voice is None:
            voice = tomlkit.table()
            doc["voice"] = voice

        voice["enabled"] = payload.enabled
        voice["base_url"] = payload.base_url
        voice["spk_audio"] = payload.spk_audio
        voice["spk_audio_text"] = payload.spk_audio_text
        voice["spk_audio_additional"] = payload.spk_audio_additional
        voice["output_device"] = payload.output_device

        with open(LIVE_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(doc))

        logger.info(f"直播语音配置已更新: {LIVE_CONFIG_PATH}")
        return {"success": True, "message": "直播语音配置已保存"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"保存直播语音配置失败: {exc}")
        raise HTTPException(status_code=500, detail=f"保存直播语音配置失败: {exc}") from exc
