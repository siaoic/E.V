"""自我进化引擎（主类）：节流判定 → 一次 LLM 复盘 → 分派各子模块落地。

职责拆分后的调度层，只保留：
- 节流 / 并发控制（_review_lock 串行化对话后复盘与定期复盘）
- 客户端管理（管家模型首选 + 主模型兜底，测试可注入工厂）
- 复盘执行与成果分派（技能 / 话题 / 经验 / 话术 / 画像 → 子模块）
- 技能库审阅节流（每天至多一次）与 GEPA 提示词进化触发

子模块实现见同包 skills / topics / advice / profile。
"""

from __future__ import annotations

import asyncio
import time

from openai import AsyncOpenAI

from src.llm.client.factory import get_async_openai_client
from src.utils import config, console

from ._utils import call_llm_json
from .advice import AdviceEvolution, _pending_advice_text
from .metrics import EVENT_REVIEW_FAILED, EVENT_REVIEW_TRIGGERED, METRICS
from .profile import ProfileEvolution
from .prompts import (
    CFG, _HERMES_COMBINED_REVIEW_PROMPT, _REVIEW_OUTPUT_PROTOCOL,
)
from .skills import SkillEvolution
from .topics import TopicEvolution


def _format_turns(turns: list[dict]) -> str:
    """把对话轮次格式化为复盘素材文本（区分弹幕/主播/AI 三方角色）。"""
    from tools.memory import memory
    return memory.format_turns_text(turns)


