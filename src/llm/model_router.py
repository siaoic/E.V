"""模型路由进化：多臂老虎机（UCB1）自动选择最优 LLM 服务。

对标 hermes-agent 的 multi-armed bandit model selection：配置多个 LLM
服务后，根据历史成功率自动路由到表现最好的服务，越用越准。

配置（.env）：
- LLM_SERVERS：逗号分隔的服务列表，每项「名称;base_url;api_key;model」
  （名称建议小写下划线）。配置 2 个及以上才启用路由；未配置或不足 2 个
  时 select() 返回 None，主对话完全走原有单一 LLM 服务逻辑，行为不变。
- LLM_ROUTER_ENABLED：总开关（默认 True），关闭后回到单一服务。
- LLM_ROUTER_EPSILON：探索率（默认 0.1）：小概率随机探索非最优服务，
  防止把某个服务的一次偶然成功当成长期最优。

奖励与选择（UCB1）：
- 每次调用成功记奖励 1.0、失败记 0.0；未尝试过的服务永远优先
- 选择 argmax(平均奖励 + sqrt(2 ln(总尝试数) / 该服务尝试数))
- 路由服务调用失败时记录统计并回退默认 LLM 服务重试（见 llm_brain）

统计持久化到 data/model_router.json 供用户审阅（每次调用实时覆写，
线程安全）。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time

from openai import OpenAI

from src.utils import config, console

# 路由统计持久化文件：name -> {tries, wins, latency_sum, last_at}
_STATE_PATH = os.path.join(config.cfg.PROJECT_ROOT, "data", "model_router.json")


def _parse_servers() -> list[dict]:
    """解析 LLM_SERVERS 配置为服务列表（非法项跳过，空列表 = 不启用路由）。

    每项格式：名称;base_url;api_key;model（api_key 可留空，与现有
    "not-needed" 占位语义一致）。
    """
    raw = (config.cfg.LLM_SERVERS or "").strip()
    if not raw:
        return []
    servers: list[dict] = []
    for seg in raw.split(","):
        parts = [p.strip() for p in seg.split(";")]
        if len(parts) < 4:
            console.warn(f"[模型路由] 服务配置格式错误，已跳过：{seg!r}")
            continue
        name = re.sub(r"[^a-z0-9_]+", "_", parts[0].lower()).strip("_")
        if not name:
            continue
        servers.append({
            "name": name,
            "base_url": parts[1],
            "api_key": parts[2],
            "model": parts[3],
        })
    return servers


class ModelRouter:
    """多臂老虎机模型路由（UCB1），统计持久化，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers = _parse_servers()
        self._stats: dict[str, dict] = self._load_stats()
        self._clients: dict[str, OpenAI] = {}
        # 服务列表变化后清理已不存在的臂统计（防残留干扰选择）
        active = {s["name"] for s in self._servers}
        for name in [n for n in self._stats if n not in active]:
            del self._stats[name]

    @property
    def enabled(self) -> bool:
        """是否启用路由：总开关开启且配置了 2 个及以上服务。"""
        return (bool(getattr(config.cfg, "LLM_ROUTER_ENABLED", True))
                and len(self._servers) >= 2)

    def _load_stats(self) -> dict[str, dict]:
        """读取路由统计（文件缺失/损坏时返回空字典）。"""
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_stats(self) -> None:
        """覆写路由统计文件（失败静默，统计丢失不影响运行）。"""
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.warn(f"[模型路由] 写入统计失败：{e}")

    def service(self, name: str) -> dict | None:
        """按名称取服务配置，不存在返回 None。"""
        for s in self._servers:
            if s["name"] == name:
                return s
        return None

    def client_for(self, name: str) -> OpenAI | None:
        """构建（并缓存）指定服务的 OpenAI 客户端，失败返回 None。"""
        service = self.service(name)
        if service is None:
            return None
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                client = OpenAI(
                    api_key=service["api_key"] or "not-needed",
                    base_url=service["base_url"] or None,
                    timeout=120.0,
                    max_retries=2,
                )
                self._clients[name] = client
            return client

    def select(self) -> str | None:
        """按 UCB1 选择本次调用的服务名；未启用路由返回 None（走默认服务）。

        未尝试过的臂永远优先；epsilon 概率随机探索，防止局部最优。
        """
        if not self.enabled or not self._servers:
            return None
        with self._lock:
            stats = {n: dict(s) for n, s in self._stats.items()}
        if random.random() < float(getattr(config.cfg, "LLM_ROUTER_EPSILON", 0.1)):
            return random.choice(self._servers)["name"]
        total = sum(int(s.get("tries") or 0) for s in stats.values())
        best_name: str | None = None
        best_ucb = -math.inf
        for s in self._servers:
            name = s["name"]
            st = stats.get(name)
            if st is None or int(st.get("tries") or 0) == 0:
                return name  # 未尝试过的臂优先
            tries = int(st["tries"])
            avg = float(st.get("wins") or 0) / tries
            ucb = avg + math.sqrt(2 * math.log(total + 1) / tries)
            if ucb > best_ucb:
                best_ucb = ucb
                best_name = name
        return best_name

    def record(self, name: str, success: bool, latency: float = 0.0) -> None:
        """记录一次调用的成功/失败与耗时，更新臂统计并持久化。"""
        with self._lock:
            st = self._stats.setdefault(
                name, {"tries": 0, "wins": 0, "latency_sum": 0.0})
            st["tries"] = int(st.get("tries") or 0) + 1
            st["wins"] = int(st.get("wins") or 0) + (1 if success else 0)
            st["latency_sum"] = float(st.get("latency_sum") or 0.0) + max(0.0, latency)
            st["last_at"] = time.time()
            self._save_stats()


_router: ModelRouter | None = None


def get_router() -> ModelRouter | None:
    """获取模型路由单例；未配置任何 LLM 服务时返回 None。"""
    global _router
    if _router is None:
        _router = ModelRouter()
        if not _router._servers:
            _router = None  # 无服务配置 → 视为未启用，避免无谓对象
    return _router


__all__ = ["ModelRouter", "get_router"]
