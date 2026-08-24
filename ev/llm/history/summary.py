"""长对话摘要压缩：被裁剪的早期轮次后台压缩成摘要，跨会话继承。

以混入类提供，LLMBrain 继承后方法仍用 self 访问任务状态
（_summary_task / _session_summary 由 LLMBrain.__init__ 初始化持有）。
"""

import asyncio
from typing import Optional

from ev.utils import console


class _SummaryMixin:
    """早期对话摘要：后台压缩 + 非阻塞取用。"""

    def _consume_summary(self) -> Optional[str]:
        """取后台摘要任务的结果（完成即用，未完成则等下一轮），返回当前生效摘要。

        非阻塞：不等待任务，避免拖慢本轮对话首字延迟。
        """
        if self._summary_task is not None:
            if self._summary_task.done():
                try:
                    result = self._summary_task.result()
                    if isinstance(result, str) and result.strip():
                        self._session_summary = result.strip()
                except Exception as e:
                    console.dim(f"[摘要] 后台压缩失败：{e}")
                self._summary_task = None
        return self._session_summary

    async def _summarize_dropped(self, turns: list[dict]) -> str:
        """后台任务：把被裁剪的早期对话压缩成中文摘要，并写入记忆跨会话继承。"""
        try:
            from ev.llm.butler_agent import ButlerAgent, _pick_owner
            butler = ButlerAgent()
            text = await asyncio.wait_for(
                butler.summarize_session(turns), timeout=30.0
            )
            text = (text or "").strip()
            if not text:
                return ""
            # 摘要写入记忆（归属从被裁剪轮次推断），重启后检索可带出
            try:
                owner = _pick_owner(turns, None)
                await butler.commit_recall_files([{
                    "name": "session-summary",
                    "description": "archive/对话摘要：早期对话压缩",
                    "content": text,
                    "user": owner,
                }])
            except Exception:
                pass
            return text
        except Exception as e:
            console.dim(f"[摘要] 后台压缩失败：{e}")
            return ""
