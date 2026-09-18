"""世界状态的「变化驱动 + 按需注入」缓存。

设计要点（对齐计划 §3.2）：
- 变化检测与渲染分离：调用方只在检测到变化时才渲染并调用 :meth:`update`；
- 注入时机是 planner 请求时（pull），而不是定时推送；
- 无变化时只在超过 ``stale_after_seconds`` 后补注入一次，保持 AI 对时间流逝的感知。

``stale_after_seconds`` 以回调方式注入：配置热重载后无需重建缓存即可生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple


@dataclass
class WorldStateEntry:
    """单个世界的状态缓存条目。"""

    world_name: str
    snapshot: str = ""
    dirty: bool = False
    last_changed_at: float = 0.0
    last_injected_at: float = 0.0


class WorldStateCache:
    """维护每个世界的状态快照与注入时机。

    每个世界最多两个视图槽位：实时视图（给 AI 决策用）与延迟视图
    （「观众此刻看到的画面」，防「先知穿帮」，只有声明
    ``supports_delayed`` 且配置在 ``worlds.delayed_sources`` 的世界才有）。
    两个槽位各自独立 ``dirty`` / ``last_injected_at``。
    """

    def __init__(self, *, stale_after_seconds: Callable[[], float]) -> None:
        """初始化状态缓存。

        Args:
            stale_after_seconds: 返回「无变化时两次注入之间最长间隔（秒）」的回调。
        """

        self._stale_after_seconds = stale_after_seconds
        self._entries: Dict[str, WorldStateEntry] = {}
        self._delayed_entries: Dict[str, WorldStateEntry] = {}

    def _table(self, *, delayed: bool) -> Dict[str, WorldStateEntry]:
        return self._delayed_entries if delayed else self._entries

    def update(self, world_name: str, snapshot: str, *, now: float, delayed: bool = False) -> bool:
        """写入新快照。

        Args:
            world_name: 世界机器名。
            snapshot: 已渲染的中文状态文本。
            now: 单调时钟当前时刻。
            delayed: 写入延迟视图（观众此刻看到的画面）时为 True。

        Returns:
            bool: 快照内容相对旧值发生变化时为 ``True``。
        """

        entries = self._table(delayed=delayed)
        entry = entries.setdefault(world_name, WorldStateEntry(world_name=world_name))
        normalized_snapshot = str(snapshot or "").strip()
        if normalized_snapshot == entry.snapshot:
            return False

        entry.snapshot = normalized_snapshot
        entry.dirty = True
        entry.last_changed_at = now
        return True

    def snapshot_of(self, world_name: str) -> str:
        """返回指定世界的当前实时快照，未注册时返回空串。"""

        entry = self._entries.get(world_name)
        return "" if entry is None else entry.snapshot

    def delayed_snapshot_of(self, world_name: str) -> str:
        """返回指定世界的当前延迟快照，不存在时返回空串。"""

        entry = self._delayed_entries.get(world_name)
        return "" if entry is None else entry.snapshot

    def refresh(self, world_name: str, snapshot: str, *, now: float) -> bool:
        """写入一份「已经直接交付给模型」的即时快照。

        ``world_observe`` 工具拉回的即时状态已经作为工具结果进入本轮上下文，
        不需要在下次 planner 请求时再注入一遍，因此这里写入快照的同时清掉
        ``dirty`` 并把 ``last_injected_at`` 推到当前时刻。

        Args:
            world_name: 世界机器名。
            snapshot: 已渲染的中文状态文本。
            now: 单调时钟当前时刻。

        Returns:
            bool: 快照内容相对旧值发生变化时为 ``True``。
        """

        changed = self.update(world_name, snapshot, now=now)
        entry = self._entries[world_name]
        entry.dirty = False
        entry.last_injected_at = now
        return changed

    def drop(self, world_name: str) -> None:
        """移除指定世界的全部缓存条目（两个视图一并丢弃）。"""

        self._entries.pop(world_name, None)
        self._delayed_entries.pop(world_name, None)

    def build_blocks(self, world_names: Sequence[str], *, now: float, delayed: bool = False) -> List[Tuple[str, str]]:
        """按三条规则决定本次请求要注入的世界状态。

        规则：
        1. 自上次注入以来有变化（``dirty``）→ 注入并清除脏标记；
        2. 无变化且距上次注入不足 ``stale_after_seconds`` → 不注入；
        3. 无变化且距上次注入已达到 ``stale_after_seconds`` → 补注入一次。

        这里只返回被选中的 ``(世界机器名, 快照)`` 对，标题前缀与拼接格式由调用方
        决定，避免缓存层耦合展示格式。延迟视图（``delayed=True``）用独立的
        槽位与计时，与实时视图互不影响。

        Args:
            world_names: 需要参与注入的世界机器名，顺序即注入顺序。
            now: 单调时钟当前时刻。
            delayed: 取延迟视图（观众此刻看到的画面）时为 True。

        Returns:
            List[Tuple[str, str]]: 本次应当注入的 ``(世界机器名, 快照)`` 列表。
        """

        stale_after_seconds = max(float(self._stale_after_seconds()), 1.0)
        entries = self._table(delayed=delayed)
        blocks: List[Tuple[str, str]] = []
        for world_name in world_names:
            entry = entries.get(world_name)
            if entry is None or not entry.snapshot:
                continue

            if entry.dirty:
                entry.dirty = False
                entry.last_injected_at = now
                blocks.append((world_name, entry.snapshot))
                continue

            if now - entry.last_injected_at >= stale_after_seconds:
                entry.last_injected_at = now
                blocks.append((world_name, entry.snapshot))

        return blocks
