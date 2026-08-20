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

import asyncio
import time
from typing import AsyncGenerator, List, Optional

from src.utils import config, console
from tools.memory import memory
from src.adapter.llm import BaseLLMAdapter
from plugins.tools.skills import get_skill_manager
from plugins.tools.sfx import strip_sfx_markers
from src.llm.cleaners.api import _clean_messages_for_api
from src.llm.cleaners.content import (
    _clean_sentence,
    _filter_thinking_content,
    _remove_tool_calls_from_content,
)
from src.llm.cleaners.sentence import (
    _find_pause_end_from,
    _find_sentence_end_from,
    _split_sentences,
)
from src.llm.client.factory import (
    build_thinking_extra_body,
    get_openai_client,
)
from src.llm.client.retry import _parse_retry_after
from src.llm.utils.constants import (
    _MAX_429_WAIT,
    _MAX_TOOL_ITERATIONS,
    _SUMMARIZE_MIN_TURNS,
)
from src.llm.history.inject import _InjectionMixin
from src.llm.history.summary import _SummaryMixin
from src.llm.tools.executor import _execute_tool_calls
from src.llm.tools.formatter import _format_tool_calls, _summarize_tool_content
from src.llm.tools.parser import _parse_qwen_tool_calls
from src.llm.utils.content_check import has_content
from src.llm.tool_message_utils import trim_messages_preserving_tool_rounds
from src.utils.perf_tracker import PerfTracker

# 记忆召回硬超时（秒）：超时熔断跳过注入，保障首字延迟不被检索拖垮
_MEMORY_RECALL_TIMEOUT = 1.5

# 段落早产切分（压 TTS 首句延迟，让「从 LLM 第一个字开始合成」落地）：
# LLM 流式产字不等句末，尽早切出可合成段交 TTS（首块 ~200ms 即出声）——
# - 句末标点（。！？…）必切（原有行为，最自然边界）；
# - 停顿标点（逗号/顿号）且当前段 ≥ _PAUSE_SEGMENT_MIN_CHARS 字时切：
#   逗号是中文自然停顿点，切成独立段合成播放不显突兀；
# - 首段早产：首个可合成段攒够 _FIRST_SEGMENT_MIN_CHARS 字就硬切（LLM 还在
#   继续吐），不等待整句生成完；
# - 无标点超长段（≥ _MAX_SEGMENT_CHARS）强制切，防播放头被长句拖住。
_FIRST_SEGMENT_MIN_CHARS = 6    # 首段早产字数（太小 GSV 短文本合成不稳）
_PAUSE_SEGMENT_MIN_CHARS = 4    # 停顿标点切段的最短段长（防"啊，嗯，"被单切）
_MAX_SEGMENT_CHARS = 30         # 无标点强制切段上限


