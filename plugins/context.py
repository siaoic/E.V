"""插件上下文：插件与应用之间的桥梁（日志 / 配置 / 主动发言 / 对话 / LLM / UI / 工具）。

同进程直连：所有方法直接操作 Application 的服务实例（brain / tts / sub / ...），
对标 live-2d 的 plugin_sdk.py 但去掉子进程 JSON-RPC 层。
"""

import asyncio
import copy
from typing import Callable, Optional

from ev.utils import console
from ev.agent.tool_registry import ToolContext
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


class ConfigView:
    """插件专属配置的视图：像 dict 一样访问，但缺 KeyError 有友好提示。"""

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict = dict(data) if data else {}

    def __getitem__(self, key: str):
        if key not in self._data:
            available = ", ".join(sorted(self._data.keys())) if self._data else "(空)"
            raise KeyError(
                f"插件配置中不存在键 '{key}'。当前可用键：{available}"
            )
        return self._data[key]

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def to_dict(self) -> dict:
        """返回配置副本（避免外部就地修改内部数据）。"""
        import copy
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
        self.tools = ToolContext()       # ctx.tools：统一工具注册接口（L3-C）

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
        from ev.kernel.output_lock import (
            STATE_AI_SPEAKING, STATE_IDLE, get_output_lock,
            set_global_state, set_output_owner,
        )
        from ev.llm import stream
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
            async for item in app.brain.chat_stream(prompt, proactive=True):
                # chat_stream 新协议：只关心 final 段，跳过 delta
                if isinstance(item, tuple) and item and item[0] == "final":
                    sentence = item[1]
                elif isinstance(item, str):
                    sentence = item
                else:
                    continue
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

    # ---- 后台任务（L3-B） ----

    def start_job(self, kind: str, run, label: str = "") -> str:
        """启动后台任务，返回 job_id；LLM 可经 jobs_get_output 工具查询。

        run 为同步或 async 函数，返回字符串结果（完成后写入 job 输出，
        查询工具会截断到 4000 字）。长任务（>10s）不应阻塞对话流——
        用本接口后台化，再让 LLM 下一轮用 jobs_list / jobs_get_output
        查询状态与结果。
        """
        return self._manager.start_background_job(
            self._plugin_name, kind, run, label)

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

    # ======================================================================
    # 5.0 新增：slots / jobs / session / config / register_subcommand
    # 有 kernel 时返回真实对象；无 kernel（4.x 路径）返回合理空值，绝不 crash
    # ======================================================================

    @property
    def slots(self):
        """SlotRegistry：插件注册自己的实现，或查询活跃实现。无 Kernel 时返回 None。"""
        kernel = getattr(self._manager, "kernel", None)
        if kernel is not None:
            return getattr(kernel, "slots", None)
        return None

    @property
    def jobs(self):
        """JobScheduler：声明式周期任务 ctx.jobs.every(N).do(fn)。无 Kernel 时返回 None。"""
        kernel = getattr(self._manager, "kernel", None)
        if kernel is not None:
            return getattr(kernel, "jobs", None)
        return None

    @property
    def session(self):
        """SessionLog：append-only 会话日志。无 Kernel 时返回 None。"""
        kernel = getattr(self._manager, "kernel", None)
        if kernel is not None:
            return getattr(kernel, "session_log", None)
        return None

    @property
    def config(self) -> ConfigView:
        """本插件专属配置（profile.yaml → plugin_config[plugin_name]）。未配置时返回空 ConfigView。"""
        kernel = getattr(self._manager, "kernel", None)
        if kernel is None:
            return ConfigView({})
        try:
            # profile 结果（dict 格式）.plugin_config[<本插件名>]
            profile = kernel.profile  # 会触发 resolve，dict 格式
            cfg = (profile.get("plugin_config") or {}).get(self._plugin_name) or {}
            return ConfigView(cfg)
        except Exception:
            return ConfigView({})

    def register_subcommand(self, name: str, handler, help_text: str = "") -> None:
        """注册 `!name` 控制台子命令。handler 签名: async fn(text: str) -> tuple[handled:bool, result:str]。

        暂存到 PluginManager._subcommands dict（无 manager 时 console.warn 并丢弃）；
        实际分发由 Application._main_loop 在命令解析阶段统一读取 manager._subcommands。
        """
        if not isinstance(name, str) or not name:
            raise ValueError("子命令名必须是非空字符串")
        mgr = self._manager
        if not hasattr(mgr, "_subcommands"):
            try:
                mgr._subcommands = {}
            except Exception:
                console.warn(f"[插件:{self._plugin_name}] 无法注册子命令 !{name}（管理器不支持）")
                return
        if name in mgr._subcommands:
            console.warn(f"[插件:{self._plugin_name}] 子命令 !{name} 已存在，覆盖注册")
        mgr._subcommands[name] = {
            "handler": handler,
            "help": help_text,
            "plugin": self._plugin_name,
        }
        self.log("ok", f"已注册子命令 !{name}" + (f"（{help_text}）" if help_text else ""))

    # ======================================================================
    # Sub-agent 委派（优化 7-B）：插件把长任务交给后台 sub-agent，
    # 完成后回调。AGENT_DELEGATE_BACKEND 开启时走 SQLite 持久化队列
    # （重启不丢、自动重试）；关闭时回退进程内 asyncio 后台任务
    # （重启即丢、行为与现状一致）。回调签名：fn(result: str | None)
    # 或 async fn(result: str | None)，超时或失败时 result 为 None。
    # ======================================================================

    async def delegate(self, task: str, *,
                       callback: Optional[Callable] = None,
                       timeout: int = 300) -> Optional[int]:
        """把任务委派给后台 sub-agent（fire-and-forget），完成时回调。

        返回值：
        - 持久化路径：返回 job_id（int，可在 delegation.db 追踪状态）
        - 进程内路径：返回 id(task) 作为软追踪号（仅本进程内有效）
        - 任务为空或异常：返回 None
        """
        if not task or not task.strip():
            return None
        # 1) 优先走持久化队列（AGENT_DELEGATE_BACKEND 开启时）
        try:
            from ev.agent.async_delegation import (
                delegate_backend_enabled, get_delegation_queue,
            )
            if delegate_backend_enabled():
                job_id = get_delegation_queue().enqueue(task)
                if job_id is not None:
                    self.log("ok", f"已后台入队委派任务 #{job_id}")
                    return job_id
        except Exception as e:
            self.log("dim", f"持久化队列不可用，回退进程内委派：{e}")
        # 2) 进程内 fire-and-forget：复用 run_task（自管 agent 生命周期）
        from ev.agent import run_task

        async def _run() -> None:
            try:
                result = await asyncio.wait_for(run_task(task), timeout=timeout)
            except asyncio.TimeoutError:
                self.log("warn", f"委派任务超时（{timeout}s）：{task[:80]}")
                result = None
            except Exception as e:
                self.log("error",
                         f"委派任务失败：{type(e).__name__}: {e}")
                result = None
            if callback is None:
                return
            try:
                ret = callback(result)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception as e:
                self.log("warn", f"委派回调异常：{type(e).__name__}: {e}")

        asyncio.create_task(_run())
        return id(task)

    async def delegate_parallel(self, tasks: list, *,
                                callback: Optional[Callable] = None,
                                timeout: int = 300) -> list:
        """并行委派多个独立子任务；返回每个任务的追踪 id 列表（顺序与入参一致）。"""
        if not isinstance(tasks, list):
            return []
        return [await self.delegate(t, callback=callback, timeout=timeout)
                for t in tasks if isinstance(t, str) and t.strip()]
