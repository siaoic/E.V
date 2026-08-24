"""Agent 间共享黑板：每个 Agent 写结果，其他 Agent 自动看到。

对标 dsh 的 ctx.sessions + scope，但单进程内存版（无持久化需求）：
- Butler 提取事实 → put("recent_facts")
- Evolution 复盘前 → get_recent("recent_facts")，省一次 LLM 信息提取
- Proactive 决策前 → get("user_profile")，不每次问 LLM

设计要点：
- 内存态：进程重启丢，符合"轻量召回"定位；持久化事实仍走记忆库
  （见 loop._commit_recall_files），黑板只做 Agent 间的实时共享
- fail-open：put/notify 异常不影响写入方主流程；get 读不到返回 None，
  调用方走原流程（黑板是优化不是依赖）
- 锁外通知：订阅者回调在锁外触发，防回调死锁卡死写入
- 历史上限：保留最近 _MAX_HISTORY 条，防无界增长
- 跨进程不共享：黑板是进程内单例，多进程（如 ASR 服务）各自独立
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Optional

# 历史记录上限：超过则丢弃最旧（保尾），防无界增长
_MAX_HISTORY = 1000
# 默认时效窗口：get_recent 默认读最近 5 分钟内的值
_DEFAULT_RECENT_WINDOW_SEC = 300.0


class AgentBlackboard:
    """Agent 间共享黑板：put 写 + 通知订阅，get 读最新，get_recent 读时效内。"""

    def __init__(self) -> None:
        self._board: dict[str, Any] = {}                       # key -> 最新值
        self._history: list[dict] = []                          # 时序记录（带 ts/source）
        self._subscribers: dict[str, list[Callable[[dict], Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: Any, source: str) -> None:
        """Agent A 写：'我刚刚提取了这些事实'，通知订阅者（fail-open）。

        写入本身不抛异常（订阅者异常隔离）；source 用于审计追溯。
        """
        entry = {"key": key, "value": value, "source": source, "ts": time.time()}
        async with self._lock:
            self._board[key] = value
            self._history.append(entry)
            # 历史上限：超限丢弃最旧（保尾）
            if len(self._history) > _MAX_HISTORY:
                # 切片保留尾部，避免逐条 pop 的 O(n) 移动
                del self._history[:len(self._history) - _MAX_HISTORY]
            subs = list(self._subscribers.get(key, []))
        # 通知在锁外执行：防订阅者回调死锁卡死后续写入
        for cb in subs:
            try:
                ret = cb(entry)
                if asyncio.iscoroutine(ret):
                    # 异步订阅者 fire-and-forget，不阻塞 put
                    asyncio.create_task(ret)
            except Exception:
                pass  # 订阅者异常不影响黑板与写入方

    def get(self, key: str, default: Any = None) -> Any:
        """读最新值（无时效约束）；读不到返回 default。"""
        return self._board.get(key, default)

    def get_recent(self, key: str,
                   within_sec: float = _DEFAULT_RECENT_WINDOW_SEC) -> Optional[Any]:
        """读'最近 within_sec 秒内'的值；超期或不存在返回 None。

        从历史尾部向前扫描（最近优先），命中即返回。
        """
        now = time.time()
        for entry in reversed(self._history):
            if entry["key"] == key and now - entry["ts"] < within_sec:
                return entry["value"]
        return None

    def subscribe(self, key: str,
                  callback: Callable[[dict], Any]) -> Callable[[], None]:
        """订阅 key 变更；返回取消订阅函数（防订阅泄漏）。

        回调签名 f(entry: dict)，entry 含 key/value/source/ts。
        异步回调以 fire-and-forget 方式触发（不阻塞 put）。
        """
        self._subscribers[key].append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers[key].remove(callback)
            except ValueError:
                pass  # 已取消或未注册，静默
        return _unsubscribe


# 进程内单例（懒加载，首次 get_blackboard() 时构造）
_blackboard: Optional[AgentBlackboard] = None


def get_blackboard() -> AgentBlackboard:
    """获取进程内黑板单例（懒加载）。"""
    global _blackboard
    if _blackboard is None:
        _blackboard = AgentBlackboard()
    return _blackboard
