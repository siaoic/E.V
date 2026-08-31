"""ev.social.proactive — 主动对话引擎适配层。

主项目已把 ProactiveEngine 重构在 ev/llm/proactive/（core + executor +
policies，Neuro 风格契机门控 + 被动 request_speak），本模块提供：

  - ProactiveEngine          主项目引擎的再导出（接口 1:1）
  - set/get_proactive_engine 全局单例（bootstrap 注册，wake 工具 / nudge 用）
  - request_speak(...)       模块级便捷入口（LLM 工具调用走这里）

不复制引擎本体：参考实现是完全被动版（heartbeat noop），主项目版是
契机门控心跳 + 被动 request_speak 的超集，行为覆盖参考实现。
"""

from __future__ import annotations

from typing import Optional

from ev.llm.proactive import ProactiveEngine  # noqa: F401


_proactive_engine: Optional[ProactiveEngine] = None


def set_proactive_engine(engine: Optional[ProactiveEngine]) -> None:
    """设置全局实例（application 启动时调用）。"""
    global _proactive_engine
    _proactive_engine = engine


def get_proactive_engine() -> Optional[ProactiveEngine]:
    """获取全局实例；未注册返回 None（工具层返回 ok=False）。"""
    return _proactive_engine


async def request_speak(topic_hint: str = "", reason: str = "",
                        nudge_reason: str = "") -> dict:
    """模块级 request_speak：委托全局引擎；未注册时返回 ok=False。"""
    engine = get_proactive_engine()
    if engine is None:
        return {"ok": False, "reason": "no_engine"}
    try:
        return await engine.request_speak(
            topic_hint=topic_hint, reason=reason, nudge_reason=nudge_reason)
    except Exception as e:
        return {"ok": False, "reason": "error", "error": str(e)}


__all__ = [
    "ProactiveEngine",
    "set_proactive_engine",
    "get_proactive_engine",
    "request_speak",
]
