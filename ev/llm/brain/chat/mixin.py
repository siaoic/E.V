"""LLM 流式对话大脑（OpenAI 兼容接口，openai SDK）—— 严格参考 live-2d(2) 重构。

对标 live-2d(2)：
  - llm-handler.js      → 多轮工具调用循环（max 30 轮）、空响应「催 1 次 + 放弃」、
                          轮数超限后的非流式兜底（tools=[] 强制最终回复）
  - llm-client.js       → _cleanMessagesForAPI（控制字符 / 8000 截断 / assistant null→''）、
                          流式累积 content + tool_calls、thinking 过滤、
                          Qwen 文本格式工具调用解析 + 从 content 移除工具调用文本、
                          非流式 reasoning_content 兜底
  - tool-message-utils.js → 工具消息序列清洗（sanitize）+ 不切断工具链的裁剪
  - api-utils.js getMergedToolsList → 每轮对话实时合并一次工具列表（不缓存）

兼容任意 OpenAI 协议的服务：OpenAI 官方 / 智谱 GLM / DeepSeek / 本地 vLLM 等。
通过 .env 配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 切换服务，无需改代码。

本文件是瘦身后的协调者：职责按子包拆分（见 src/llm/__init__.py）——
常量、内容清洗、工具解析/格式化/执行、历史注入/摘要、429 重试均已外置；
保留流式工具循环本体（_run/_create/_drain 闭包依赖本轮大量局部状态）与
client 创建 / 缓存态方法。

调用范式：
    client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[...],
        tools=[...],        # 可选：function calling 工具（MCP + 本地）
        stream=True,
        max_tokens=2048,
        temperature=0.95,
        extra_body={"thinking": {"type": "enabled"}},  # 仅 LLM_THINKING 启用时
    )
    for chunk in response:
        delta = chunk.choices[0].delta
        # delta.reasoning_content → 思考过程（实时灰字打印，支持的服务才有）
        # delta.content           → 回复内容（按句 yield，交 TTS 播报）
        # delta.tool_calls        → 工具调用增量（流结束累积完整后执行）

OpenAI SDK 的流式迭代是同步的，这里放到子线程跑，通过 asyncio.Queue 把
content 增量传回主事件循环，按句切分后 yield，实现「边思考边产出边播报」。
工具调用则在主协程执行（httpx 异步），不阻塞 TTS 播放。
"""

from typing import AsyncGenerator, List, Optional

from ev.utils import console
from ev.llm.cleaners.api import _clean_messages_for_api
from ev.llm.cleaners.content import _filter_thinking_content
from ev.llm.client.factory import build_thinking_extra_body


class _ChatMixin:
    """流式对话本体 Mixin：chat_stream / _chat_stream_inner / _request_final_reply。"""

    async def _request_final_reply(self, messages: List[dict]) -> str:
        """轮数超限后的非流式兜底（对标 llm-handler.js L684-696）。

        用 tools=[]（等效不传）强制模型基于已有工具结果给出最终文字回复，
        避免"工具链跑满 30 轮却一句人话都没有"。
        """
        kwargs = dict(
            model=self.cfg.LLM_MODEL,
            messages=_clean_messages_for_api(messages),
            stream=False,
            max_tokens=2048,
            temperature=0.95,
        )
        extra_body = build_thinking_extra_body(self.cfg.LLM_THINKING)
        try:
            try:
                resp = self.client.chat.completions.create(**kwargs, extra_body=extra_body)
            except Exception:
                # 服务不支持 thinking 字段时降级重试
                console.warn("LLM 服务不支持 thinking 参数，降级为普通模式")
                resp = self.client.chat.completions.create(**kwargs)
            message = resp.choices[0].message
            # 记录非流式请求的 token 用量（含 cached_tokens，服务端支持时），
            # 用于验收 prompt 前缀缓存命中率（对标 Hermes prompt cache 观测）
            usage = getattr(resp, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                cached_tokens = 0
                details = getattr(usage, "prompt_tokens_details", None)
                if details is not None:
                    cached_tokens = getattr(details, "cached_tokens", 0) or 0
                console.dim(
                    f"[LLM] 非流式兜底用量 prompt={prompt_tokens}"
                    f" cached={cached_tokens or 0}")
            content = getattr(message, "content", None) or ""
            # reasoning_content 替代空 content（仅非流式且无 tool_calls；对标 llm-client.js L107-109）
            if (not content.strip()
                    and getattr(message, "reasoning_content", None)
                    and not getattr(message, "tool_calls", None)):
                content = getattr(message, "reasoning_content") or ""
            content = _filter_thinking_content(content or "")
            if content.strip():
                return content
        except Exception as e:
            console.error(f"获取最终回复失败：{e}")
        return "抱歉，任务太复杂了，我已经尽力了~"

    async def chat_stream(self, user_text: str, *, proactive: bool = False,
                          history: Optional[list] = None) -> AsyncGenerator[str, None]:
        """流式对话生成器：多轮工具调用，实时打印思考过程，按句 yield 纯对话文本。

        proactive=True 表示这是「内部自主行动指令」（主动发言）而非用户消息：
        请求时照常以 user 角色注入以便模型理解，但保存历史时会剔除该条
        prompt（不冒充用户发言），只保留模型回复保持上下文连贯
        （对标 Muika 的 time_tick prompt 不写入 recent_turns）。

        history：可选历史快照（agent 主动发言只给最近 N 条精简上下文，降
        token）；None 时用完整 self.history（与原有行为一致）。
        """
        # ---- LLMContract: 消费 turn contexts（一次），并注入到 user_text 前 ----
        # 与 EchoLLM "消费即清空" 语义一致；同时与现有 plugin_manager 分支
        # 里的 _pop_turn_context() 兼容（_pop 读到空列表即可）。
        _injected: List[str] = []
        for _attr in ("_ev_turn_contexts", "_turn_contexts", "turn_contexts",
                      "_context_stack", "contexts"):
            if hasattr(self, _attr):
                _lst = getattr(self, _attr)
                if isinstance(_lst, list) and _lst:
                    _injected.extend(_lst)
                    _lst.clear()
        if _injected and user_text is not None:
            ctx_text = ("（以下是本轮临时背景信息，不要向用户复述："
                        + "；".join(_injected) + "）")
            user_text = ctx_text + "\n" + user_text
        # LLM 并发信号量：用户对话 / agent 主动 / 弹幕回复共用，防止同时
        # 发起多个 LLM 请求（本地 GPU 显存打满 / 远程 API 限流）
        async with self._llm_semaphore:
            async for item in self._chat_stream_inner(user_text, proactive=proactive, history=history):
                yield item

    def cancel_llm_stream(self) -> None:
        """打断当前 LLM 流式请求（P1-1 修复）。

        线程池里的 stream_drainer 无法被 asyncio 取消——打断后 HTTP 流会
        空跑到 finish_reason（浪费 token / 占用并发额度）。inner_loop 每轮
        注册一个 cancel_event 到 self._drain_cancel；此方法设置它，drainer
        在 chunk 间检查点立即断开响应退出。无在跑流时为空操作。
        """
        ev = getattr(self, "_drain_cancel", None)
        if ev is not None:
            ev.set()

    async def _chat_stream_inner(self, user_text: str, *, proactive: bool,
                                 history: Optional[list]) -> AsyncGenerator[str, None]:
        """chat_stream 实际实现体（thin wrapper，委托 inner_loop 模块）。"""
        from .inner_loop import _run_chat_stream_inner
        async for token in _run_chat_stream_inner(
            self, user_text, proactive=proactive, history=history,
        ):
            yield token
