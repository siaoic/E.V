import asyncio
from typing import List, Optional

from ev.utils import config
from ev.adapter.llm import BaseLLMAdapter
from plugins.builtin.tools.skills import get_skill_manager
from ev.llm.client.factory import get_openai_client
from ev.llm.utils.constants import _MAX_TOOL_ITERATIONS
from ev.llm.history.inject import _InjectionMixin
from ev.llm.history.summary import _SummaryMixin

from .chat.mixin import _ChatMixin
from .curator import _CuratorMixin


class LLMBrain(_ChatMixin, _CuratorMixin, BaseLLMAdapter, _InjectionMixin, _SummaryMixin):
    """LLM 流式大脑：支持多轮工具调用，按句 yield 纯对话文本。"""

    def __init__(self, mcp=None) -> None:
        self.cfg = config.cfg
        # LLMContract: 可读 name；父类 BaseLLMAdapter 已定义 name="llm"，
        # 这里按模型名覆盖以便多实现切换时区分；失败则保持父类默认。
        try:
            _model = getattr(self.cfg, "LLM_MODEL", None) or ""
            self.name = f"llm-brain-{_model}" if _model else "llm-brain-default"
        except Exception:
            pass
        # LLMContract 兜底字段：若子类没有 _turn_contexts 机制，push_turn_context
        # 会落到这里；chat_stream 开头会联合消费所有候选列表。
        if not hasattr(self, "_turn_contexts"):
            self._turn_contexts: List[str] = []
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
        # L3 会话历史落盘任务（后台写 SQLite，失败不影响主链路）
        self._session_persist_task: Optional[asyncio.Task] = None
        # L4 治理（会后秘书）：低频后台复盘写 L2 MEMORY.md；轮次计数器防并发
        self._curator_task: Optional[asyncio.Task] = None
        self._curator_turn_count: int = 0
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
        self._policy_previous: str = ""  # 5.16 A/B：上一版策略文本（盲测轮换用）
        # 模型路由进化（多臂老虎机）：配置多 LLM 服务时按历史表现选服务；
        # 未配置/未启用时 router 为 None，完全走原有单一 LLM 服务逻辑
        from ev.llm.utils.model_router import get_router
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
        AGENT_TOOLSET 非空时按工具集门控过滤（3.3）；空 = 全量（旧行为）。
        """
        from plugins.builtin.tools import get_merged_tools
        return get_merged_tools(self.mcp, toolset=self.cfg.AGENT_TOOLSET)

    @staticmethod
    def _describe_mcp_servers(mcp) -> str:
        """取 MCP 服务器能力说明（供 system prompt 注入，见 _chat_stream_inner）。"""
        from ev.mcp.llm_bridge import describe_mcp_servers
        return describe_mcp_servers(mcp)

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
