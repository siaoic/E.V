"""技能进化评估闭环：生成测试集 → 执行打分 → 择优保留。

对标 hermes-agent 的 GEPA 变异-评估-择优：技能文本不再「生成即落盘」，
而是先用 LLM 依据技能内容合成测试用例（task + expected_keywords），
把技能作为 system 指令执行任务，用关键词命中率计算 fitness
（score = 0.3 + 0.7 × 命中关键词数 / 期望关键词数，与 hermes 一致），
修补场景新旧版本同测对比，只保留更优版本。

设计约束（保证不破坏原有技能沉淀/修补行为）：
- 全程 fail-open：任何一步失败（模型调用异常 / JSON 解析失败）都返回
  None 由调用方按原逻辑落盘，仅告警，绝不阻塞技能写入
- 用例数量受 EVOLUTION_EVAL_CASES 控制（默认 2，1~3），控制成本
- 评估只在复盘触发时随技能沉淀/修补执行，不新增独立调用入口
- 复用主 LLM 服务（LLM_* 配置），与 evolution.py 一致
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from openai import AsyncOpenAI

from src.llm.client.factory import get_async_openai_client
from src.llm.utils.jsonutil import parse_json_array
from src.utils import config, console

# 评估结果存档（追加写，供用户审阅每次评估的对比数据）
_EVAL_LOG_PATH = os.path.join(config.cfg.DATA_ROOT, "evolution_evals.jsonl")

# 生成测试用例的系统提示：要求输出 task + expected_keywords 的 JSON 数组
_GENERATE_CASES_SYSTEM = (
    "你是技能质量评估员。根据给定的技能指令（SKILL.md 正文），生成 {count} 个"
    "能检验该技能效果的任务测试用例。\n"
    "每个用例包含：\n"
    "  task：一句话任务描述（中文，直播/对话场景优先，10~60 字）\n"
    "  expected_keywords：3~6 个期望执行结果中应出现的关键词（中文短语，"
    "用于自动比对执行效果，选技能核心能力对应的词，避免过于宽泛）\n"
    "输出 JSON 数组，每项 {\"task\": \"...\", \"expected_keywords\": [\"...\"]}。\n"
    "只输出 JSON 数组，不要任何额外文字。"
)

# 执行技能任务的系统提示：把技能作为指令，完成单个测试任务
_EXECUTE_SYSTEM = (
    "以下是你要遵循的技能指令。请按它完成用户给出的任务，"
    "输出简洁可执行的结果（不要复述技能内容）。"
)


def _message_text(message) -> str:
    """取模型回复正文：部分推理模型 content 为空、正文在 reasoning_content，
    两者都读，避免评估因空 content 而误判（对标 agent 的兜底）。"""
    content = (getattr(message, "content", None) or "").strip()
    if not content:
        content = (getattr(message, "reasoning_content", None) or "").strip()
    return content


def _append_eval_log(record: dict) -> None:
    """把一次技能评估的对比结果追加到存档（失败静默）。"""
    try:
        os.makedirs(os.path.dirname(_EVAL_LOG_PATH), exist_ok=True)
        with open(_EVAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        console.warn(f"[技能评估] 写入存档失败：{e}")


class SkillEvaluator:
    """技能评估器：生成测试集 + fitness 打分（评估失败一律返回 None）。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model = ""

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 LLM_* 配置构建客户端（复用主 LLM 服务）。"""
        if self._client is None:
            base_url = (config.cfg.LLM_BASE_URL or "").strip()
            api_key = (config.cfg.LLM_API_KEY or "").strip()
            self._model = (config.cfg.LLM_MODEL or "").strip()
            if not (base_url and api_key and self._model):
                return None
            self._client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=60.0)
        return self._client

    async def _complete(self, system: str, user_text: str, max_tokens: int,
                        task: str = ""):
        """调用主模型（关闭思考，保证输出干净文本/JSON）。

        task: 5.16 记账任务名（如 "skill_eval.patch"）；为空不记账。
        """
        client = self._ensure_client()
        if client is None:
            return None
        start = time.time()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_text},
                    ],
                    temperature=0.2,
                    max_tokens=max_tokens,
                    extra_body={"thinking": {"type": "disabled"}},
                ),
                timeout=60.0,
            )
        except Exception as e:
            console.warn(f"[技能评估] 模型调用失败：{e}")
            return None
        if task:
            try:
                usage = getattr(resp, "usage", None)
                from .usage import record_usage
                record_usage(
                    task, self._model,
                    getattr(usage, "prompt_tokens", 0),
                    getattr(usage, "completion_tokens", 0),
                    time.time() - start,
                )
            except Exception:
                pass
        return resp

    async def generate_eval_cases(self, skill_text: str) -> list[dict] | None:
        """依据技能内容生成测试用例（task + expected_keywords）。

        用例数量取 EVOLUTION_EVAL_CASES（默认 2，钳制在 1~3）；
        解析失败返回 None（调用方按原逻辑落盘）。
        """
        if not getattr(config.cfg, "EVOLUTION_EVAL_ENABLED", True):
            return None
        count = max(1, min(int(getattr(config.cfg, "EVOLUTION_EVAL_CASES", 2)), 3))
        system = _GENERATE_CASES_SYSTEM.replace("{count}", str(count))
        # 技能正文可能带 frontmatter，去掉后只把指令正文交给生成器
        body = skill_text
        if body.startswith("---"):
            sep = body.find("\n---", 3)
            if sep > 0:
                body = body[sep + 4 :].lstrip("\n")
        # 5.6.3 技能效果回路：注入最近真实负反馈作为用例素材（替代纯 LLM 合成），
        # 使 fitness 反映真实场景；无反馈或关闭时维持原行为（纯技能正文）
        user_text = body
        try:
            from .feedback import feedback_section
            fb = feedback_section(max_events=5)
            if fb:
                user_text = (body
                             + "\n\n请优先参考以下真实负反馈设计能检验该技能的"
                               "用例（如针对被吐槽的场景）：" + fb)
        except Exception:
            pass
        resp = await self._complete(system, user_text, max_tokens=1024,
                                    task="skill_eval.gen_cases")
        if resp is None:
            return None
        content = _message_text(resp.choices[0].message)
        data = parse_json_array(content)
        if not data:
            return None
        cases: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            task = (item.get("task") or "").strip()
            keywords = [
                str(k).strip()
                for k in (item.get("expected_keywords") or [])
                if str(k).strip()
            ]
            if task and keywords:
                cases.append({"task": task, "expected_keywords": keywords})
        return cases[:count] or None

    async def _run_case(self, skill_text: str, case: dict) -> str:
        """用技能作为指令执行单个测试任务，返回执行结果文本。"""
        resp = await self._complete(
            _EXECUTE_SYSTEM, case["task"], max_tokens=512,
            task="skill_eval.run_case")
        if resp is None:
            return ""
        return _message_text(resp.choices[0].message).strip()

    @staticmethod
    def _score_output(output: str, keywords: list[str]) -> float:
        """关键词命中率 fitness：0.3 + 0.7 × 命中数/期望数（对标 hermes）。

        期望关键词为空时返回 0.3 保底（无法判断质量，不拉低均值）。
        """
        if not keywords:
            return 0.3
        hits = sum(1 for k in keywords if k in output)
        return 0.3 + 0.7 * hits / len(keywords)

    async def score_skill(self, skill_text: str,
                          cases: list[dict] | None = None) -> float | None:
        """对技能完整打分：执行全部测试用例后取平均 fitness。

        任一用例执行失败（模型调用异常）返回 None；单个用例输出为空
        只记 0.3 保底不中断，避免一次抖动拉垮整次评估。
        """
        if not cases:
            cases = await self.generate_eval_cases(skill_text)
        if not cases:
            return None
        scores: list[float] = []
        for case in cases:
            output = await self._run_case(skill_text, case)
            if not output:
                return None
            scores.append(self._score_output(output, case["expected_keywords"]))
        return sum(scores) / len(scores)

    async def evaluate_patch(self, name: str, old_text: str,
                             new_text: str) -> tuple[float, float] | None:
        """对比新旧技能正文：同一测试集分别执行打分，返回 (new, old)。

        评估/执行任一环节失败返回 None（调用方按原逻辑落盘，不拦截）；
        成功则把对比结果追加存档供用户审阅。
        """
        if not getattr(config.cfg, "EVOLUTION_EVAL_ENABLED", True):
            return None
        cases = await self.generate_eval_cases(new_text)
        if not cases:
            return None
        new_score = await self.score_skill(new_text, cases)
        if new_score is None:
            return None
        old_score = await self.score_skill(old_text, cases)
        if old_score is None:
            return None
        _append_eval_log({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "skill": name,
            "cases": cases,
            "old_score": round(old_score, 3),
            "new_score": round(new_score, 3),
            "kept": new_score >= old_score,
        })
        return new_score, old_score


_evaluator: SkillEvaluator | None = None


def get_evaluator() -> SkillEvaluator:
    """获取评估器单例（延迟构建）。"""
    global _evaluator
    if _evaluator is None:
        _evaluator = SkillEvaluator()
    return _evaluator


__all__ = ["SkillEvaluator", "get_evaluator"]
