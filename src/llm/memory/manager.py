"""MemoryManager —— 记忆 Provider 编排器（Hermes 式 memory_manager.py 精简落地）。

统一调度 L4 层的所有 MemoryProvider：注册、系统提示汇总、每轮召回/写入、
边界事件分发、关闭清理。单个 Provider 失败不阻塞其他 Provider。

对标 Hermes agent/memory_manager.py 的编排语义：
- 1 个内置 + 至多 1 个外部 Provider（add_provider 拒绝第 2 个外部）；
- prefetch_all 召回带硬超时（MEMORY_GATE_TIMEOUT，0 关闭），慢后端不拖垮对话；
- sync_all 写回走后台单 worker 串行（防并发写库），shutdown_all 最多 5s drain；
- 召回/静态提示用 <memory-context> 围栏包裹，流式输出侧配
  StreamingContextScrubber 剥除（防模型复读记忆内容）；
- build_system_prompt 会话级冻结（frozen snapshot）：只构建一次，
  会话中 Provider 写盘不刷新 prompt（保 system 字节稳定，缓存友好）。

设计约束（贴合本项目规范）：
- 不引入第三方库（后台 worker 用标准库 threading + queue）；
- 提供 async 接口（与 asyncio 主循环一致），内部丢线程池调同步 provider；
- 进程内单例，!config memory 热重载时 reset。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any, Optional

from src.utils import config, console
from src.llm.memory.provider import MemoryProvider

# 记忆注入围栏（对标 Hermes build_memory_context_block）：让模型清楚这是
# 检索到的记忆而非自己说过的话，降低复读风险；输出侧由 Scrubber 剥除。
_MEMORY_FENCE_START = "<memory-context>"
_MEMORY_FENCE_END = "</memory-context>"

# 后台写回队列容量上限：突发高峰写回积压时丢弃最旧，绝不阻塞主循环
_SYNC_QUEUE_MAX = 500
# shutdown drain 上限（秒）：退出时最多等这么久让后台写回落地
_SYNC_DRAIN_SECONDS = 5.0


class StreamingContextScrubber:
    """流式剥除 <memory-context> 围栏内容（对标 Hermes StreamingContextScrubber）。

    记忆注入段若被模型原样复读进输出，会污染对话上下文、浪费 TTS。
    对整句/增量文本调用 scrub()：检测围栏起止标记并剥除其覆盖区间。
    未出现围栏时原样返回（零开销、行为不变）。

    用法（整句）：``cleaned = StreamingContextScrubber.strip(text)``
    用法（增量流式）：实例化后逐段喂 ``scrub(chunk)``，内部跨 chunk 记住
    围栏开始状态，直到遇到结束标记才恢复输出。
    """

    def __init__(self) -> None:
        self._inside = False  # 已看到 <memory-context>，等待结束标记

    def scrub(self, text: str) -> str:
        """剥除一段流式文本中的围栏区间（支持跨 chunk 状态）。"""
        if not text:
            return ""
        if self._inside:
            end = text.find(_MEMORY_FENCE_END)
            if end < 0:
                return ""  # 仍在围栏内，整段丢弃
            self._inside = False
            text = text[end + len(_MEMORY_FENCE_END):]
        result: list[str] = []
        while text:
            start = text.find(_MEMORY_FENCE_START)
            if start < 0:
                result.append(text)
                break
            result.append(text[:start])
            rest = text[start + len(_MEMORY_FENCE_START):]
            end = rest.find(_MEMORY_FENCE_END)
            if end < 0:
                self._inside = True  # 结束标记在更后面的 chunk
                break
            text = rest[end + len(_MEMORY_FENCE_END):]
        return "".join(result)

    @staticmethod
    def strip(text: str) -> str:
        """整段剥除（一次性处理完整文本的便捷入口）。"""
        return StreamingContextScrubber().scrub(text)


class MemoryManager:
    """编排全部已注册的记忆 Provider（当前内置 memU，未来可加外部）。"""

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._external_count = 0
        # 系统提示冻结快照（frozen snapshot）：会话内只构建一次
        self._frozen_prompt: Optional[str] = None
        # 后台写回：单 worker 串行执行（防并发写库），shutdown 时 drain
        self._sync_queue: "queue.Queue" = queue.Queue(maxsize=_SYNC_QUEUE_MAX)
        self._sync_worker: Optional[threading.Thread] = None
        self._sync_closed = False

    # -- 注册 ----------------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """注册一个记忆 Provider（不可用/重名则拒绝并告警）。

        对标 Hermes：1 个内置 + 至多 1 个外部 Provider；注册第 2 个外部
        Provider 时拒绝（防多后端语义冲突）。
        """
        if self.get_provider(provider.name) is not None:
            console.warn(f"[记忆编排] Provider '{provider.name}' 已注册，忽略重复")
            return
        if not provider.is_available():
            console.dim(f"[记忆编排] Provider '{provider.name}' 不可用，未激活")
            return
        if not provider.is_builtin and self._external_count >= 1:
            console.warn(
                f"[记忆编排] 外部 Provider 已有 1 个（至多 1 个），"
                f"拒绝注册 '{provider.name}'")
            return
        self._providers.append(provider)
        if not provider.is_builtin:
            self._external_count += 1
        console.ok(f"[记忆编排] Provider '{provider.name}' 已激活")

    @property
    def providers(self) -> list[MemoryProvider]:
        """全部已注册（按注册顺序）。"""
        return list(self._providers)

    def get_provider(self, name: str) -> Optional[MemoryProvider]:
        """按名字取 Provider；未注册返回 None。"""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    # -- 系统提示 ------------------------------------------------------------

    def build_memory_context_block(self, text: str) -> str:
        """用 <memory-context> 围栏包裹注入文本（对标 Hermes）。

        围栏让模型清楚这是检索到的记忆而非自己的话（防复读），流式
        输出侧配合 StreamingContextScrubber 剥除。
        """
        return f"{_MEMORY_FENCE_START}\n{text}\n{_MEMORY_FENCE_END}"

    def build_system_prompt(self) -> str:
        """汇总全部 Provider 的静态系统提示段（会话级冻结，只构建一次）。

        Frozen snapshot（对标 Hermes MemoryStore.load_from_disk）：会话开始
        一次性冻结进 system prompt，会话中 Provider 写盘不刷新（保字节稳定，
        与 Prompt Cache 分层组装配合）。reset_memory_manager() 会清空冻结缓存。
        """
        if self._frozen_prompt is not None:
            return self._frozen_prompt
        blocks = []
        for p in self._providers:
            try:
                block = p.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} system_prompt_block 失败：{e}")
        self._frozen_prompt = "\n\n".join(blocks)
        return self._frozen_prompt

    # -- 每轮召回 / 写入 -----------------------------------------------------

    async def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """收集全部 Provider 的召回上下文（单个失败不影响其他）。

        每个 Provider 的召回带硬超时（MEMORY_GATE_TIMEOUT，0 关闭）：
        慢后端超时后丢弃该 Provider 结果，优先保障对话首字延迟。
        返回文本用 <memory-context> 围栏包裹（无召回时返回空串）。
        """
        parts = []
        for p in self._providers:
            try:
                timeout = float(
                    getattr(config.cfg, "MEMORY_GATE_TIMEOUT", 8.0) or 0)
                if timeout > 0:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            p.prefetch, query, session_id=session_id),
                        timeout=timeout)
                else:
                    result = await asyncio.to_thread(
                        p.prefetch, query, session_id=session_id)
                if result and result.strip():
                    parts.append(result)
            except asyncio.TimeoutError:
                console.warn(
                    f"[记忆编排] {p.name} prefetch 超时"
                    f"（>{getattr(config.cfg, 'MEMORY_GATE_TIMEOUT', 8.0)}s），跳过")
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} prefetch 失败：{e}")
        if not parts:
            return ""
        return self.build_memory_context_block("\n\n".join(parts))

    def _ensure_sync_worker(self) -> None:
        """确保后台写回 worker 存活（daemon 线程，进程退出自动回收）。"""
        if self._sync_worker is not None and self._sync_worker.is_alive():
            return
        self._sync_worker = threading.Thread(
            target=self._sync_worker_loop, name="memory-sync", daemon=True)
        self._sync_worker.start()

    def _sync_worker_loop(self) -> None:
        """后台单 worker：串行执行各 Provider 的 sync_turn（防并发写库）。"""
        while True:
            item = self._sync_queue.get()
            if item is None:  # 停止哨兵
                self._sync_queue.task_done()
                break
            provider, kwargs = item
            try:
                provider.sync_turn(**kwargs)
            except Exception as e:
                console.warn(f"[记忆编排] {provider.name} sync_turn 失败：{e}")
            finally:
                self._sync_queue.task_done()

    async def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """每轮对话结束后把写回入队（后台单 worker 串行，主循环不阻塞）。

        对标 Hermes sync_all：异步入队立即返回；队列满（突发高峰）时
        丢弃最旧 Provider 的写回并告警，绝不阻塞对话主链路。
        """
        if self._sync_closed:
            return
        self._ensure_sync_worker()
        for p in self._providers:
            kwargs = dict(
                user_content=user_content,
                assistant_content=assistant_content,
                session_id=session_id,
                messages=messages,
            )
            try:
                self._sync_queue.put_nowait((p, kwargs))
            except queue.Full:
                console.warn(
                    f"[记忆编排] 写回队列已满（{_SYNC_QUEUE_MAX}），"
                    f"丢弃 {p.name} 本轮写回")

    # -- 边界事件 ------------------------------------------------------------

    async def commit_session_boundary(
        self, messages: list[dict[str, Any]]) -> None:
        """会话边界：通知全部 Provider 做会话结束提取（复盘/落盘）。"""
        snapshot = list(messages or [])
        for p in self._providers:
            try:
                await asyncio.to_thread(p.on_session_end, snapshot)
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} on_session_end 失败：{e}")

    async def on_pre_compress(
        self, messages: list[dict[str, Any]]) -> str:
        """上下文压缩前通知全部 Provider，返回并入压缩摘要的文本。"""
        parts = []
        snapshot = list(messages or [])
        for p in self._providers:
            try:
                result = await asyncio.to_thread(p.on_pre_compress, snapshot)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} on_pre_compress 失败：{e}")
        return "\n\n".join(parts)

    def notify_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """L2 curated memory 工具写入后镜像给全部 Provider。

        默认 Provider 均 no-op；未来实现镜像的后端可在此收到通知。
        """
        for p in self._providers:
            try:
                p.on_memory_write(action, target, content, metadata=metadata)
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} on_memory_write 失败：{e}")

    # -- 关闭 ----------------------------------------------------------------

    async def shutdown_all(self) -> None:
        """关闭全部 Provider：先 drain 后台写回队列（最多 5s），再逆序清理。"""
        self._sync_closed = True
        if self._sync_worker is not None and self._sync_worker.is_alive():
            self._sync_queue.put(None)  # 停止哨兵
            self._sync_worker.join(timeout=_SYNC_DRAIN_SECONDS)  # 5s drain 上限
        for p in reversed(self._providers):
            try:
                await asyncio.to_thread(p.shutdown)
            except Exception as e:
                console.warn(f"[记忆编排] {p.name} shutdown 失败：{e}")


# ---------- 进程内单例（懒构建） ----------

_instance: Optional[MemoryManager] = None
_init_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """返回进程内单例（首次访问时注册内置 memU Provider）。"""
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = MemoryManager()
                # 延迟导入，避免 provider.py 依赖 tools.memory 造成循环导入
                from src.llm.memory.memu_provider import MemUProvider
                _instance.add_provider(MemUProvider())
    return _instance


def reset_memory_manager() -> None:
    """清空单例（!config memory 热重载时调用），冻结快照一并失效。"""
    global _instance
    with _init_lock:
        _instance = None
