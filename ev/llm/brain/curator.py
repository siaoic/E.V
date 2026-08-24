import asyncio
from typing import List

from ev.utils import console


class _CuratorMixin:
    """会后治理 Mixin：L3 会话历史落盘 + L4 会后秘书复盘。"""

    async def _persist_session_history(self, rows: List[dict]) -> None:
        """后台把本轮消息落盘到 L3 会话历史库（SQLite+FTS5）。

        通过 asyncio.to_thread 丢线程池执行（sqlite 同步 IO 不阻塞事件循环），
        落盘失败仅记日志，不影响对话主链路（历史库只是可追溯的旁路）。
        """
        try:
            from ev.llm.memory.session import _session_id, get_session_store
            await asyncio.to_thread(
                get_session_store().add_messages, _session_id(), rows)
        except Exception as e:
            console.warn(f"[会话历史] 落盘失败：{e}")

    def _schedule_curator_review(self) -> None:
        """低频调度会后复盘：每 MEMORY_CURATOR_INTERVAL 轮且无进行中任务时触发。

        复盘提取是独立后台任务（额外 LLM 调用），不阻塞本轮回复。
        """
        if not getattr(self.cfg, "MEMORY_CURATOR_ENABLED", True):
            return
        interval = max(1, int(getattr(self.cfg, "MEMORY_CURATOR_INTERVAL", 10)))
        self._curator_turn_count += 1
        if self._curator_turn_count % interval != 0:
            return
        if self._curator_task is not None and not self._curator_task.done():
            return
        self._curator_task = asyncio.create_task(self._curator_review())

    async def _curator_review(self) -> None:
        """会后秘书：后台复盘最近对话，把值得长期记住的事实写入 L2。

        写入走 curated store（去重 + 威胁扫描 + 字符上限），只影响下一次
        会话的冻结快照；失败静默，不影响主链路。
        """
        from ev.llm.memory.govern import run_curator

        snapshot = list(self.history)
        try:
            written = await run_curator(self.client, self.cfg, snapshot)
            if written:
                console.ok(f"[记忆复盘] 已沉淀 {written} 条长期记忆（下次会话生效）")
        except Exception as e:
            console.warn(f"[记忆复盘] 失败：{e}")