class EvolutionEngine:
    """自我进化引擎：节流判定 → 一次 LLM 复盘 → 各子模块落地。"""

    def __init__(
        self,
        *,
        butler_client_factory=None,
        main_client_factory=None,
        prompt_evo_getter=None,
    ) -> None:
        self._last_review = 0.0      # 上次复盘时间戳（节流用）
        self._last_prune = 0.0       # 上次技能库审阅时间戳（节流用）
        self._turns_since_review = 0  # 上次复盘后新增的对话轮次
        self._review_lock = asyncio.Lock()  # 串行化对话后复盘与定期复盘
        self._client: AsyncOpenAI | None = None
        self._model = ""
        # 主模型客户端（复盘故障兜底 / 技能审阅）
        self._main_client: AsyncOpenAI | None = None
        self._main_model = ""
        # 客户端 / GEPA 工厂（测试注入用；None = 走全局配置懒构建）
        self._butler_factory = butler_client_factory
        self._main_factory = main_client_factory
        self._get_prompt_evo = prompt_evo_getter or self._default_prompt_evo_getter
        # 子模块（职责拆分，无状态）
        self.skills = SkillEvolution()
        self.topics = TopicEvolution()
        self.advice = AdviceEvolution()
        self.profile = ProfileEvolution()

    @staticmethod
    def _default_prompt_evo_getter():
        """默认 GEPA 进化器获取（懒 import，调用时才加载 prompt_evo 模块）。"""
        from src.llm.prompt_evo import get_evolver
        return get_evolver()

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 BUTLER_*（回退 LLM_*）构建管家客户端（复盘首选，智谱）。

        复盘交给管家模型（智谱 glm-4v-flash）执行，不占用主对话 DeepSeek
        并发、不与对话互相限流；且复盘由调用方放入后台任务，不阻塞对话。
        """
        if self._client is None:
            if self._butler_factory is not None:
                self._client, self._model = self._butler_factory()
            else:
                base_url = (config.cfg.BUTLER_BASE_URL
                            or config.cfg.LLM_BASE_URL or "").strip()
                api_key = (config.cfg.BUTLER_API_KEY
                           or config.cfg.LLM_API_KEY or "").strip()
                self._model = (config.cfg.BUTLER_MODEL
                               or config.cfg.LLM_MODEL or "").strip()
                if not (base_url and api_key and self._model):
                    return None
                self._client = self._build_client(base_url, api_key)
            if self._client is None:
                return None
        return self._client

    def _ensure_main_client(self) -> AsyncOpenAI | None:
        """按 LLM_* 构建主模型客户端（DeepSeek）：复盘故障兜底 + 技能审阅。"""
        if self._main_client is None:
            if self._main_factory is not None:
                self._main_client, self._main_model = self._main_factory()
            else:
                base_url = (config.cfg.LLM_BASE_URL or "").strip()
                api_key = (config.cfg.LLM_API_KEY or "").strip()
                self._main_model = (config.cfg.LLM_MODEL or "").strip()
                if not (base_url and api_key and self._main_model):
                    return None
                self._main_client = self._build_client(base_url, api_key)
            if self._main_client is None:
                return None
        return self._main_client

    @staticmethod
    def _build_client(base_url: str, api_key: str) -> AsyncOpenAI | None:
        """构建 OpenAI 兼容客户端；缺配置返回 None。"""
        if not (base_url and api_key):
            return None
        return get_async_openai_client(
            api_key=api_key, base_url=base_url, timeout=60.0)

    async def maybe_review(self, turns: list[dict],
                           proactive=None) -> None:
        """对话结束后调用：计数 + 节流/轮次阈值判定，达标才复盘。"""
        cfg = config.cfg
        if not cfg.EVOLUTION_ENABLED:
            return
        self._turns_since_review += 1
        if not turns:
            return
        async with self._review_lock:
            # 拿锁后二次判定（与定期提示并发时防止重复触发）
            now = time.time()
            if now - self._last_review < cfg.EVOLUTION_MIN_INTERVAL:
                return
            if self._turns_since_review < cfg.EVOLUTION_MIN_TURNS:
                return
            self._turns_since_review = 0
            self._last_review = now
            try:
                await self._do_review(turns[-12:], proactive)
            except Exception as e:
                console.warn(f"[进化] 复盘失败（不影响运行）：{e}")
                METRICS.incr(EVENT_REVIEW_FAILED)
            await self._maybe_run_prune()

    async def periodic_tick(self, turns: list[dict],
                            proactive=None) -> None:
        """定期自我提示：空闲期主动补一次复盘（对标 hermes 定期自我评估）。

        与 maybe_review 共享节流状态：仅当「距上次复盘已达标且上次复盘后有
        新增对话轮次（当时未凑够轮数或已凑够但时间未到）」时才补刀，
        避免用旧对话重复消费 token。
        """
        cfg = config.cfg
        if not cfg.EVOLUTION_ENABLED:
            return
        if not turns:
            return
        async with self._review_lock:
            now = time.time()
            if now - self._last_review < cfg.EVOLUTION_MIN_INTERVAL:
                return
            if self._turns_since_review < 1:
                return  # 上次复盘后没有新对话，不重复复盘
            self._turns_since_review = 0
            self._last_review = now
            try:
                await self._do_review(turns[-12:], proactive)
            except Exception as e:
                console.warn(f"[进化] 定期复盘失败（不影响运行）：{e}")
                METRICS.incr(EVENT_REVIEW_FAILED)
            await self._maybe_run_prune()
            await self._maybe_run_prompt_evo(turns[-12:])

    async def _maybe_run_prompt_evo(self, turns: list[dict]) -> None:
        """GEPA 系统提示词进化：定期复盘后顺带触发（独立节流，fail-open）。

        分析对话失败点变异候选行为策略段，与当前策略同批评审择优，落盘后由
        llm_brain 注入系统提示（对标 hermes 的 GEPA）。任何失败不影响运行。
        """
        if not turns:
            return
        try:
            await self._get_prompt_evo().maybe_evolve(turns)
        except Exception as e:
            console.dim(f"[进化] GEPA 提示词进化跳过：{e}")

    async def _maybe_run_prune(self) -> None:
        """节流执行技能库审阅（每天至多一次，失败不影响运行）。"""
        now = time.time()
        if now - self._last_prune < CFG.prune_interval_seconds:
            return
        self._last_prune = now
        try:
            await self.skills.maybe_prune(
                client=self._ensure_main_client(), model=self._main_model)
        except Exception as e:
            console.warn(f"[进化] 技能库审阅失败（不影响运行）：{e}")

    # ---------- 复盘执行 ----------

    async def _do_review(self, recent: list[dict], proactive) -> None:
        """调用 LLM 复盘最近对话，解析后落地各子模块成果。"""
        METRICS.incr(EVENT_REVIEW_TRIGGERED)
        text = _format_turns(recent)
        if not text:
            return
        # 附上已到期的话术建议，让 LLM 一并回评续期/移除
        pending = _pending_advice_text()
        if pending:
            text += ("\n\n### 待评估的话术建议（逐条判断 keep 决定续期或移除）\n"
                     + "\n".join(f"- {t}" for t in pending))
        # hermes 复盘原文入 user 消息，输出协议入 system 消息（保证 JSON 解析兼容）
        user_content = (_HERMES_COMBINED_REVIEW_PROMPT
                        + "\n\n[CONVERSATION TO REVIEW]\n" + text)
        # 追加技能使用统计：让 LLM 修补/清理决策有真实使用数据支撑
        # （对标 hermes 的 skill usage counters，仅作参考不作硬性依据）
        try:
            from plugins.tools.skills import get_skill_manager
            usage = get_skill_manager().usage_section()
            if usage:
                user_content += "\n\n[SKILL USAGE STATS]\n" + usage
        except Exception:
            pass
        console.dim("[进化] 开始复盘最近对话...")
        # 候选模型链：管家模型（智谱，首选）→ 主模型（DeepSeek，故障兜底）。
        # 任一模型调用失败自动换下一个，全挂才跳过本次（不阻断主流程）。
        candidates = []
        butler = self._ensure_client()
        if butler:
            candidates.append((butler, self._model, "管家模型"))
        main = self._ensure_main_client()
        if main and self._main_model and self._main_model != self._model:
            candidates.append((main, self._main_model, "主模型"))
        data = await call_llm_json(
            candidates,
            [
                {"role": "system", "content": _REVIEW_OUTPUT_PROTOCOL},
                {"role": "user", "content": user_content},
            ],
            label="复盘",
        )
        if data is None:
            return
        await self._apply_review(data, proactive)

    async def _apply_review(self, data: dict, proactive) -> None:
        """把复盘结果分七类落地（技能 / 修补 / 话题 / 经验 / 话术 / 回评 / 画像）。"""
        skill = data.get("skill")
        if isinstance(skill, dict):
            await self.skills.save_skill(skill)
        patch = data.get("skill_patch")
        if isinstance(patch, dict):
            await self.skills.apply_patch(patch)
        topics = data.get("topics")
        if isinstance(topics, list) and topics:
            self.topics.append_topics(topics, proactive)
        lesson = (data.get("lesson") or "").strip()
        if lesson:
            await self._save_lesson(lesson)
        advice = (data.get("advice") or "").strip()
        if advice:
            self.advice.append_advice(advice)
        status = data.get("advice_status")
        if isinstance(status, list) and status:
            self.advice.apply_advice_status(status)
        profile = data.get("profile")
        if isinstance(profile, list) and profile:
            self.profile.append_profile(profile)
        if not (skill or patch or topics or lesson or advice or status or profile):
            console.dim("[进化] 本轮无沉淀内容")

    # ---------- 行为反思（经验教训写记忆库） ----------

    async def _save_lesson(self, lesson: str) -> None:
        """把复盘出的经验教训写入记忆库（后续检索可带出）。"""
        try:
            from tools.memory import memory
            mm = memory.get_manager()
            await mm.commit_recall_files([{
                "name": "进化经验",
                "description": "进化/经验教训",
                "content": lesson,
                "user": mm.self_user_id,
            }])
        except Exception as e:
            console.warn(f"[进化] 经验教训写入失败：{e}")
            return
        console.ok("[进化] 行为反思：经验教训已入记忆库")
