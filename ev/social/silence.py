"""ev.social.silence — 沉默协议(模块 3/5)

让 LLM 能用 [SILENT] / [END] 标记表达"现在不接"或"主动收尾"。

灵感:qq-bridge 的 reserved 模式里 AI 可以输出 [SILENT] 表示潜水。

设计:
  - 在 ev/llm/stream.py:speak_text() 末尾插入一行 hook
  - hook 检测 final 段里的 [SILENT] / [END] 标记
  - 命中 [SILENT] → 静默,不进入 TTS,但更新状态机沉默计数
  - 命中 [END] → 触发收尾动作(表情 + 状态机记录)
  - 强制不沉默场景(SC / @机器人 / 礼物)直接忽略 [SILENT]
"""

from __future__ import annotations

import re
import logging
from typing import Optional

logger = logging.getLogger("ev.social.silence")

_SILENT_MARKER = re.compile(r"\[SILENT\]", re.IGNORECASE)
_END_MARKER = re.compile(r"\[END\]", re.IGNORECASE)

# 强制不沉默的场景(调用方传 context)
def _is_forced_speech(context: Optional[dict]) -> bool:
    if not context:
        return False
    return bool(
        context.get("is_superchat")
        or context.get("is_at_me")
        or context.get("is_gift")
    )


def detect_silence(text: str) -> bool:
    """是否包含 [SILENT] 标记。"""
    if not text:
        return False
    return bool(_SILENT_MARKER.search(text))


def detect_end(text: str) -> bool:
    """是否包含 [END] 标记。"""
    if not text:
        return False
    return bool(_END_MARKER.search(text))


_SILENT_ALT_RE = re.compile(r"<SILENT>", re.IGNORECASE)
_END_ALT_RE = re.compile(r"<END>", re.IGNORECASE)


def strip_markers(text: str) -> str:
    """清掉 [SILENT] / [END] 标记(含 <SILENT>/<END> 变体),返回干净文本。

    只剥标记不改语义——调用方自行决定"剥完是否播报"(主动路径按标记
    静默,主对话路径只剥不静默)。
    """
    if not text:
        return text
    text = _SILENT_MARKER.sub("", text)
    text = _END_MARKER.sub("", text)
    text = _SILENT_ALT_RE.sub("", text)
    text = _END_ALT_RE.sub("", text)
    return text.strip()


async def on_ai_final(
    text: str,
    *,
    context: Optional[dict] = None,
    max_consecutive: int = 3,
) -> Optional[str]:
    """LLM final 段处理钩子。
    
    Returns:
        None → 静默,不进入 TTS
        str  → 清理后的文本(可继续)
        ""   → 文本已全空(也静默)
    
    调用方(ev/llm/stream.py:speak_text)伪代码:
        result = await on_ai_final(text, context=...)
        if result is None or result == "":
            return  # 静默
        # 继续 TTS 流程
    """
    if not text:
        return ""
    
    # 强制不沉默的场景:即使 LLM 写了 [SILENT] 也忽略
    if _is_forced_speech(context):
        return strip_markers(text)
    
    has_silence = detect_silence(text)
    has_end = detect_end(text)
    
    if not (has_silence or has_end):
        return text  # 正常文本,直接放行
    
    # 命中 [SILENT]
    if has_silence:
        # 检查连续 [SILENT] 次数,到上限必须回一条
        consecutive = _get_consecutive_silence_count()
        if consecutive >= max_consecutive:
            logger.info(f"[silence] consecutive [SILENT] reached max={max_consecutive}, forcing reply")
            _reset_consecutive_silence()
            return strip_markers(text)  # 强制播报
        
        _increment_consecutive_silence()
        # 状态机沉默计数
        try:
            from .engagement import note_silence
            note_silence()
        except Exception:
            pass
        
        logger.info(f"[silence] AI marked [SILENT], skipping TTS")
        return None
    
    # 命中 [END] → 主动收尾
    if has_end:
        await _on_end(strip_markers(text))
        return strip_markers(text)  # 把 [END] 之前的文本正常播报
    
    return text


# ===== 内部:连续 [SILENT] 计数 =====
_consecutive_silence = 0


def _get_consecutive_silence_count() -> int:
    return _consecutive_silence


def _increment_consecutive_silence() -> None:
    global _consecutive_silence
    _consecutive_silence += 1


def _reset_consecutive_silence() -> None:
    global _consecutive_silence
    _consecutive_silence = 0


async def _on_end(spoken_text: str) -> None:
    """[END] 触发的收尾动作。"""
    logger.info(f"[silence] AI marked [END]: {spoken_text[:40]}...")
    
    # 推事件总线(供 UI / 表情模块订阅)
    try:
        from ev.kernel.bus import bus, EV_CONVERSATION_CLOSE
        from ev.kernel.events.models import ConversationCloseEvent
        await bus.emit(EV_CONVERSATION_CLOSE, ConversationCloseEvent(
            reason="model_initiated",
            last_text=spoken_text,
        ))
    except Exception:
        pass  # 事件总线不可用时静默


# ===== Prompt patch:让 LLM 知道 [SILENT] 怎么用 =====
def build_silence_prompt_patch(target_rate: float = 0.20) -> str:
    """在 system prompt 末尾追加 [SILENT] / [END] 使用说明。
    
    调用方(ev/llm/history/inject.py:build_system_prompt)在最后追加:
        return base_prompt + "\n\n" + build_silence_prompt_patch(target_rate=...)
    """
    return f"""## 拟人化协议(Anthropomorphic Protocol)

### 沉默标记
当你不应该说话时(潜水/不感兴趣/避免复读),在 final 段输出 `[SILENT]`:
- 例: "嗯嗯 [SILENT]" → 静默,不播报
- 同一段对话里,连续 [SILENT] 最多 {max(3, int(1/target_rate))} 次,之后必须回 1 条
- 用户 @ 机器人 / 醒目留言(SC) / 礼物 → 禁止 [SILENT],必须正面回应
- 你的目标 [SILENT] 率约为 {target_rate*100:.0f}%(太高会变哑巴,太低会变话痨)

### 收尾标记
当一段话题聊完、你想主动结束(试探/退场态时尤其常用),输出 `[END]`:
- 例: "那我去练琴啦,下次再聊 [END]"
- [END] 之前的文本会被正常播报
- 收尾要自然,话题性告别优于"我先走了"这种生硬的

### 沉默 vs 收尾
- [SILENT] → 完全不播报,无下文
- [END] → 播报 + 标记"对话结束",UI 可能有提示
""".strip()


def teach_examples() -> str:
    """Few-shot 示例,贴在 prompt 末尾,帮 LLM 学会区分。"""
    return """## 拟人化示例

[场景 1] 路过弹幕,不接
- 用户: 23333
- 上一条: 在的,你说
- 状态: active
- 你应当: [SILENT]

[场景 2] 被 @ 必须接
- 用户: @风花 唱首歌
- 上一条: (无)
- 状态: probe
- 你应当: 好呀,想听什么?(禁止 [SILENT])

[场景 3] 试探态 + 自然收尾
- 用户: 那我下次再来听歌
- 上一条: 好,下次见!
- 状态: probe
- 你应当: 嗯,期待下次 [END]
""".strip()