class LLMBrain(BaseLLMAdapter, _InjectionMixin, _SummaryMixin):
    """LLM 流式大脑：支持多轮工具调用，按句 yield 纯对话文本。"""

    def __init__(self, mcp=None) -> None:
        self.cfg = config.cfg
        # 延迟导入，避免缺包时整个程序无法启动提示
        self.client = get_openai_client(
            api_key=self.cfg.LLM_API_KEY,
            base_url=self.cfg.LLM_BASE_URL,
            timeout=120.0,
            # 重试次数必须小：免费档限流(429)时，SDK 会按 1s/2s/4s…指数退避
            # 悄悄重试，max_retries=5 最多可干等 ~30s 才报错——正是"很慢"的元凶。
            # 只留 1 次重试，真限流立刻走友好提示，而不是把几秒耗在空等上。
            max_retries=2,
        )
        self.history: list = []
        # LLM 并发信号量（.env LLM_MAX_CONCURRENCY，默认 2）：用户对话 + agent
        # 主动 + 弹幕回复共用，同时最多 N 个 LLM 推理，超出排队等待而非无限
        # 并发——本地大模型防显存打满、远程 API 防限流。
        self._llm_semaphore = asyncio.Semaphore(
            max(1, int(config.cfg.LLM_MAX_CONCURRENCY or 2)))
        # 长对话摘要压缩（对标 NagaAgent 上下文压缩）：
        # 历史超长被裁剪时，后台把丢弃的早期轮次压缩成摘要，
        # 下一轮以 system 段注入，避免长聊后早期信息永久丢失；
        # 摘要同时写入记忆实现跨会话继承。
        self._session_summary: Optional[str] = None
        self._summary_task: Optional[asyncio.Task] = None
        # MCP 管理器（外部工具服务器）；None 表示禁用
        self.mcp = mcp
        # 插件系统（Application 启动后注入）：钩子分发与系统提示补丁
        self.plugin_manager = None
        # 插件 on_user_input 注入的本轮背景上下文（一次性消费，下轮自动清除）
        self._turn_contexts: List[str] = []
        # 生效话术建议缓存（进化引擎写入 active json，30s TTL 防每轮读文件）
        self._advice_cache_ts = 0.0
        self._advice_cache: list[str] = []
        # 观众画像缓存（进化引擎写入 profile json，30s TTL 防每轮读文件）
        self._profile_cache_ts = 0.0
        self._profile_cache: list[dict] = []
        # GEPA 进化策略段缓存（prompt_evo.py 写入 policy json，30s TTL）
        self._policy_cache_ts = 0.0
        self._policy_cache: str = ""
        # 模型路由进化（多臂老虎机）：配置多 LLM 服务时按历史表现选服务；
        # 未配置/未启用时 router 为 None，完全走原有单一 LLM 服务逻辑
        from src.llm.utils.model_router import get_router
        self.router = get_router()

    def reload_client(self) -> None:
        """控制中心「更新配置」热更新后重建 OpenAI client。

        API Key / Base URL / 模型名变化时 client 需重建；LLM_MODEL 变化
        也会随 cfg 单例在下一轮对话读取时自动生效。
        """
        self.cfg = config.cfg  # 指向 reload 后的最新单例
        self.client = get_openai_client(
            api_key=self.cfg.LLM_API_KEY,
            base_url=self.cfg.LLM_BASE_URL,
            timeout=120.0,
            max_retries=2,
        )

    async def warmup(self) -> None:
        """启动时预热 LLM 连接：发一个最小请求建立 TLS/HTTP 连接。

        首轮对话的 TTFT 比后续轮慢 ~1s，主要来自握手冷启动（实测首轮
        2565ms → 热连接后 959-1440ms）。启动后台预热一次，把冷启动
        挪到空闲期，用户第一次提问即命中热连接。失败静默（不影响启动）。
        """
        if not self.cfg.LLM_API_KEY:
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=self.cfg.LLM_MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                ),
                timeout=10,
            )
        except Exception:
            pass  # 预热失败无影响：首次真实对话再建连

    # ---------- 插件：本轮上下文 ----------

    def push_turn_context(self, contexts: List[str]) -> None:
        """追加插件 on_user_input 注入的本轮背景上下文（仅本轮对话生效）。"""
        self._turn_contexts.extend(contexts)

    def _pop_turn_context(self) -> List[str]:
        """取走本轮注入的上下文并清空（一次性消费）。"""
        contexts, self._turn_contexts = self._turn_contexts, []
        return contexts

    # ---------- 工具 ----------

    def _get_tools(self) -> List[dict]:
        """合并 MCP + 本地工具（对标 live-2d(2) getMergedToolsList）。

        每轮对话实时获取一次（不缓存）：MCP 服务器可能在运行中动态增删工具。
        """
        from plugins.tools import get_merged_tools
        return get_merged_tools(self.mcp)

    @staticmethod
    def _describe_mcp_servers(mcp) -> str:
        """取 MCP 服务器能力说明（供 system prompt 注入，见 _chat_stream_inner）。"""
        from src.mcp.llm_bridge import describe_mcp_servers
        return describe_mcp_servers(mcp)

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

    # ---------- 流式对话 ----------

    def _max_tool_iterations(self) -> int:
        """工具调用轮数上限（对标 NagaAgent max_loop_stream：.env TOOL_MAX_ITERATIONS 可配，默认 30）。"""
        try:
            return max(1, int(config.cfg.TOOL_MAX_ITERATIONS or _MAX_TOOL_ITERATIONS))
        except (TypeError, ValueError, AttributeError):
            return _MAX_TOOL_ITERATIONS

    def _skill_intent_hint(self, user_text: str) -> str:
        """本地技能意图预判（match_intent，零依赖）：命中时返回「优先加载」提示段。

        不命中返回空串（行为与未接入前完全一致）。提示段作为独立 system 消息
        追加在 user 之后（近因效应），要求模型先用 load_skill 加载命中技能，
        不再只依赖模型从常驻技能列表里自觉想起。
        """
        hit = get_skill_manager().match_intent(user_text)
        if hit is None:
            return ""
        return (f"【技能预判】检测到用户输入与技能「{hit.name}」的触发时机匹配，"
                f"请先用 load_skill 加载该技能，再严格按其指令执行。")

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
        # LLM 并发信号量：用户对话 / agent 主动 / 弹幕回复共用，防止同时
        # 发起多个 LLM 请求（本地 GPU 显存打满 / 远程 API 限流）
        async with self._llm_semaphore:
            async for item in self._chat_stream_inner(user_text, proactive=proactive, history=history):
                yield item

    async def _chat_stream_inner(self, user_text: str, *, proactive: bool,
                                 history: Optional[list]) -> AsyncGenerator[str, None]:
        """chat_stream 实际实现体（信号量外），保持原有逻辑不变。"""
        tracker = PerfTracker("LLM")
        tracker.begin("首字延迟")     # 从调用到第一个 content chunk
        tracker.begin("总生成")       # 整个流程（含工具调用）耗时

        # 注入记忆上下文（严格参照 memU hosts/instruction.py 的 standing instruction）：
        # 1) 记忆使用说明常驻系统提示（segments/files 两层渐进 + fail-open）；
        # 2) 检索结果按 memU hosts/retrieval.py _shape_for_agent 的三层形状注入。
        #    Embedding 不可用/失败时自动回退 LLM 检索（仍输出同形状）。
        sys_content = self.cfg.SYSTEM_PROMPT
        # 每轮对话实时合并一次工具列表（对标 live-2d(2) getMergedToolsList，不缓存）
        tools = self._get_tools()
        # 有工具可用时注入能力引导：角色人设（尤其 Neuro-sama 这类"不是通用
        # 助手"人设）常导致模型遇到搜索/查资料需求直接道歉"我无法搜索"，
        # 明明有 bing_search 等工具却从不调用。引导段只在有工具时注入。
        if tools:
            sys_content += (
                """                
                \n\n### 工具使用\n
                你可以调用函数工具完成实际任务（联网搜索、抓取网页、查询时间天气、
                加载技能、读写记忆等）。当用户问需要实时/最新信息、新闻、资料、
                事实核查的问题时，必须先调用下方列出的搜索/抓取网页工具获取真实
                结果再回答，不要说自己无法联网搜索——工具列表已提供给你。"""
            )
            # 工具使用时机清单：逐条列出当前可用工具 + 触发时机（与 function
            # calling 同源，弥补引导段不列具体清单、模型不知何时该调哪个的缺口）
            from plugins.tools import render_tool_guide
            tool_guide = render_tool_guide(tools)
            if tool_guide:
                sys_content += "\n\n" + tool_guide
            # 注入 MCP 服务器能力说明（mcp_config.json 的 description 字段），
            # 让模型明确知道每台服务器能做什么、有哪些工具可调用
            mcp_desc = self._describe_mcp_servers(self.mcp)
            if mcp_desc:
                sys_content += "\n\n### 可用的联网服务器\n" + mcp_desc
        # 知识库注入：信号闸门命中才追加权威设定段（防幻觉；闲聊/无关消息
        # 返回空串不注入，省 Token）。数据懒加载，进程内缓存一次。
        knowledge_section = self._knowledge_section(user_text)
        if knowledge_section:
            sys_content += "\n\n" + knowledge_section
        if self.cfg.MEMORY_ENABLED:
            sys_content += "\n\n" + memory.STANDING_INSTRUCTION
            try:
                # 硬超时熔断：召回超过 1.5s 直接跳过注入，优先保障响应速度
                mem_ctx = await asyncio.wait_for(
                    memory.retrieve(user_text), timeout=_MEMORY_RECALL_TIMEOUT)
            except asyncio.TimeoutError:
                console.warn("[记忆检索] 召回超时（>1.5s），熔断跳过本次注入")
                mem_ctx = ""
            # 记忆写入完全交给管家模型（ButlerAgent 每轮从对话提取，参照
            # <memory> 标签（曾在此注入写标签指令，主模型经常漏写/写错）
            if mem_ctx:
                sys_content += (
                    "\n\n### 检索到的记忆（segments / files，按相关度排序）\n"
                    + mem_ctx
                )
        # 注入观众画像（进化引擎复盘的长期事实，关键词召回补充向量记忆）
        profile_section = self._profile_section(user_text)
        if profile_section:
            sys_content += "\n\n" + profile_section
        # 注入技能段（严格参照 Muika agent.py：系统提示 = 人设 + Available skills 段）。
        # 只列技能名+描述（轻量），完整指令由 load_skill 工具按需加载。
        skills_section = get_skill_manager().render_prompt_section()
        if skills_section:
            sys_content += "\n\n" + skills_section
        # 注入生效中的话术建议（进化引擎沉淀，到期由复盘回评续期/移除）
        advice_section = self._active_advice_section()
        if advice_section:
            sys_content += "\n\n" + advice_section
        # 注入 GEPA 进化策略段（对标 hermes 的 GEPA：变异 → 评审择优落盘，
        # 与话术建议互补——策略是长期行为准则，建议是短期话术优化）
        policy_section = self._policy_section()
        if policy_section:
            sys_content += "\n\n" + policy_section
        # 插件注入：on_user_input 本轮背景上下文（一次性）+ add_system_prompt_patch 长期提示
        if self.plugin_manager is not None:
            turn_contexts = self._pop_turn_context()
            if turn_contexts:
                sys_content += ("\n\n### 插件补充背景（仅本轮对话参考，不要向用户复述）\n"
                                + "\n".join(f"- {t}" for t in turn_contexts))
            patch_section = self.plugin_manager.system_prompt_patch_section()
            if patch_section:
                sys_content += "\n\n" + patch_section
        messages: List[dict] = [{"role": "system", "content": sys_content}]
        # 注入早期对话摘要（历史裁剪时后台压缩生成，跨会话继承）
        summary = self._consume_summary()
        if summary:
            messages.append({
                "role": "system",
                "content": f"【早期对话摘要（以下为被压缩掉的更早对话内容）】{summary}",
            })
        # agent 主动发言：只带精简历史快照（最近 N 条），降低 token 消耗
        messages.extend(history if history is not None else self.history)
        messages.append({"role": "user", "content": user_text})
        # 技能意图预判（本地 match_intent，零依赖）：命中时在 user 后追加
        # 提示段（近因效应），要求模型优先 load_skill——主动发言是 AI 内部
        # 指令非用户输入，不参与预判，避免误匹配。
        if not proactive:
            skill_hint = self._skill_intent_hint(user_text)
            if skill_hint:
                messages.append({"role": "system", "content": skill_hint})
        # Author's Note 尾部人设锚点（近因效应）：追加在 user 消息之后，
        # 比 system prompt 开头更有效锁定语气/格式；默认空 = 不注入。
        anchor = self._tail_anchor_section()
        if anchor:
            messages.append({
                "role": "system",
                "content": f"【人设锚点（务必遵守）】{anchor}",
            })

        # 插件钩子：on_llm_request（可在请求发出前修改 messages）
        if self.plugin_manager is not None:
            from plugins.base import LLMRequestEvent
            request = LLMRequestEvent(messages)
            await self.plugin_manager.run_llm_request_hooks(request)
            messages = request.messages

        # 模型路由进化：本轮按 UCB1 选择要用的 LLM 服务（未启用时均为
        # None → 沿用 self.client / LLM_MODEL，与原有行为完全一致）
        route_name = None
        route_client = None
        route_model = None
        if self.router is not None:
            route_name = self.router.select()
            if route_name is not None:
                route_client = self.router.client_for(route_name)
                service = self.router.service(route_name)
                route_model = (service or {}).get("model") or None

        loop = asyncio.get_running_loop()
        iteration = 0            # 工具调用轮数（含空响应催促轮）
        empty_count = 0          # 连续空响应计数
        tool_call_total = 0      # 工具调用总次数
        sentence_count = 0       # 已产出句数
        final_reply: Optional[str] = None
        sound_effect_used = False  # 本轮已播放音效：抑制音效后的文字/语音回复

        # ===== 多轮工具调用循环（对标 llm-handler.js 的 while (iteration < maxIterations)） =====
        max_tool_iterations = self._max_tool_iterations()
        while iteration < max_tool_iterations:
            q: asyncio.Queue = asyncio.Queue()
            tool_calls_acc: List[Optional[dict]] = []
            full_raw: List[str] = []
            round_content: List[str] = []
            _first_content = True

            def _run(messages=messages, tools=tools) -> None:
                """子线程：同步迭代流式响应，推送 content 增量并累积 tool_calls。"""
                nonlocal _first_content

                def _create():
                    """发起一次带 tools 的流式请求。thinking 字段不被支持时
                    自动降级重试；路由服务整体不可用时回退默认 LLM 服务重试一次。"""
                    nonlocal extra_body, route_name, route_client, route_model
                    client = route_client or self.client
                    model = route_model or self.cfg.LLM_MODEL

                    def _send():
                        kwargs = dict(
                            model=model,
                            messages=_clean_messages_for_api(messages),
                            stream=True,
                            max_tokens=2048,
                            temperature=0.95,
                        )
                        if tools:
                            kwargs["tools"] = tools
                        if extra_body is not None:
                            return client.chat.completions.create(
                                **kwargs, extra_body=extra_body)
                        return client.chat.completions.create(**kwargs)

                    try:
                        return _send()
                    except Exception as e:
                        # 429 是服务端限流不是 thinking 不支持，不能走降级分支
                        # （否则会把 429 误判成「不支持 thinking」打误导警告）
                        from openai import RateLimitError
                        if (extra_body is None
                                or isinstance(e, RateLimitError)
                                or "429" in str(e)):
                            raise
                        console.warn("LLM 服务不支持 thinking 参数，降级为普通模式")
                        extra_body = None
                        try:
                            return _send()
                        except Exception as e2:
                            from openai import RateLimitError
                            if isinstance(e2, RateLimitError) or "429" in str(e2):
                                raise
                            # 路由服务整体不可用 → 记录失败并回退默认服务重试
                            # （route_name 清空后成功路径不再误记到该服务头上）
                            if route_name is not None:
                                self.router.record(route_name, False)
                                console.warn(
                                    f"[模型路由] 服务 {route_name!r} 不可用，"
                                    "回退默认 LLM 服务重试")
                                route_name = None
                                route_client = None
                                route_model = None
                                client = self.client
                                model = self.cfg.LLM_MODEL
                                return _send()
                            raise

                def _push(content: str) -> None:
                    """把 content 增量推回主循环（记录首字延迟）。"""
                    nonlocal _first_content
                    if not content:
                        return
                    if _first_content:               # 首字到达
                        _first_content = False
                        loop.call_soon_threadsafe(tracker.end, "首字延迟")
                    loop.call_soon_threadsafe(q.put_nowait, content)

                try:
                    # —— 组装请求（thinking 显式控制） ——
                    # GLM-4.5/4.7 系列默认「强制思考」：即使不传 thinking 参数，
                    # 模型也会输出思维链（造成"明明关了还在思考"）。
                    # 所以关闭时必须显式传 {"type": "disabled"} 才能真正禁用；
                    # 不支持 thinking 字段的服务（OpenAI 官方等）会 400，自动降级重试。
                    def _drain(response) -> None:
                        """迭代一个流式响应：推送 content 增量 + 累积工具调用。"""
                        for chunk in response:
                            if not getattr(chunk, "choices", None):
                                continue
                            delta = chunk.choices[0].delta
                            reasoning = getattr(delta, "reasoning_content", None) or ""
                            if reasoning:
                                # 思考过程：灰字实时打印
                                print(console.paint(reasoning, console.GRAY), end="", flush=True)
                            content = getattr(delta, "content", None) or ""
                            if content:
                                full_raw.append(content)
                                _push(content)
                            # —— 工具调用增量累积（对标 llm-client.js _handleStreamResponse）——
                            tcs = getattr(delta, "tool_calls", None)
                            if tcs:
                                for tc in tcs:
                                    index = tc.index
                                    while len(tool_calls_acc) <= index:
                                        tool_calls_acc.append(None)
                                    if tool_calls_acc[index] is None:
                                        tool_calls_acc[index] = {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    if getattr(tc, "id", None):
                                        tool_calls_acc[index]["id"] = tc.id
                                    fn = getattr(tc, "function", None)
                                    if fn is not None:
                                        if getattr(fn, "name", None):
                                            tool_calls_acc[index]["function"]["name"] = fn.name
                                        if getattr(fn, "arguments", None):
                                            tool_calls_acc[index]["function"]["arguments"] += fn.arguments

                    # 429 自动等待重试：免费档服务端 1 并发限流（高峰期常触发）。
                    # 按服务端 Retry-After / X-RateLimit-Reset 等待限流窗口结束后
                    # 自动重试，而不是直接中断；总等待封顶 _MAX_429_WAIT 秒，
                    # 超时仍 429 才放弃（走 __RATELIMIT__ 友好提示）。
                    waited = 0.0
                    while True:
                        try:
                            extra_body = build_thinking_extra_body(self.cfg.LLM_THINKING)
                            _drain_start = time.perf_counter()
                            _drain(_create())
                            # 路由服务调用成功 → 记录奖励与耗时（供 UCB1 择优）；
                            # route_name 已被回退清空时不记录（成功属于默认服务）
                            if route_name is not None:
                                self.router.record(
                                    route_name, True,
                                    time.perf_counter() - _drain_start)
                            break
                        except Exception as e:
                            from openai import RateLimitError
                            if not (isinstance(e, RateLimitError) or "429" in str(e)):
                                raise
                            wait = _parse_retry_after(e) or 15.0   # 无头信息用保守默认
                            wait = min(wait, _MAX_429_WAIT - waited)
                            if wait <= 0:
                                raise
                            waited += wait
                            print(console.paint(
                                f"⏳ LLM 限流(429)，等待 {wait:.0f}s 后自动重试…",
                                console.YELLOW), flush=True)
                            time.sleep(wait)
                except Exception as e:
                    # 429 自动重试耗尽 → 中文友好提示；其他错误原样上报
                    from openai import RateLimitError
                    if isinstance(e, RateLimitError) or "429" in str(e):
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            "__RATELIMIT__::你的 LLM 账户达到速率限制（429），"
                            "自动等待重试后仍被限流。\n"
                            "  免费模型限 1 并发，高峰期请稍等片刻再提问。",
                        )
                    else:
                        loop.call_soon_threadsafe(q.put_nowait, f"__ERROR__::{e}")
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, None)  # 结束哨兵

            # 子线程跑同步流式迭代
            bg_task = loop.run_in_executor(None, _run)

            # 首轮思考过程提示
            if iteration == 0 and self.cfg.LLM_THINKING:
                print(console.paint("💭 思考过程：", console.GRAY), end="", flush=True)

            # 主协程：消费 content 增量，按句切分 yield
            buffer = ""
            scanned = 0  # buffer[:scanned] 已确认无句末符号，增量扫描起点

            def _emit(sentence: str) -> str:
                """清洗、保存记忆、计数并返回清理后的文本。空文本返回 ''。"""
                nonlocal sentence_count
                cleaned = _clean_sentence(sentence)
                # 纯标点/符号/空白句（无实质文字）直接丢弃：
                # GPT-SoVITS 对这类文本会合成退化，输出拖长音怪叫
                if not cleaned.strip() or not has_content(cleaned):
                    return ""
                round_content.append(cleaned)
                sentence_count += 1
                return cleaned

            while True:
                item = await q.get()
                if item is None:
                    break
                if isinstance(item, str) and item.startswith("__ERROR__::"):
                    console.error(f"LLM 调用失败：{item[len('__ERROR__::'):]}")
                    await bg_task
                    tracker.end("总生成", f"{sentence_count} 句（出错中断）")
                    tracker.print_report()
                    return
                if isinstance(item, str) and item.startswith("__RATELIMIT__::"):
                    console.warn(item[len("__RATELIMIT__::"):])
                    await bg_task
                    tracker.end("总生成", f"{sentence_count} 句（限流）")
                    tracker.print_report()
                    return
                buffer += item

                # 增量切段（压 TTS 首句延迟）：每 chunk 只扫描新增区域
                # （scanned 之后），切出段后剩余 buffer 从头开始。
                # 边界优先级：句末标点 > 停顿标点（逗号/顿号） > 首段早产 > 超长兜底，
                # 保证 LLM 第一个字到达后尽早产出可合成段，TTS 首块即出声。
                while True:
                    idx = _find_sentence_end_from(buffer, scanned)
                    if idx < 0:
                        if len(buffer) >= _PAUSE_SEGMENT_MIN_CHARS:
                            # 从 0 全扫：句末标点才保证 scanned 前无边界，
                            # 逗号可能出现在旧 buffer（不足 4 字时未切）中
                            idx = _find_pause_end_from(buffer, 0)
                        if idx < 0 and not round_content \
                                and len(buffer) >= _FIRST_SEGMENT_MIN_CHARS:
                            # 首段早产：首个可合成段攒够字数即切（不等句末），
                            # 让 TTS 与 LLM 并行——边生成边合成，首声只等首块
                            idx = len(buffer) - 1
                        if idx < 0 and len(buffer) >= _MAX_SEGMENT_CHARS:
                            idx = len(buffer) - 1  # 无标点超长段兜底
                    if idx < 0:
                        break
                    sentence = buffer[: idx + 1]
                    buffer = buffer[idx + 1:]
                    scanned = 0
                    cleaned = _emit(sentence)
                    if cleaned and not sound_effect_used:
                        yield cleaned
                scanned = len(buffer)

            # 收尾：剩余内容作为最后一句
            cleaned = _emit(buffer)
            if cleaned and not sound_effect_used:
                yield cleaned

            await bg_task

            # ---- 流结束：判断本轮结果（对标 llm-client.js _handleStreamResponse）----
            raw_content = "".join(full_raw)
            # 先过滤思考内容，再解析工具调用（与 JS 顺序一致）
            clean_content = _filter_thinking_content(raw_content)
            tool_calls = [tc for tc in tool_calls_acc if tc and tc["function"]["name"]]

            # Qwen 文本格式工具调用解析（对标 llm-client.js _parseQwenToolCalls）
            if not tool_calls:
                parsed = _parse_qwen_tool_calls(clean_content)
                if parsed:
                    tool_calls = parsed
                    console.info(f"🔧 解析到 {len(parsed)} 个 Qwen 文本格式工具调用")
                    # 从 content 中移除工具调用文本，只保留文本回复（对标 _removeToolCallsFromContent）
                    clean_content = _remove_tool_calls_from_content(clean_content)

            if tool_calls:
                # ===== 执行工具并进入下一轮（对标 llm-handler.js） =====
                iteration += 1
                tool_call_total += len(tool_calls)
                console.accent(f"===== 🔧 第 {iteration} 轮工具调用 =====")
                console.accent(_format_tool_calls(tool_calls))

                # 本轮含音效播放工具：音效播放后不再语音/文字回复（直播场景
                # 只要音效效果，多余的总结会打断听感）。标记后后续轮次的
                # 文本只进历史上下文、不 yield 播出。
                if any(
                    (tc.get("function") or {}).get("name") == "play_sound_effect"
                    for tc in tool_calls
                ):
                    sound_effect_used = True

                # 1) assistant 消息（含 tool_calls；content 为 null 时
                #    由 _clean_messages_for_api 兜底转为 ''，兼容严格模式 API）
                messages.append({
                    "role": "assistant",
                    "content": clean_content.strip() or None,
                    "tool_calls": tool_calls,
                })

                # 2) 执行工具 → tool 响应消息（工具链完整进入下一轮上下文）
                tool_messages = await _execute_tool_calls(self.mcp, tool_calls)
                messages.extend(tool_messages)
                continue

            # ===== 无工具调用 =====
            if not clean_content.strip():
                if sound_effect_used:
                    # 音效播放后无需文字回复：空响应直接结束，不再催促
                    break
                # 空响应处理（对标 llm-handler.js consecutiveEmptyResponses：
                # 第 1 次催模型回复，第 2 次仍空则放弃）
                empty_count += 1
                if empty_count == 1:
                    console.warn("⚠️ 空响应，添加提示消息催促模型回复")
                    messages.append({
                        "role": "user",
                        "content": "请根据工具执行结果，回复用户。",
                    })
                    iteration += 1
                    continue
                console.error(f"❌ 连续 {empty_count} 次空响应，放弃等待")
                final_reply = "抱歉，我好像卡住了，请重新问我吧~"
                yield final_reply
                break

            # 正常产出文本，结束（本轮已流式 yield 的句子即最终回复）
            final_reply = "".join(round_content)
            break

        # ===== 达到最大工具轮数：非流式强制获取最终回复（对标 llm-handler.js L684-696）=====
        if final_reply is None and iteration >= max_tool_iterations:
            console.warn(f"⚠️ 已达到最大工具调用次数限制（{max_tool_iterations} 轮），"
                         "非流式获取最终回复")
            final_reply = await self._request_final_reply(messages)
            for seg in _split_sentences(final_reply):
                cleaned = _clean_sentence(seg)
                if cleaned.strip() and not sound_effect_used:
                    yield cleaned

        # 思考过程换行
        if self.cfg.LLM_THINKING:
            print()

        meta = f"{sentence_count} 句"
        if tool_call_total:
            meta += f"，{tool_call_total} 次工具调用"
        tracker.end("总生成", meta)

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

        # 打印 LLM 性能报告
        tracker.print_report()
