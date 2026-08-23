"""插件上下文：插件与应用之间的桥梁（日志 / 配置 / 主动发言 / 对话 / LLM / UI / 工具）。

同进程直连：所有方法直接操作 Application 的服务实例（brain / tts / sub / ...），
对标 live-2d 的 plugin_sdk.py 但去掉子进程 JSON-RPC 层。
"""

import asyncio
import copy

from src.utils import console
from plugins.base import VALID_HOOKS


class _Storage:
    """临时存储（进程内，重启清空）。"""

    def __init__(self) -> None:
        self._data = {}

    def get(self, key, default=None):
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key, value) -> None:
        self._data[key] = value

    def delete(self, key) -> None:
        self._data.pop(key, None)

    def get_all(self) -> dict:
        return copy.deepcopy(self._data)


class PluginContext:
    """插件可用的全部能力；this.context 等价物（Python 下为 self.context）。"""

    def __init__(self, manager, plugin_dir: str, plugin_name: str) -> None:
        self._manager = manager
        self._app = manager.app
        self._plugin_dir = plugin_dir
        self._plugin_name = plugin_name
        self._plugin_config: dict = {}   # plugin_config.json（可选）
        self.storage = _Storage()

    # ---- 日志 ----

    def log(self, level: str, message: str) -> None:
        """打日志（显示在终端）：info / warn / error / ok / dim。"""
        text = f"[插件:{self._plugin_name}] {message}"
        if level == "warn":
            console.warn(text)
        elif level == "error":
            console.error(text)
        elif level == "ok":
            console.ok(text)
        elif level == "dim":
            console.dim(text)
        else:
            console.info(text)

    # ---- 配置 ----

    def get_config(self):
        """整个应用配置（config.cfg，即 .env 解析结果）。"""
        return self._app.cfg

    def get_plugin_config(self) -> dict:
        """插件自己的 plugin_config.json 内容（目录下存在时）。"""
        return self._plugin_config

    # ---- 主动发言 ----

    async def send_message(self, text: str) -> None:
        """让 AI 主动说一句话（走完整输出锁 + TTS / 字幕管线）。

        有主动引擎时走其队列（自动忙碌抑制 / 去重）；否则直接持输出锁播报。
        """
        app = self._app
        if app.proactive is not None:
            app.proactive._enqueue(text, None)
            return
        # 无主动引擎：直接持全局输出锁播报（与主动引擎同一条 speak_text 管线）
        from src.core.output_lock import (
            STATE_AI_SPEAKING, STATE_IDLE, get_output_lock,
            set_global_state, set_output_owner,
        )
        from src.llm import stream
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("plugin")
            set_global_state(STATE_AI_SPEAKING)
            try:
                if app.tts is not None:
                    app.tts.clear_interrupt()
                await stream.speak_text(text, app.tts, app.face, app.sub)
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)

    # ---- 对话 ----

    async def get_messages(self) -> list:
        """获取当前对话历史（OpenAI messages 格式）。"""
        app = self._app
        if app.brain is None:
            return []
        return list(app.brain.history)

    def add_system_prompt_patch(self, patch_id: str, text: str) -> None:
        """往系统提示词里注入内容（每次 AI 请求都带着，直到 remove）。"""
        self._manager.add_system_prompt_patch(patch_id, text)

    def remove_system_prompt_patch(self, patch_id: str) -> None:
        """移除已注入的系统提示词片段。"""
        self._manager.remove_system_prompt_patch(patch_id)

    # ---- LLM ----

    async def call_llm(self, prompt: str, options: dict | None = None) -> str:
        """插件自己偷偷问 AI（不进入对话历史）。"""
        app = self._app
        if app.brain is None:
            return ""
        if self._manager._dispatching_llm_request:
            # 防止插件在 on_llm_request 里递归调 LLM 造成死循环
            return ""
        history_len = len(app.brain.history)
        parts = []
        try:
            async for sentence in app.brain.chat_stream(prompt, proactive=True):
                if sentence:
                    parts.append(sentence)
        finally:
            # 不进入对话历史：还原调用前的历史长度（proactive 也会保留回复）
            del app.brain.history[history_len:]
        return "".join(parts).strip()

    # ---- UI ----

    async def show_subtitle(self, text: str, duration: int = 3000) -> None:
        """在屏幕上显示字幕（duration 毫秒后自动清除）。"""
        app = self._app
        if app.sub is None:
            return
        app.sub.push("text", text)
        if duration and duration > 0:
            await asyncio.sleep(duration / 1000.0)
            app.sub.push("clear", "")

    async def trigger_emotion(self, emotion: str) -> None:
        """触发情绪表情（VTS / 桌宠模式，需 emotion_actor 就绪）。"""
        app = self._app
        if app.emotion_actor is None:
            return
        try:
            await app.emotion_actor.play_expression(emotion)
        except Exception as e:
            self.log("dim", f"触发表情失败：{e}")

    # ---- 工具 ----

    def register_tool(self, tool_def: dict) -> None:
        """运行时动态注册工具（OpenAI function calling 格式）。"""
        self._manager.register_dynamic_tool(self._plugin_name, tool_def)

    def unregister_tool(self, name: str) -> None:
        """移除本插件动态注册的工具。"""
        self._manager.unregister_dynamic_tool(self._plugin_name, name)

    # ---- 钩子 / 记忆 provider（3.11 编程式注册 API） ----

    def register_hook(self, name: str, fn) -> None:
        """编程式注册钩子回调：name 必须在 VALID_HOOKS 白名单内。

        转发到管理器事件总线（与 on() 同一分发路径，钩子异常由管理器
        逐个容错）。未知钩子名直接抛错拒绝（fail-closed）。
        """
        if name not in VALID_HOOKS:
            raise ValueError(
                f"未知钩子名：{name}（可用：{sorted(VALID_HOOKS)}）")
        self._manager.on(name, fn, self._plugin_name)

    def register_memory_provider(self, provider) -> None:
        """编程式注册记忆 provider（预留接口，转发到管理器暂存）。

        提供方需实现 query / save 语义的自定义记忆后端；目前为增量注册
        能力（不改变现有 memU 检索路径），后续记忆检索可注入第三方实现。
        """
        self._manager.register_memory_provider(self._plugin_name, provider)

    # ---- 插件间通信 ----

    def get_plugin(self, name: str):
        """获取另一个插件的实例（插件间通信）。"""
        return self._manager.get_plugin(name)

    def on(self, event: str, handler) -> None:
        """订阅事件总线（跨插件松耦合通信）。"""
        self._manager.on(event, handler, self._plugin_name)

    def off(self, event: str, handler) -> None:
        """退订事件总线。"""
        self._manager.off(event, handler, self._plugin_name)

    async def emit(self, event: str, data=None) -> None:
        """发布事件到总线。"""
        await self._manager.emit(event, data)
