"""E.V 拟人化层（ev.social）— 完全事件驱动 + Neuro-sama 风格。

基于 qq-bridge 的 "agent-driven" 思路：AI 自己决定什么时候说话、什么时候潜水；
加上 Nudge 契机引擎：系统主动给 LLM 创造「说话契机」，实现 Neuro 式「随时说话」。

模块清单（nudge/proactive 为 ev.llm.proactive 已有实现的兼容再导出层）：
  1. wake         潜水/唤醒（单次 wait_for + 事件 poke 打断）
  2. engagement   参与度状态机（事件触发评估，无 tick_loop）
  3. nudge        ⭐ Neuro 风格契机引擎（→ ev.llm.proactive.nudge 再导出）
  4. deliberation 选择性决策（弹幕打分，过线才送 LLM）
  5. silence      [SILENT] / [END] 标记协议
  6. learning     社会学习（弹幕新词 → 词库 → prompt 注入）
  7. quote        引用回复（@ / 引用 / 叫名 / SC / 礼物）
  8. personalize  读 SKILL.md 拟人化参数
  9. proactive    主动对话引擎（→ ev.llm.proactive.ProactiveEngine 再导出）
 10. mcp_speak    把 4 个发言工具暴露给 LLM
 11. bootstrap    启动入口（application 启动时调用一次）

主循环接入（全部失败兜底、零破坏）：
  - 弹幕到达 → ev.social.observe_danmaku(text, ...)（engagement/learning/wake 埋点）
  - 弹幕过滤（可选）→ ev.social.should_block(danmaku)
  - 事件到来 → await ev.social.on_event("danmaku", {...})
  - 事件处理后（可选）→ await ev.social.on_post_event(...) → handle_nudge
  - 空闲等待（可选）→ await ev.social.wait_for_window()
  - AI 想说话 → 调 request_speak 工具
  - AI 想潜水 → 调 set_wake_config 工具
  - LLM 想自己检查契机 → 调 nudge_check 工具

设计原则：
  - 零定时器：没有任何 setInterval / 循环 sleep
  - 零破坏：全部以事件订阅 / 工具调用接入，失败 bypass 回原 E.V
  - 主动驱动：系统给 LLM 创造契机，但不强制（LLM 可 [SILENT] 拒绝）
"""

from .engagement import (
    STATE_OBSERVE, STATE_ACTIVE, STATE_PROBE, STATE_EXIT, STATE_SLEEP,
    get_state as get_engagement_state,
    transition_to, is_proactive_allowed, is_engagement_silent,
    get_dialogue_openness, on_event,
)
from .wake import (
    WakeMode, WakeConfig,
    get_wake_config, is_diving, is_sleeping,
    set_wake_config, cancel_wake, poke, check_should_wake,
    wait_for_window, get_tool_definitions, handle_tool_call,
)
from .nudge import (
    NudgeReason, NudgeEvent, NudgeEngine,
    get_engine as get_nudge_engine, reset_engine as reset_nudge_engine,
)
from .deliberation import score as deliberation_score, should_pass
from .silence import on_ai_final, detect_silence, detect_end
from .learning import observe_danmaku, recall_lexicon
from .quote import detect_quote_signal, build_quote_context
from .personalize import PersonaParams, load_persona_params
from .proactive import (
    ProactiveEngine, get_proactive_engine, set_proactive_engine,
)
from .mcp_speak import (
    get_speak_tool_definitions, handle_speak_tool_call, register_speak_tools,
    install_local_tools, install_speak_tools_at_startup,
)
from .bootstrap import (
    bootstrap, bootstrap_lazy, is_enabled,
    on_event as social_on_event, on_post_event, handle_nudge,
    wait_for_window as social_wait_for_window,
    observe_danmaku, should_block,
)

__all__ = [
    # engagement
    "STATE_OBSERVE", "STATE_ACTIVE", "STATE_PROBE", "STATE_EXIT", "STATE_SLEEP",
    "get_engagement_state", "transition_to", "is_proactive_allowed",
    "is_engagement_silent", "get_dialogue_openness", "on_event",
    # wake
    "WakeMode", "WakeConfig",
    "get_wake_config", "is_diving", "is_sleeping",
    "set_wake_config", "cancel_wake", "poke", "check_should_wake",
    "wait_for_window", "get_tool_definitions", "handle_tool_call",
    # nudge
    "NudgeReason", "NudgeEvent", "NudgeEngine",
    "get_nudge_engine", "reset_nudge_engine",
    # deliberation
    "deliberation_score", "should_pass",
    # silence
    "on_ai_final", "detect_silence", "detect_end",
    # learning
    "observe_danmaku", "recall_lexicon",
    # quote
    "detect_quote_signal", "build_quote_context",
    # persona
    "PersonaParams", "load_persona_params",
    # proactive
    "ProactiveEngine", "get_proactive_engine", "set_proactive_engine",
    # mcp_speak
    "get_speak_tool_definitions", "handle_speak_tool_call",
    "register_speak_tools", "install_local_tools",
    "install_speak_tools_at_startup",
    # bootstrap
    "bootstrap", "bootstrap_lazy", "is_enabled",
    # main loop helpers
    "social_on_event", "on_post_event", "handle_nudge",
    "social_wait_for_window", "observe_danmaku", "should_block",
]
