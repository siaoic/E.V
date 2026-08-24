"""Echo LLM：对 LLMContract 的最小实现，原样回显文本（用于 Kernel demo）。

满足协议（从 src.core.slots import LLMContract 验证 isinstance）：
  name: str
  chat_stream(text, *, proactive=False, history=None) -> AsyncIterator[str]
  push_turn_context(contexts: list[str]) -> None
  reload_client() -> None
额外：
  prefix: str（可选，来自 ctx.config.get("prefix", "[Echo] ")）
  contexts: list（存 push_turn_context 注入的内容，下次 echo 时附加在文本前）
"""
from __future__ import annotations
from typing import Any, AsyncIterator, Optional

from plugins.base import Plugin  # noqa: F401  (让 PluginManager 能找到 Plugin 子类)


# ---------- 入口：register(ctx)（T7 会调用） ----------
def register(ctx) -> None:
    # 读取配置：可配置前缀（缺省 "[Echo] "）
    cfg = ctx.config
    prefix: str = cfg.get("prefix", "[Echo] ")
    # 实例化
    impl = EchoLLM(prefix=prefix)
    # 注册到 LLM slot，impl_name = "echo-default"（如果 config 有 custom_name 也可覆盖）
    impl_name: str = cfg.get("impl_name", "echo-default")
    try:
        from ev.kernel.slots import SlotName
    except Exception as e:
        ctx.log("error", f"无法导入 SlotName: {e}")
        return
    if ctx.slots is None:
        ctx.log("warn", "ctx.slots 为 None（4.x 路径？），跳过 slot 注册")
        return
    try:
        ctx.slots.register(SlotName.model, impl_name, impl)
    except Exception as e:
        ctx.log("error", f"注册 LLM slot 失败 ({impl_name}): {e}")
        return
    ctx.log("ok", f"已注册 LLM 槽位: {impl_name}（prefix={prefix!r}）")


# ---------- EchoLLM 实现（满足 LLMContract @runtime_checkable）----------
class EchoLLM:
    """LLMContract 最小实现：原样回显（附加 prefix + 已注入 context）。"""

    name: str = "echo"

    def __init__(self, prefix: str = "[Echo] ") -> None:
        self._prefix = prefix
        self._turn_contexts: list[str] = []
        self._client: str = "echo-stub"   # 用于 reload_client() 验证 id 变化

    async def chat_stream(
        self,
        text: str,
        *,
        proactive: bool = False,
        history: Optional[list[dict[str, Any]]] = None,
    ) -> AsyncIterator[str]:
        """流式生成：合并 contexts + prefix + text 后按句 yield。"""
        parts: list[str] = []
        if self._turn_contexts:
            parts.append("（注入背景：" + "；".join(self._turn_contexts) + "）")
        if proactive:
            parts.append(self._prefix + "（主动） " + text)
        else:
            parts.append(self._prefix + text)
        full = "".join(parts)
        # 清空 turn contexts（一次消费；与 LLMBrain.clear_turn 语义对齐：消费即清空）
        self._turn_contexts.clear()
        # 流式：按 len(full)//4 切 3~4 块（模拟真实 LLM 分块）
        n = max(1, len(full) // 4)
        for i in range(0, len(full), n):
            chunk = full[i:i + n]
            if chunk:
                yield chunk

    def push_turn_context(self, contexts: list[str]) -> None:
        """注入本轮背景：合并到下次 chat_stream 输出。"""
        if contexts:
            self._turn_contexts.extend(contexts)

    def reload_client(self) -> None:
        """重建 client（演示用：生成新的 stub id，验证对象替换）。"""
        import time
        self._client = f"echo-stub-reloaded-{int(time.time()*1000)}"


# ---------- 空 Plugin 子类：让 PluginManager.load() 不抛错 ----------
class EchoLLMPlugin(Plugin):
    """Echo LLM 插件：真正的注册逻辑在 register(ctx)，此处保持空实现。"""
    pass
