"""Minecraft 世界插件（``maibot-live.world-minecraft``）。

链路：mineflayer（mc-node 子进程）→ stdio NDJSON 协议 → 本插件 →
``world.state`` 上报快照 + ``world.event`` 上报事件。

事件显著性（在 Node 侧判定，本插件只按标志选投递语义）：
- 显著（掉血 / 死亡 / 敌对生物出现或增多）：``preempt``，立即打断；
  掉血与死亡是独立协议帧，同样 preempt；
- 一般（位置移动 / 背包变化等）：``debounce``，参与合批与频率门控；
- 玩家进出与游戏内聊天：``flush``，立即唤醒。

动作工具「提交即返回」：工具体只把动作塞进内部队列并立刻返回，
worker 协程串行发给 mc-node，真实结果以 ``task_done`` 事件回传，
不阻塞聊天。mc-node 掉线时工具明确报错，不静默吞掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from maibot_sdk import API, Field as PluginField, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.components import ToolParameterInfo
from maibot_sdk.types import ToolParamType
from pydantic import Field

from .bridge import MinecraftBridge
from . import renderer

# 主程序世界框架能力名（基座实现在 src/worlds/，插件通过能力通道调用）。
WORLD_REGISTER_CAPABILITY = "world.register"
WORLD_UNREGISTER_CAPABILITY = "world.unregister"
WORLD_EVENT_CAPABILITY = "world.event"
WORLD_STATE_CAPABILITY = "world.state"

# 主程序配置读取能力（用于读取 OBS 延迟秒数与延迟来源清单）。
CONFIG_GET_CAPABILITY = "config.get"

DEFAULT_ENV_PROMPT = (
    "你正在 Minecraft 服务器里玩游戏，你的真实处境（位置 / 血量 / 饥饿 / 周围生物 / 背包）"
    "会作为世界状态注入，掉血、死亡、敌对生物靠近、玩家进出会立刻以世界事件通知你；"
    "你可以用 minecraft_do / minecraft_scout / minecraft_check 对游戏下动作，"
    "动作提交后真实结果会以世界事件回传。"
)

# 状态历史环形缓冲长度：1 秒节拍下足够覆盖 OBS 延迟（worlds.delayed_seconds，默认 8s）
STATE_HISTORY_LIMIT = 120

# minecraft_do 支持的动词表（渲染进工具描述，供模型选择）
ACTION_VERBS = (
    "goto x y z | goto player 玩家名 | follow 玩家名 | stop | dig 方块名 | "
    "attack 生物名(留空打最近敌对) | say 内容 | check | scout"
)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件通用配置。"""

    enabled: bool = PluginField(default=True, description="是否启用插件")
    config_version: str = PluginField(default="0.1.0", description="配置版本，由宿主用于配置迁移")


class MinecraftConfig(PluginConfigBase):
    """Minecraft 服务器连接与桥接参数。"""

    host: str = PluginField(default="127.0.0.1", description="MC 服务器地址")
    port: int = PluginField(default=25565, ge=1, le=65535, description="MC 服务器端口")
    username: str = PluginField(default="MaiBot", description="进服用户名（离线模式账号）")
    version: str = PluginField(default="", description="MC 协议版本；留空自动协商")
    threat_radius: float = PluginField(
        default=8.0, ge=2.0, le=32.0, description="敌对生物警戒半径（格），进入该范围按显著事件立即上报"
    )
    node_executable: str = PluginField(
        default="", description="Node 可执行文件路径；留空用 PATH 上的 node（需要 Node ≥ 18）"
    )
    ready_timeout_seconds: float = PluginField(
        default=30.0, ge=5.0, le=300.0, description="等待进服完成的超时（秒），超时插件拒绝加载"
    )
    name: str = PluginField(default="minecraft", description="世界机器名，全局唯一")
    display_name: str = PluginField(default="Minecraft 世界", description="世界显示名，出现在注入文本与事件前缀中")
    env_prompt: str = PluginField(
        default=DEFAULT_ENV_PROMPT, description="注入给模型的静态世界说明，只在世界集合变化时注入一次"
    )


class WorldMinecraftConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    minecraft: MinecraftConfig = Field(default_factory=MinecraftConfig)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


class WorldMinecraftPlugin(MaiBotPlugin):
    """Minecraft 世界插件入口。"""

    config_model = WorldMinecraftConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: Optional[MinecraftBridge] = None
        self._connected = False
        self._registered_name = ""
        self._state: Dict[str, Any] = {}
        # 状态历史环形缓冲（monotonic 时刻 → 状态字典）：延迟视图从这里重建「N 秒前的状态」
        self._state_history: Deque[Tuple[float, Dict[str, Any]]] = deque(maxlen=STATE_HISTORY_LIMIT)
        self._delayed_seconds: float = 0.0  # >0 表示本世界在 delayed_sources 里，需要推送延迟视图
        self._action_queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._action_counter = itertools.count(1)

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """启动 mc-node 桥 → 等待进服 → 注册世界 → 启动动作 worker。

        Raises:
            RuntimeError: Node 环境不满足、依赖未安装或进服超时时抛出，
                让加载失败并把原因暴露出来，而不是注册一个永远沉默的世界。
        """

        settings = self.config.minecraft
        node_dir = Path(__file__).resolve().parent / "node"

        ready_event = asyncio.Event()
        last_error: List[str] = []

        def on_frame(frame: Dict[str, Any]) -> None:
            if frame.get("type") == "hello":
                ready_event.set()
            elif frame.get("type") == "error":
                last_error.append(str(frame.get("message") or ""))
            self._dispatch_frame(frame)

        def on_closed(reason: str) -> None:
            self._connected = False
            self.ctx.logger.error(f"mc-node 断开: {reason}")
            # 断线是直播观众能感知的事实：立即作为世界事件暴露
            if self._registered_name:
                self._report_event_nowait(
                    event_type="disconnect",
                    text=f"与 Minecraft 服务器的连接已断开：{reason}",
                    trigger="flush",
                )

        bridge = MinecraftBridge(
            node_dir=node_dir,
            node_executable=settings.node_executable,
            host=settings.host,
            port=settings.port,
            username=settings.username,
            version=settings.version,
            threat_radius=settings.threat_radius,
            on_frame=on_frame,
            on_closed=on_closed,
            logger=self.ctx.logger,
        )
        await bridge.start()
        self._bridge = bridge

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=settings.ready_timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self._stop_bridge()
            raise RuntimeError(
                f"等待进入 Minecraft 服务器超时（{settings.ready_timeout_seconds:g}s，"
                f"{settings.host}:{settings.port}）；"
                + ("；".join(last_error) if last_error else "服务器无响应或地址/版本配置错误")
            ) from exc
        except Exception:
            await self._stop_bridge()
            raise
        if not bridge.alive:
            detail = "；".join(last_error) if last_error else "未知原因"
            raise RuntimeError(f"mc-node 在进服前退出：{detail}")

        self._connected = True
        await self._register_to_core()
        await self._load_delayed_settings()
        self._action_queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._action_worker())
        self.ctx.logger.info(
            f"Minecraft 世界已接入框架：{settings.host}:{settings.port} as {settings.username}"
        )

    async def on_unload(self) -> None:
        """停掉动作 worker 与 mc-node，并从框架基座注销本世界。"""

        await self._stop_worker()
        await self._stop_bridge()
        await self._unregister_from_core()
        self.ctx.logger.info("Minecraft 世界已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置变更后整体重连（服务器地址是构造期参数）。"""

        del config_data
        await self.on_unload()
        await self.on_load()
        self.ctx.logger.info(f"Minecraft 世界配置已更新（scope={scope} version={version}）")

    # ------------------------------------------------------------------ 框架约定 API

    @API(
        "world_observe",
        description="按需拉取 Minecraft 世界的即时完整状态（位置/血量/周围生物/背包）。",
        version="1",
        public=True,
    )
    async def handle_world_observe(self, **_kwargs: Any) -> Dict[str, Any]:
        """供 world_observe 工具按需拉取。"""

        return {"snapshot": self._render_snapshot()}

    @API(
        "world_observe_delayed",
        description="拉取「观众此刻看到的画面」：从状态历史重建 OBS 延迟秒数前的状态。",
        version="1",
        public=True,
    )
    async def handle_world_observe_delayed(self, **_kwargs: Any) -> Dict[str, Any]:
        """供框架在延迟视图启用时按需拉取。"""

        return {"snapshot": self._render_delayed_snapshot()}

    # ------------------------------------------------------------------ 动作工具

    @Tool(
        "minecraft_do",
        description=(
            "在 Minecraft 里执行一个动作，提交即返回，真实结果稍后以世界事件回传。"
            f"动作格式：{ACTION_VERBS}。"
        ),
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description=f"动作字符串，动词表：{ACTION_VERBS}",
            ),
        ],
    )
    async def handle_minecraft_do(self, action: str, **_kwargs: Any) -> Dict[str, Any]:
        """解析动作字符串并入队；不等待真实执行。"""

        verb_args = str(action or "").strip().split()
        if not verb_args:
            return {"success": False, "message": "动作不能为空；格式示例：goto 100 64 -200"}
        return self._submit_action(verb_args[0].lower(), verb_args[1:])

    @Tool(
        "minecraft_scout",
        description="观察 Minecraft 周围环境（6 格内方块与生物），提交即返回，结果稍后以世界事件回传。",
        parameters=[],
    )
    async def handle_minecraft_scout(self, **_kwargs: Any) -> Dict[str, Any]:
        return self._submit_action("scout", [])

    @Tool(
        "minecraft_check",
        description="查询自己的 Minecraft 状态与背包，提交即返回，结果稍后以世界事件回传。",
        parameters=[],
    )
    async def handle_minecraft_check(self, **_kwargs: Any) -> Dict[str, Any]:
        return self._submit_action("check", [])

    # ------------------------------------------------------------------ 协议分派

    def _dispatch_frame(self, frame: Dict[str, Any]) -> None:
        """按帧类型分派 mc-node 的产出。"""

        frame_type = frame.get("type")
        if frame_type == "state":
            self._record_state(frame.get("state"))
            self._publish_state()
        elif frame_type == "state_changed":
            self._record_state(frame.get("state"))
            self._publish_state()
            self._report_state_changed(frame)
        elif frame_type in ("damage", "death", "chat", "player_join", "player_leave"):
            self._report_protocol_event(frame)
        elif frame_type == "task_done":
            self._report_task_done(frame)
        elif frame_type == "error":
            self.ctx.logger.error(f"mc-node 上报错误: {frame.get('message')}")

    def _report_state_changed(self, frame: Dict[str, Any]) -> None:
        """按显著性把状态变化上报为世界事件。"""

        state = self._state
        if frame.get("significant"):
            self._report_event_nowait(
                event_type="threat",
                text=renderer.render_significant(state),
                trigger="preempt",
            )
        else:
            changed = [str(field) for field in (frame.get("changed") or [])]
            self._report_event_nowait(
                event_type="state_changed",
                text=renderer.render_changes(changed, state),
                trigger="debounce",
            )

    def _report_protocol_event(self, frame: Dict[str, Any]) -> None:
        """把离散事件帧按各自语义上报。"""

        frame_type = frame.get("type")
        # 玩家进出与游戏内聊天直接唤醒一轮；掉血/死亡必须立即打断
        trigger = "flush" if frame_type in ("chat", "player_join", "player_leave") else "preempt"
        self._report_event_nowait(
            event_type=str(frame_type),
            text=renderer.render_event(frame),
            trigger=trigger,
        )

    def _report_task_done(self, frame: Dict[str, Any]) -> None:
        """渲染动作执行结果并回传事件；check/scout 结果同时刷新状态快照。"""

        data = frame.get("data") or {}
        state = data.get("state")
        if isinstance(state, dict):
            self._record_state(state)
            self._publish_state()

        text = renderer.render_task_done(frame)
        scout_text = renderer.render_scout(data)
        if scout_text and frame.get("data", {}).get("scout"):
            text = f"{text}{scout_text}"
        self._report_event_nowait(event_type="task_done", text=text, trigger="debounce")

    # ------------------------------------------------------------------ 动作队列

    def _submit_action(self, verb: str, args: List[str]) -> Dict[str, Any]:
        """把动作塞进队列并立刻返回（提交即返回语义）。"""

        if self._bridge is None or not self._connected:
            return {
                "success": False,
                "message": "错误：MC 未连接（mc-node 未在运行），动作无法提交",
            }
        queue = self._action_queue
        if queue is None:
            return {"success": False, "message": "错误：动作队列尚未就绪，请稍后重试"}

        action_id = f"act-{next(self._action_counter)}"
        queue.put_nowait({"id": action_id, "verb": verb, "args": args})
        verb_zh = "观察周围" if verb == "scout" else "查询状态" if verb == "check" else verb
        return {"success": True, "message": f"已提交：{verb_zh}（id={action_id}），执行结果稍后以世界事件回传"}

    async def _action_worker(self) -> None:
        """串行消费动作队列，逐条发给 mc-node。

        单条发送失败记录日志并回传失败事件：动作丢了必须让 AI 知道，
        不能让它一直等一个永远不会来的结果。
        """

        queue = self._action_queue
        if queue is None:
            return

        while True:
            frame = await queue.get()
            bridge = self._bridge
            if bridge is None:
                continue
            try:
                await bridge.send({"type": "command", **frame})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败不应终止 worker
                self.ctx.logger.exception(f"发送动作指令失败: {frame!r} error={exc!r}")
                self._report_event_nowait(
                    event_type="task_done",
                    text=f"动作执行失败：无法发送到游戏（{exc!r}）",
                    trigger="debounce",
                )

    # ------------------------------------------------------------------ 状态与事件上报

    def _record_state(self, state: Any) -> None:
        """记录最新状态并写入历史环形缓冲（延迟视图的重建素材）。"""

        if not isinstance(state, dict):
            return
        self._state = dict(state)
        self._state_history.append((time.monotonic(), self._state))

    async def _load_delayed_settings(self) -> None:
        """读主配置判定本世界是否需要推送延迟视图（worlds.delayed_sources）。

        配置读取失败时不启用延迟（记日志），不影响实时视图。
        """

        name = self._registered_name
        try:
            delayed_sources = await self._read_host_config("worlds.delayed_sources", [])
            delayed_seconds = await self._read_host_config("worlds.delayed_seconds", 0)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error(f"读取防穿帮延迟配置失败，本世界不启用延迟视图: {exc!r}")
            self._delayed_seconds = 0.0
            return
        if name in set(delayed_sources or []):
            self._delayed_seconds = max(float(delayed_seconds or 0), 0.0)
            self.ctx.logger.info(f"防穿帮延迟视图已启用: 延迟 {self._delayed_seconds:g}s（OBS 画面延迟对齐）")
        else:
            self._delayed_seconds = 0.0

    def _render_delayed_snapshot(self) -> str:
        """从历史重建「OBS 延迟秒数前」的状态快照。

        状态是变化驱动的：两帧之间没有 state_changed 就意味着状态没变，
        因此取「不晚于 now − delayed_seconds 的最近一份」在语义上就是那
        个时刻的状态。历史为空时如实返回占位文本，不用实时视图冒充。
        """

        target = time.monotonic() - self._delayed_seconds
        chosen: Optional[Dict[str, Any]] = None
        for recorded_at, state in self._state_history:
            if recorded_at <= target:
                chosen = state
            else:
                break
        if chosen is None:
            return "（暂无法重建观众此刻看到的画面：状态历史尚未覆盖到延迟时刻）"
        return renderer.render_state(chosen, connected=self._connected)

    def _publish_state(self) -> None:
        """把最新状态快照经 ``world.state`` 交给基座（内容没变时基座会去重）。

        延迟视图启用时同步推送一份「观众此刻看到的画面」
        （``view="delayed"``）——本世界是事件推送型（polls_changes=false），
        基座的轮询不会代劳，延迟视图必须由本插件自己推。
        """

        name = self._registered_name
        if not name:
            return
        snapshot = self._render_snapshot()
        self._fire_and_forget(self._call_world_state(snapshot))
        if self._delayed_seconds > 0:
            delayed_snapshot = self._render_delayed_snapshot()
            self._fire_and_forget(self._call_world_state(delayed_snapshot, delayed=True))

    async def _call_world_state(self, snapshot: str, *, delayed: bool = False) -> None:
        response = await self.ctx.call_capability(
            WORLD_STATE_CAPABILITY,
            world_name=self._registered_name,
            snapshot=snapshot,
            view="delayed" if delayed else "realtime",
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"上报世界状态失败：{error}")

    def _report_event_nowait(self, *, event_type: str, text: str, trigger: str) -> None:
        """上报一条世界事件；失败记日志不抛出（分派方在回调链里）。"""

        name = self._registered_name
        if not name:
            return
        self._fire_and_forget(self._call_world_event(event_type=event_type, text=text, trigger=trigger))

    async def _call_world_event(self, *, event_type: str, text: str, trigger: str) -> None:
        response = await self.ctx.call_capability(
            WORLD_EVENT_CAPABILITY,
            world_name=self._registered_name,
            event_type=event_type,
            text=text,
            trigger=trigger,
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"上报世界事件失败：{error}")

    def _fire_and_forget(self, coroutine) -> None:
        """在事件循环上跑一个上报协程，失败打完整日志。"""

        async def runner():
            try:
                await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 上报失败不反噬分派链
                self.ctx.logger.exception(f"上报世界框架失败: {exc!r}")

        asyncio.get_running_loop().create_task(runner())

    # ------------------------------------------------------------------ 内部实现

    def _render_snapshot(self) -> str:
        """渲染当前状态快照（未连接时如实说明）。"""

        return renderer.render_state(self._state, connected=self._connected)

    async def _register_to_core(self) -> None:
        """把本世界注册进主程序的世界框架。"""

        settings = self.config.minecraft
        name = settings.name.strip()
        if not name:
            raise RuntimeError("Minecraft 世界配置缺少 minecraft.name")

        response = await self.ctx.call_capability(
            WORLD_REGISTER_CAPABILITY,
            name=name,
            display_name=settings.display_name,
            env_prompt=settings.env_prompt,
            # 状态由 mc-node 主动推，基座轮询不介入
            polls_changes=False,
            # 能从状态历史重建「N 秒前的状态」，声明延迟能力（是否生效由 worlds.delayed_sources 决定）
            supports_delayed=True,
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"向世界框架注册 Minecraft 世界失败：{error}")

        self._registered_name = name

    async def _read_host_config(self, key: str, default):
        """经 ``config.get`` 能力读主程序全局配置的点路径字段。"""

        response = await self.ctx.call_capability(CONFIG_GET_CAPABILITY, key=key, default=default)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"读取主程序配置 {key} 失败：{error}")
        return response.get("value", default)

    async def _unregister_from_core(self) -> None:
        """把本世界从世界框架注销；未注册时直接返回。"""

        registered = self._registered_name
        if not registered:
            return
        self._registered_name = ""

        response = await self.ctx.call_capability(WORLD_UNREGISTER_CAPABILITY, name=registered)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"从世界框架注销 Minecraft 世界失败：world={registered} error={error}")

    async def _stop_bridge(self) -> None:
        """停止 mc-node 子进程。"""

        bridge = self._bridge
        self._bridge = None
        self._connected = False
        if bridge is not None:
            await bridge.stop()

    async def _stop_worker(self) -> None:
        """取消动作 worker 任务。"""

        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_plugin() -> WorldMinecraftPlugin:
    """插件工厂函数。"""

    return WorldMinecraftPlugin()
