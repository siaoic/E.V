"""chat_stream 尾部收尾：历史保存、裁剪、落盘、复盘调度、性能报告打印。

无 yield，仅消费 state 完成副作用。与 inner_loop.py 在循环外的 yield 之后衔接。
"""

import asyncio
import json

from plugins.builtin.tools.sfx import strip_sfx_markers
from ev.llm.tools.formatter import _summarize_tool_content
from ev.llm.tool_message_utils import trim_messages_preserving_tool_rounds
from ev.llm.utils.constants import _SUMMARIZE_MIN_TURNS
from ev.utils import console


async def _chat_stream_tail(self, state: dict) -> None:
    """_chat_stream_inner 的尾部副作用（无 yield）。

    state 必须包含：messages / history / user_text / proactive / final_reply /
                    sound_effect_used / tracker / sentence_count / tool_call_total。
    """
    messages = state["messages"]
    history = state["history"]
    user_text = state["user_text"]
    proactive = state["proactive"]
    final_reply = state["final_reply"]
    tracker = state["tracker"]

    # ===== 历史保存完整工具链（对标 live-2d(2)：
    # assistant+tool_calls + tool 响应都进历史，跨轮保留上下文）=====
    if final_reply is not None:
        # 音效标记（{{sfx:编号}}）只服务于本轮 TTS 播放，不写入历史，
        # 避免标记污染后续轮次的 LLM 上下文/记忆
        messages.append({
            "role": "assistant",
            "content": strip_sfx_markers(final_reply),
        })
    if history is not None:
        # 用快照发起的请求（agent 主动发言）：只把本轮新产出的消息并入
        # 真实历史，不能整体替换——否则被精简掉的早期轮次会丢失
        new_parts = messages[1 + len(history):]
        self.history = self.history + [
            m for m in new_parts
            if not (m.get("role") == "user"
                    and m.get("content") == user_text)
        ]
    else:
        # 丢弃 system（每轮按最新记忆重建），其余完整保留
        self.history = messages[1:]
        if proactive:
            # 主动发言：剔除注入的内部指令 user 消息（不冒充用户发言），
            # 保留 assistant 回复以维持上下文连贯
            self.history = [m for m in self.history
                            if not (m.get("role") == "user"
                                    and m.get("content") == user_text)]
    # 工具结果历史摘要化（对标 NagaAgent「消除历史污染」）：
    # 跨轮次历史中的 tool 消息只保留结果摘要（截断超长 JSON），
    # 防 token 污染；本轮内 messages 保持完整，不影响多轮工具调用链。
    self.history = [
        {**m, "content": _summarize_tool_content(m["content"])}
        if m.get("role") == "tool" else m
        for m in self.history
    ]
    max_messages = self.cfg.HISTORY_ROUNDS * 2
    if len(self.history) > max_messages:
        dropped = self.history[: len(self.history) - max_messages]
        # 以「单元」为粒度从后向前裁剪，不切断工具调用链
        self.history = trim_messages_preserving_tool_rounds(
            self.history, max_messages
        )
        # 被裁剪的早期轮次足够多时，后台压缩成摘要（不阻塞本轮，
        # 下一轮对话开始时若已生成则注入）
        if self._summary_task is None and len(dropped) >= _SUMMARIZE_MIN_TURNS:
            self._summary_task = asyncio.create_task(
                self._summarize_dropped(dropped)
            )

    # ===== L3 会话历史落盘（Hermes 式 state.db，SQLite+FTS5 全文检索）=====
    # 只落盘本轮新增消息（与 history 相同的起点），完整保留工具调用链
    # （tool_call 序列化）；注入的内部指令 user 消息不入库。后台任务执行，
    # 失败仅记日志，不影响主链路（默认开启，MEMORY_HISTORY_ENABLED 关闭跳过）。
    if (final_reply is not None
            and getattr(self.cfg, "MEMORY_HISTORY_ENABLED", True)):
        start = 1 + len(history) if history is not None else 1
        new_rows = [
            {"role": m.get("role"),
             "content": (_summarize_tool_content(m["content"])
                         if m.get("role") == "tool"
                         else (m.get("content") or "")),
             "tool_call": json.dumps(m.get("tool_calls"), ensure_ascii=False)
                          if m.get("tool_calls") else None}
            for m in messages[start:]
            if m.get("role") in ("user", "assistant", "tool")
            and not (m.get("role") == "user"
                     and m.get("content") == user_text)
        ]
        if new_rows:
            self._session_persist_task = asyncio.create_task(
                self._persist_session_history(new_rows)
            )
    # L4 治理（会后秘书）：低频后台复盘，把值得长期记住的事实写入 L2。
    # 冻结快照保证当前会话 prompt 不变，沉淀只在下一次会话生效。
    self._schedule_curator_review()

    # 打印 LLM 性能报告
    tracker.print_report()
