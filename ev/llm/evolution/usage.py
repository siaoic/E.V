"""进化 LLM 调用记账（5.16）。

EvolutionEngine 全家（复盘 / 技能审阅 / GEPA / 技能评估）的 LLM 调用
统一记录到 DATA_ROOT/evolution_usage.jsonl（task/model/prompt_tokens/
completion_tokens/latency/ts），供面板/控制中心展示各任务的 token 与耗时。

记账为旁路：文件写失败静默，绝不阻塞或改变任何 LLM 调用的行为与结果。
"""

from __future__ import annotations

import json
import os
import time

from ev.utils import config

# 记账文件（可写数据根 evolution/ 子目录）：JSONL 追加写，一条一行
_USAGE_PATH = os.path.join(config.cfg.DATA_ROOT, "evolution",
                           "evolution_usage.jsonl")


def record_usage(task: str, model: str, prompt_tokens: int,
                 completion_tokens: int, latency: float) -> None:
    """追加一条 LLM 调用记账（失败静默，不影响调用方）。"""
    if not task:
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


def usage_summary(limit: int = 20) -> str:
    """最近 N 条记账的人类可读文本（供面板展示）；无记录/失败返回提示。"""
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
        return "暂无进化 LLM 调用记录"
    if not lines:
        return "暂无进化 LLM 调用记录"
    out = []
    for d in lines[-limit:]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(d.get("ts") or 0))
        out.append(
            f"{stamp} [{d.get('task')}] {d.get('model')} "
            f"in={d.get('prompt_tokens')} out={d.get('completion_tokens')} "
            f"{d.get('latency')}s")
    return "\n".join(out)
