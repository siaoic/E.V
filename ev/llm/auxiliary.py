"""辅助 LLM 统一入口与记账（3.16，对标 Hermes agent/auxiliary_client.py）。

E.V 多路辅助 LLM 调用（evolution 复盘 / proactive 开口决策 / butler 记忆
提取 / skill_eval 技能评估 / GEPA 策略进化）各自直连 factory，无统一入口
与记账。本模块提供：

- call_llm(task, system, messages, **kw)：按任务名路由 model（AUX_MODELS
  [task] 覆盖，缺省走主模型），返回 (text, usage)；AUX_ACCOUNTING 开启时
  自动记账到 DATA_ROOT/aux_usage.jsonl。
- record_aux_usage(...)：记账函数（供已有直连调用点旁路记账用）。
- get_aux_usage_summary(limit)：聚合最近记账，供 !perf 展示各任务 token。

迁移策略：call_llm 默认回主模型直连、等价旧行为；既有调用点按需逐步
迁移（保持各自入参与返回契约，仅替换网络层）。记账是旁路，写失败静默。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Optional

from ev.utils import config, console

# 记账文件（可写数据根）：JSONL 追加写，一条一行
_USAGE_PATH = os.path.join(config.cfg.DATA_ROOT, "aux_usage.jsonl")

# 已由主链路/evolution 记账的任务：此处再记会重复，跳过
_EXCLUDED_TASKS = frozenset({
    "review", "skill.prune", "gepa.evolve", "gepa.judge",
    "skill_eval.gen_cases", "skill_eval.run_case",
})

# 默认超时 / 采样参数（对齐 evolution call_llm_json 的默认值）
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_TEMPERATURE = 0.4
_DEFAULT_MAX_TOKENS = 2048


def aux_accounting_enabled() -> bool:
    """3.16 记账总开关：关闭时 record_aux_usage 空转（行为不变）。"""
    try:
        return bool(config.cfg.AUX_ACCOUNTING)
    except Exception:
        return False


def get_aux_model(task: str) -> str:
    """按任务路由 model：AUX_MODELS[task] 覆盖，缺省回主模型。"""
    overrides = getattr(config.cfg, "AUX_MODELS", None) or {}
    return overrides.get(task) or config.cfg.LLM_MODEL or ""


def _normalize_messages(system: str, messages: list[dict]) -> list[dict]:
    """把 (system, messages) 归一为 OpenAI 消息数组。

    system 为空时直接透传 messages（兼容无 system 的调用点，如视觉描述）。
    """
    if not system:
        return messages or []
    return [{"role": "system", "content": system}, *(messages or [])]


async def call_llm(
    task: str,
    system: str = "",
    messages: Optional[list[dict]] = None,
    *,
    model: Optional[str] = None,
    temperature: float = _DEFAULT_TEMPERATURE,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], dict]:
    """按任务路由的辅助 LLM 调用（异步，默认走主模型直连）。

    返回 (text, usage)：
        text: 回复纯文本（content 为空时兜底读 reasoning_content）；
             失败返回 None（fail-open，不抛异常）。
        usage: {"task", "model", "prompt_tokens", "completion_tokens",
                "latency_ms"}，记账与排查用。

    参数：
        task: 任务名（如 "butler.extract" / "topic.gen"），用于路由与记账。
        system: 系统提示；为空则直接透传 messages。
        messages: 用户消息数组（[{"role", "content"}, ...]）。
        model: 显式指定 model，覆盖 AUX_MODELS 路由（默认 None 走路由）。
    """
    used_model = model or get_aux_model(task)
    if not used_model:
        console.dim(f"[辅助LLM] 任务 {task} 无可用模型（LLM_MODEL 未配置）")
        return None, {"task": task, "model": "", "latency_ms": 0}

    from ev.llm.client.factory import get_async_openai_client

    start = time.time()
    client = get_async_openai_client(
        api_key=config.cfg.LLM_API_KEY,
        base_url=config.cfg.LLM_BASE_URL,
        timeout=timeout,
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=used_model,
                messages=_normalize_messages(system, messages),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=timeout,
        )
    except Exception as e:
        console.warn(f"[辅助LLM] 任务 {task} 调用失败：{e}")
        return None, {"task": task, "model": used_model, "latency_ms": 0}

    usage = getattr(resp, "usage", None)
    latency_ms = int((time.time() - start) * 1000)
    usage_info = {
        "task": task,
        "model": used_model,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "latency_ms": latency_ms,
    }
    if task not in _EXCLUDED_TASKS:
        record_aux_usage(
            task, used_model,
            usage_info["prompt_tokens"], usage_info["completion_tokens"],
            latency_ms / 1000.0,
        )

    msg = resp.choices[0].message
    content = (msg.content or "").strip()
    if not content:
        content = (getattr(msg, "reasoning_content", None) or "").strip()
    return (content or None), usage_info


def record_aux_usage(task: str, model: str, prompt_tokens: int,
                     completion_tokens: int, latency: float) -> None:
    """追加一条辅助调用记账（旁路：失败静默，不影响调用方）。"""
    if not task or task in _EXCLUDED_TASKS:
        return
    if not aux_accounting_enabled():
        return
    try:
        os.makedirs(os.path.dirname(_USAGE_PATH), exist_ok=True)
        with open(_USAGE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "task": task,
                "model": (model or ""),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "latency": round(float(latency or 0), 3),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_aux_usage_summary(limit: int = 20) -> str:
    """最近 N 条记账的聚合文本（供 !perf 展示）；无记录返回提示。"""
    lines: list[dict] = []
    try:
        with open(_USAGE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return "暂无辅助 LLM 调用记录"
    if not lines:
        return "暂无辅助 LLM 调用记录"
    # 按任务聚合：次数 + token 合计 + 平均耗时
    stats: dict[str, dict] = {}
    for d in lines:
        key = str(d.get("task") or "?")
        s = stats.setdefault(key, {
            "count": 0, "prompt": 0, "completion": 0, "latency_sum": 0.0,
        })
        s["count"] += 1
        s["prompt"] += int(d.get("prompt_tokens") or 0)
        s["completion"] += int(d.get("completion_tokens") or 0)
        s["latency_sum"] += float(d.get("latency") or 0)
    out = [f"[辅助调用] 共 {len(lines)} 条 / {len(stats)} 个任务"]
    for task in sorted(stats):
        s = stats[task]
        avg = s["latency_sum"] / s["count"] if s["count"] else 0.0
        out.append(
            f"  {task}: {s['count']}次 in={s['prompt']} out={s['completion']} "
            f"avg={avg:.1f}s")
    # 追加最近 3 条明细
    for d in lines[-3:]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(d.get("ts") or 0))
        out.append(
            f"    {stamp} [{d.get('task')}] {d.get('model')} "
            f"in={d.get('prompt_tokens')} out={d.get('completion_tokens')} "
            f"{d.get('latency')}s")
    return "\n".join(out)
