"""GEPA 系统提示词进化：分析对话轨迹失败点，进化出更优的行为策略段。

对标 hermes-agent 的 GEPA（Genetic-Pareto Prompt Evolution）：不修改人设
SYSTEM_PROMPT 本体，而是进化一层独立的「行为策略段」——复盘对话轨迹中
暴露的失败点，变异出候选策略，与当前策略同批评审择优，落盘供 llm_brain
注入系统提示。人设不变、策略渐进优化，越用越贴合观众。

实现流程（变异 → 评估 → 择优，对标 GEPA 的 generate-fitness-select）：
1. 变异：LLM 基于最近对话 + 当前策略，输出失败点清单与候选策略段
2. 评估：候选与当前策略交由评审 LLM 对同一批对话样本分别打分（0~10）
3. 择优：候选分更高才落盘；无当前策略时直接采用候选；平局/更低保留现状
4. 注入：llm_brain 每轮读取 evolution_policy.json 注入系统提示

设计约束：
- 独立节流（EVOLUTION_PROMPT_EVO_INTERVAL，默认 6 小时一次），不与复盘抢节奏
- 全程 fail-open：任何一步失败都保留当前策略，绝不阻塞运行
- 复用主 LLM 服务（LLM_* 配置），与 evolution.py 一致，不引入新依赖
- 候选策略上限 _POLICY_MAX_CHARS 字符，防止策略膨胀失控
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from openai import AsyncOpenAI

from ev.llm.client.factory import get_async_openai_client
from ev.llm.utils.jsonutil import parse_json_object
from ev.utils import config, console

# 生效策略段文件：llm_brain 每轮读取注入系统提示（GEPA 择优后的当前版本）
_POLICY_PATH = os.path.join(config.cfg.DATA_ROOT, "evolution_policy.json")

# 策略版本历史存档（追加写，供用户审阅每次进化的成败）
_POLICY_HISTORY_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution_policy_history.md")

# 候选策略段最大字符数：超限截断，防止策略膨胀失控稀释系统提示
_POLICY_MAX_CHARS = 400

# 评审评估存档（evolution_evals.jsonl，与技能评估共用同文件追加写）：
# 记录每次 GEPA 评审的 {candidate, 双评分, context}，A/B 可回溯（5.16）
_EVALS_PATH = os.path.join(config.cfg.DATA_ROOT, "evolution_evals.jsonl")

# 变异提示：把最近对话 + 当前策略交给 LLM，输出失败点与候选策略段
_EVOLVE_SYSTEM = (
    "你是虚拟主播的行为策略进化师。用户消息包含「最近对话记录」与「当前生效的"
    "行为策略段」。请找出对话中暴露出的失败点（观众不满、冷场、答非所问、人设"
    "崩塌、话术重复等），并变异出一版改进后的行为策略段。\n"
    "输出 JSON 对象：\n"
    "  issues：失败点清单（0-3 条，每条一句话，≤40 字）\n"
    "  candidate：改进后的行为策略段（Markdown 列表或短段落，≤{max} 字，"
    "给主播的可执行行为准则；若当前策略已足够好无可改进则填 null）\n"
    "只输出 JSON，不要任何多余文字或代码块标记。"
)

# 评审提示：候选与当前策略对同一批对话样本分别打分（GEPA 择优依据）
_JUDGE_SYSTEM = (
    "你是虚拟主播行为策略的评审。用户消息包含「最近对话记录」「当前策略」与"
    "「候选策略」两份行为准则。请分别判断：在刚才的对话情境下，遵循当前策略与"
    "遵循候选策略，主播的回应质量会更好还是更差。\n"
    "输出 JSON 对象：\n"
    "  score_current：当前策略的预估质量分（0~10 整数）\n"
    "  score_candidate：候选策略的预估质量分（0~10 整数）\n"
    "  reason：一句话理由（≤50 字）\n"
    "只输出 JSON，不要任何多余文字或代码块标记。"
)


def _format_turns(turns: list[dict]) -> str:
    """把对话轮次格式化为进化素材文本（与 evolution.py 同一格式，区分三方角色）。"""
    from tools.memory import memory
    return memory.format_turns_text(turns)


def _with_feedback(prompt: str) -> str:
    """把观众负反馈素材追加进 GEPA 评审/变异输入（5.6.4）。

    候选策略只在「含负反馈的轮次」同批评审时获得真实失败点参照，使评分
    更看重对负反馈的改善；无负反馈或开关关闭时原样返回（行为不变）。
    """
    try:
        from .feedback import feedback_section
        fb = feedback_section(max_events=5)
    except Exception:
        return prompt
    if not fb:
        return prompt
    return (prompt + fb
            + "\n（本轮含观众负反馈：候选策略应优先针对这些失败点改进，"
              "评审计分应更看重对负反馈的改善）")


def _load_policy() -> str:
    """读取当前生效策略段（文件缺失/损坏时返回空字符串）。"""
    try:
        with open(_POLICY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("text") or "").strip() if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def _save_policy(text: str, version: int, previous: str = "") -> None:
    """覆写生效策略段文件（含版本号与上一版策略，供用户审阅与 A/B 盲测）。"""
    try:
        os.makedirs(os.path.dirname(_POLICY_PATH), exist_ok=True)
        with open(_POLICY_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "text": text,
                "version": version,
                "previous": previous,
                "updated": time.time(),
            }, f, ensure_ascii=False, indent=2)
    except OSError as e:
        console.warn(f"[GEPA] 写入策略段失败：{e}")


def _append_history(version: int, text: str) -> None:
    """把新落盘的策略版本追加到历史存档（失败静默）。"""
    try:
        os.makedirs(os.path.dirname(_POLICY_HISTORY_PATH), exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(_POLICY_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n## v{version}（{stamp}）\n{text}\n")
    except OSError:
        pass


def _append_eval(record: dict) -> None:
    """把一次 GEPA 评审结果追加到评估存档（5.16，失败静默）。"""
    try:
        os.makedirs(os.path.dirname(_EVALS_PATH), exist_ok=True)
        with open(_EVALS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _record_call(task: str, resp, latency: float) -> None:
    """记账一次 GEPA LLM 调用（5.16 旁路，失败不影响结果）。"""
    try:
        usage = getattr(resp, "usage", None)
        from .usage import record_usage
        record_usage(
            task, resp.model if hasattr(resp, "model") else "",
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
            latency,
        )
    except Exception:
        pass


class PolicyEvolver:
    """GEPA 系统提示词进化器：节流判定 → 变异 → 评审择优 → 落盘。"""

    def __init__(self) -> None:
        self._last_evolve = 0.0    # 上次进化时间戳（节流用）
        self._evolve_lock = asyncio.Lock()  # 串行化进化调用
        self._client: AsyncOpenAI | None = None
        self._model = ""

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 LLM_* 配置构建 OpenAI 兼容客户端（复用主 LLM 服务）。"""
        if self._client is None:
            base_url = (config.cfg.LLM_BASE_URL or "").strip()
            api_key = (config.cfg.LLM_API_KEY or "").strip()
            self._model = (config.cfg.LLM_MODEL or "").strip()
            if not (base_url and api_key and self._model):
                return None
            self._client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=60.0)
        return self._client

    async def maybe_evolve(self, turns: list[dict]) -> None:
        """定期触发一次策略进化：节流判定，达标才调用 LLM（fail-open）。"""
        if not getattr(config.cfg, "EVOLUTION_PROMPT_EVO_ENABLED", True):
            return
        if not turns:
            return
        interval = max(600, int(
            getattr(config.cfg, "EVOLUTION_PROMPT_EVO_INTERVAL", 0) or 21600))
        async with self._evolve_lock:
            now = time.time()
            if now - self._last_evolve < interval:
                return
            self._last_evolve = now
            try:
                await self._do_evolve(turns[-12:])
            except Exception as e:
                console.warn(f"[GEPA] 策略进化失败（不影响运行）：{e}")

    # ---------- 进化执行（变异 → 评审择优） ----------

    async def _do_evolve(self, recent: list[dict]) -> None:
        """调用 LLM 变异候选策略，与当前策略同批评审择优后落盘。"""
        text = _format_turns(recent)
        if not text:
            return
        current = _load_policy()
        client = self._ensure_client()
        if client is None:
            return
        console.dim("[GEPA] 开始分析对话失败点，变异候选策略...")
        # 1) 变异：输出失败点与候选策略段（5.6.4 含负反馈素材时优先针对失败点）
        evolve_user = _with_feedback(
            f"[最近对话记录]\n{text}\n\n"
            f"[当前生效的行为策略段]\n{current or '（无，首次进化）'}")
        start = time.time()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _EVOLVE_SYSTEM.replace(
                            "{max}", str(_POLICY_MAX_CHARS))},
                        {"role": "user", "content": evolve_user},
                    ],
                    temperature=0.7,
                    max_tokens=1024,
                ),
                timeout=60.0,
            )
        except Exception as e:
            console.warn(f"[GEPA] 变异调用失败：{e}")
            return
        # 5.16 记账（旁路）：记录变异调用的 token 与耗时
        _record_call("gepa.evolve", resp, time.time() - start)
        content = (resp.choices[0].message.content or "").strip()
        data = parse_json_object(content)
        candidate = (data.get("candidate") or "").strip()
        if not candidate:
            console.dim("[GEPA] 当前策略已足够好，无改进，跳过本次")
            return
        if len(candidate) > _POLICY_MAX_CHARS:
            candidate = candidate[:_POLICY_MAX_CHARS]
        # 2) 评审：候选与当前策略对同一批对话样本分别打分
        if not current:
            # 无当前策略 → 直接采用候选（首次进化，无对照）
            self._commit(candidate, current)
            return
        judge_user = _with_feedback(
            f"[最近对话记录]\n{text}\n\n"
            f"[当前策略]\n{current}\n\n[候选策略]\n{candidate}")
        start = time.time()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _JUDGE_SYSTEM},
                        {"role": "user", "content": judge_user},
                    ],
                    temperature=0.2,
                    max_tokens=512,
                ),
                timeout=60.0,
            )
        except Exception as e:
            console.warn(f"[GEPA] 评审调用失败：{e}")
            return
        # 5.16 记账（旁路）：记录评审调用的 token 与耗时
        _record_call("gepa.judge", resp, time.time() - start)
        judge = parse_json_object(resp.choices[0].message.content or "")
        try:
            score_current = float(judge.get("score_current") or 0)
            score_candidate = float(judge.get("score_candidate") or 0)
        except (TypeError, ValueError):
            score_current = score_candidate = 0.0
        # 5.16 A/B 可回溯：评审上下文（负反馈素材）+ 双评分 + 候选落档
        try:
            from .feedback import feedback_section
            context = feedback_section(max_events=5) or ""
        except Exception:
            context = ""
        _append_eval({
            "ts": time.time(),
            "candidate": candidate,
            "score_current": round(score_current, 3),
            "score_candidate": round(score_candidate, 3),
            "reason": judge.get("reason") or "",
            "context": context,
        })
        if score_candidate > score_current:
            self._commit(candidate, current)
            console.ok(
                f"[GEPA] 策略进化：{score_current:.0f}→{score_candidate:.0f}，"
                f"已采用新策略（{judge.get('reason') or ''}）")
        else:
            console.dim(
                f"[GEPA] 候选未胜出（{score_current:.0f} vs {score_candidate:.0f}），"
                "保留当前策略")

    def _commit(self, candidate: str, current: str) -> None:
        """择优落盘：从历史版本号 +1 写入新策略，并追加存档。

        previous 记录被替换的旧策略文本（5.16），供 inject 侧按
        EVOLUTION_POLICY_AB 开关做 50% 盲测轮换（线上 A/B）。
        """
        version = 1
        try:
            with open(_POLICY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            version = int(data.get("version") or 0) + 1
        except (OSError, ValueError):
            pass
        _save_policy(candidate, version, previous=current)
        _append_history(version, candidate)
        if current:
            console.dim(f"[GEPA] 旧策略已存档于 evolution_policy_history.md")


_evolver: PolicyEvolver | None = None


def get_evolver() -> PolicyEvolver:
    """获取进化器单例（延迟构建）。"""
    global _evolver
    if _evolver is None:
        _evolver = PolicyEvolver()
    return _evolver


__all__ = ["PolicyEvolver", "get_evolver"]
