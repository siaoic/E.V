"""世界管理器：注册表 + 状态缓存 + 事件总线的组合体。

这是 World 框架基座对外的唯一门面，各世界的插件通过主程序能力
（``world.register`` / ``world.event`` / ``world.state``）与它交互；
planner 请求时由它决定要注入哪些内容。

与插件版基座的关键差异：事件与状态注入不再走「插件 → maisaka.context.append
+ proactive.trigger」这条通用通道，而是直接落到主程序的
``MaisakaHeartFlowChatting.register_world_event()``——这样世界事件既不会污染
外部消息统计，又能复用 runtime 自己的合批、打断与频率门控逻辑。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.common.logger import get_logger
from src.config.config import global_config

from .bus import WorldEventBus
from .registry import WorldRegistry
from .state_cache import WorldStateCache
from .types import WorldDescriptor, WorldEvent, WorldEventTrigger

if TYPE_CHECKING:
    from src.maisaka.runtime import MaisakaHeartFlowChatting

logger = get_logger("worlds")

# 世界插件按约定需要实现的公开 API 名（由框架通过插件运行时回呼）。
WORLD_POLL_API = "world_poll"
WORLD_OBSERVE_API = "world_observe"
# 可选 API：延迟视图（「观众此刻看到的画面」，防「先知穿帮」），
# 只由声明 supports_delayed 的世界实现
WORLD_OBSERVE_DELAYED_API = "world_observe_delayed"


class WorldManager:
    """框架基座的核心对象。"""

    def __init__(self) -> None:
        """按当前全局配置构建世界管理器。

        节流窗口、状态新鲜度、轮询周期都以回调方式读取 ``global_config.worlds``，
        因此配置热重载后无需重建管理器即可生效。
        """

        self._registry = WorldRegistry()
        self._state_cache = WorldStateCache(stale_after_seconds=self._stale_after_seconds)
        self._bus = WorldEventBus(
            deliver=self._deliver_event,
            throttle_seconds=self._event_throttle_seconds,
            logger=logger,
        )

        # 绑定的直播会话：世界事件与注入都发往它。
        self._runtime: Optional["MaisakaHeartFlowChatting"] = None
        self._bound_session_id: str = ""

        # 世界清单版本号：注册/注销时自增，用于「清单变了才重新注入」。
        self._env_revision = 0
        self._injected_env_revision = -1
        self._poll_task: Optional[asyncio.Task[None]] = None
        # 已警告过「声明了延迟却没推送延迟快照」的世界（每世界只警告一次）
        self._warned_missing_delayed: set[str] = set()

    @property
    def registry(self) -> WorldRegistry:
        """世界注册表（只读用途，注册/注销请走管理器方法以同步缓存与清单版本）。"""

        return self._registry

    @property
    def bound_session_id(self) -> str:
        """当前绑定的直播会话 ID；未绑定时为空串。"""

        return self._bound_session_id

    @property
    def pending_event_count(self) -> int:
        """尚未到期的事件数量。"""

        return self._bus.pending_count

    @property
    def event_history(self) -> List[Dict[str, object]]:
        """最近世界事件记录（含被节流丢弃的），供 WebUI 展示。"""

        return self._bus.history

    # ------------------------------------------------------------------ 会话绑定

    async def bind_session(self, session_id: str, runtime: "MaisakaHeartFlowChatting") -> bool:
        """尝试把世界框架绑定到刚创建的会话运行时。

        只有「直播聊天流」才会被绑定：显式配置了 ``worlds.stream_id`` 时按其匹配，
        否则按 ``worlds.live_platform`` 匹配该会话的平台。

        Args:
            session_id: 会话 ID。
            runtime: 该会话的 Maisaka 运行时实例。

        Returns:
            bool: 本次是否绑定成功。

        Raises:
            RuntimeError: 已绑定到另一条会话时抛出——世界事件只能有一个投递目标，
                静默改绑会让先前的会话收不到事件。
        """

        settings = global_config.worlds
        if not settings.enabled:
            return False

        if not self._session_matches_config(session_id, runtime):
            return False

        if self._bound_session_id and self._bound_session_id != session_id:
            raise RuntimeError(
                f"世界框架已绑定会话 {self._bound_session_id}，"
                f"无法再绑定 {session_id}：请把 worlds.stream_id 显式配置为直播聊天流 ID"
            )

        self._runtime = runtime
        self._bound_session_id = session_id
        self.start()
        logger.info(f"世界框架已绑定直播会话 {session_id}，当前已注册世界 {len(self._registry)} 个")
        return True

    def unbind_session(self, session_id: str) -> None:
        """会话运行时报废时解除绑定；世界注册表保持不变。"""

        if self._bound_session_id != session_id:
            return

        self._runtime = None
        self._bound_session_id = ""
        self.stop_bus()
        logger.info(f"世界框架已解除与直播会话 {session_id} 的绑定")

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动事件总线与变更轮询。"""

        self._bus.start()
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("世界变更轮询已启动")

    def stop_bus(self) -> None:
        """停止变更轮询并丢弃未到期的世界事件。"""

        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            poll_task.cancel()

    async def stop(self) -> None:
        """停止变更轮询与事件总线，未到期事件一并丢弃。"""

        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        await self._bus.stop()

    # ------------------------------------------------------------------ 世界注册

    def register_world(self, descriptor: WorldDescriptor) -> None:
        """注册一个世界。

        Args:
            descriptor: 世界描述符。

        Raises:
            ValueError: 世界机器名已被占用时抛出。
        """

        self._registry.register(descriptor)
        self._env_revision += 1
        self._warned_missing_delayed.discard(descriptor.name)
        logger.info(
            f"已注册世界: name={descriptor.name} display_name={descriptor.display_name} "
            f"plugin_id={descriptor.plugin_id} polls_changes={descriptor.polls_changes} "
            f"supports_delayed={descriptor.supports_delayed}"
        )

    def unregister_world(self, name: str) -> bool:
        """注销指定世界，同时丢弃其状态缓存。"""

        descriptor = self._registry.unregister(name)
        if descriptor is None:
            return False
        self._state_cache.drop(name)
        self._env_revision += 1
        logger.info(f"已注销世界: name={name} plugin_id={descriptor.plugin_id}")
        return True

    def unregister_plugin(self, plugin_id: str) -> List[str]:
        """注销某个插件注册的全部世界。"""

        names = self._registry.unregister_plugin(plugin_id)
        for name in names:
            self._state_cache.drop(name)
        if names:
            self._env_revision += 1
            logger.info(f"已注销插件 {plugin_id} 的全部世界: {names}")
        return names

    # ------------------------------------------------------------------ 事件与状态

    def publish_event(self, event: WorldEvent) -> None:
        """把一条世界事件交给事件总线排队投递。"""

        self._bus.publish(event)

    def update_state(self, world_name: str, snapshot: str, *, delayed: bool = False) -> bool:
        """写入世界状态快照。

        Args:
            world_name: 世界机器名。
            snapshot: 已渲染的中文状态文本。
            delayed: 写入延迟视图（「观众此刻看到的画面」）时为 True。

        Returns:
            bool: 快照内容相对旧值发生变化时为 ``True``。
        """

        return self._state_cache.update(world_name, snapshot, now=time.monotonic(), delayed=delayed)

    def delayed_seconds_for(self, world_name: str) -> Optional[float]:
        """取某世界的防穿帮延迟秒数；不启用延迟时返回 ``None``。

        只有「声明了 ``supports_delayed`` 且配置在 ``worlds.delayed_sources``
        里」的世界才算启用延迟视图。
        """

        descriptor = self._registry.get(world_name)
        if descriptor is None or not descriptor.supports_delayed:
            return None
        if world_name not in set(global_config.worlds.delayed_sources or []):
            return None
        return float(global_config.worlds.delayed_seconds)

    def state_views(self, world_name: str) -> Dict[str, str]:
        """取某世界当前缓存的双视图文本（只读，供 WebUI 等展示用）。"""

        return {
            "realtime": self._state_cache.snapshot_of(world_name),
            "delayed": self._state_cache.delayed_snapshot_of(world_name),
        }

    async def observe(self, world_name: str) -> str:
        """向世界插件拉取一份即时完整状态。

        Args:
            world_name: 世界机器名。

        Returns:
            str: 该世界的即时状态文本。

        Raises:
            KeyError: 世界未注册时抛出。
            RuntimeError: 世界插件未实现 ``world_observe`` 或调用失败时抛出。
        """

        descriptor = self._registry.require(world_name)
        payload = await self._invoke_world_api(descriptor, WORLD_OBSERVE_API)
        snapshot = str(payload.get("snapshot") or "").strip()
        self._state_cache.refresh(world_name, snapshot, now=time.monotonic())
        return snapshot

    async def observe_delayed(self, world_name: str) -> str:
        """向世界插件拉取一份延迟视图（观众此刻看到的画面）。

        Args:
            world_name: 世界机器名。

        Returns:
            str: 该世界的延迟视图文本；世界未声明延迟能力时返回空串。

        Raises:
            KeyError: 世界未注册时抛出。
            RuntimeError: 世界插件未实现 ``world_observe_delayed`` 或调用失败时抛出。
        """

        descriptor = self._registry.require(world_name)
        if not descriptor.supports_delayed:
            return ""
        payload = await self._invoke_world_api(descriptor, WORLD_OBSERVE_DELAYED_API)
        snapshot = str(payload.get("snapshot") or "").strip()
        if snapshot:
            self._state_cache.update(world_name, snapshot, now=time.monotonic(), delayed=True)
        return snapshot

    # ------------------------------------------------------------------ 注入文本

    def build_env_injection(self) -> str:
        """返回静态世界清单文本；清单未变化时返回空串。

        对应计划里 ``{worlds_section}`` 的角色：只在世界集合发生变化后注入一次，
        避免每次请求都重复消耗 token。
        """

        if self._injected_env_revision == self._env_revision:
            return ""

        descriptors = self._registry.descriptors()
        if not descriptors:
            self._injected_env_revision = self._env_revision
            return ""

        lines = ["<world_env>", "你正在直播，同时接入以下世界，可以用 world_observe 工具随时拉取任一世界的即时状态："]
        for descriptor in descriptors:
            env_prompt = descriptor.env_prompt.strip()
            suffix = f"：{env_prompt}" if env_prompt else ""
            lines.append(f"- {descriptor.display_name}（world_name={descriptor.name}）{suffix}")
        lines.append("</world_env>")

        self._injected_env_revision = self._env_revision
        return "\n".join(lines)

    def build_state_injection(self) -> str:
        """返回本次请求需要注入的世界状态文本；无内容时返回空串。

        是否注入由配置 ``worlds.inject_state`` 与 :class:`WorldStateCache` 的
        ``dirty`` / ``stale`` 规则共同决定。声明了延迟视图的世界会额外给出
        「观众此刻看到的画面」一段，两个视图各自独立计时。
        """

        if not global_config.worlds.inject_state:
            return ""

        world_names = self._registry.names()
        realtime_blocks = dict(self._state_cache.build_blocks(world_names, now=time.monotonic()))
        delayed_blocks = dict(self._state_cache.build_blocks(world_names, now=time.monotonic(), delayed=True))
        if not realtime_blocks and not delayed_blocks:
            return ""

        lines = ["<world_state>"]
        for world_name in world_names:
            realtime = realtime_blocks.get(world_name)
            delayed = delayed_blocks.get(world_name)
            if not realtime and not delayed:
                continue
            descriptor = self._registry.get(world_name)
            display_name = descriptor.display_name if descriptor is not None else world_name
            lines.append(f"【{display_name}】")
            if delayed:
                # 双视图：实时与「观众此刻看到的画面」并列（§4-D9 的扩展格式）
                if realtime:
                    lines.append(f"【实时】{realtime}")
                lines.append(f"【观众此刻看到的画面】{delayed}")
            elif realtime:
                # 无延迟视图的世界维持现状格式，注入内容与旧版逐字一致
                lines.append(realtime)
            if (
                realtime
                and not delayed
                and descriptor is not None
                and descriptor.supports_delayed
                and self.delayed_seconds_for(world_name) is not None
                and not self._state_cache.delayed_snapshot_of(world_name)
                and world_name not in self._warned_missing_delayed
            ):
                # 世界声明了延迟能力却从未推送过延迟快照：契约违约要暴露，不冒充
                self._warned_missing_delayed.add(world_name)
                logger.warning(
                    f"世界 {world_name} 声明了 supports_delayed 但尚未推送延迟视图快照，"
                    "请检查插件是否实现了 world_observe_delayed 或经 world.state(view=delayed) 上报"
                )
        lines.append("</world_state>")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 内部实现

    def _session_matches_config(self, session_id: str, runtime: "MaisakaHeartFlowChatting") -> bool:
        """判断某个会话是否为配置指定的直播聊天流。"""

        settings = global_config.worlds
        configured_stream_id = settings.stream_id.strip()
        if configured_stream_id:
            return configured_stream_id == session_id
        return runtime.chat_stream.platform == settings.live_platform

    @staticmethod
    def _stale_after_seconds() -> float:
        """读取「无变化时两次状态注入之间的最长间隔」。"""

        return float(global_config.worlds.stale_after_seconds)

    @staticmethod
    def _event_throttle_seconds() -> float:
        """读取「同一世界两次普通事件投递之间的最小间隔」。"""

        return float(global_config.worlds.event_throttle_seconds)

    async def _deliver_event(self, event: WorldEvent) -> None:
        """把到期事件交给绑定的直播会话。

        投递语义由 runtime 负责落地：PREEMPT 打断当前调用、FLUSH 绕过频率门限、
        DEBOUNCE 参与正常合批、PIGGYBACK 只落缓存。

        Raises:
            RuntimeError: 尚未绑定直播会话时抛出——事件一旦丢弃就无法补投，
                必须暴露而不是静默吞咽。
        """

        runtime = self._runtime
        if runtime is None:
            raise RuntimeError(
                f"世界事件到达时没有绑定直播会话，无法投递: "
                f"world={event.world_name} trigger={event.trigger.value}"
            )

        descriptor = self._registry.get(event.world_name)
        display_name = descriptor.display_name if descriptor is not None else event.world_name
        await runtime.register_world_event(
            source_name=f"世界·{display_name}",
            text=event.text,
            trigger=event.trigger.value,
        )

    async def _invoke_world_api(self, descriptor: WorldDescriptor, api_name: str) -> Dict[str, Any]:
        """调用世界插件公开的约定 API 并取出 ``result`` 载荷。

        Raises:
            RuntimeError: 调用失败或返回结构不合法时抛出——世界插件没实现约定 API
                属于契约违约，必须完整暴露而不是静默跳过。
        """

        from src.plugin_runtime.integration import get_plugin_runtime_manager

        plugin_runtime_manager = get_plugin_runtime_manager()
        for supervisor in plugin_runtime_manager.supervisors:
            entries = supervisor.api_registry.get_apis(
                plugin_id=descriptor.plugin_id,
                name=api_name,
                enabled_only=True,
            )
            if not entries:
                continue
            if len(entries) > 1:
                raise RuntimeError(
                    f"世界 API 存在多个可用版本，无法确定目标: "
                    f"world={descriptor.name} plugin_id={descriptor.plugin_id} api={api_name}"
                )

            entry = entries[0]
            invoke_args: Dict[str, Any] = {}
            if entry.dynamic:
                invoke_args.setdefault("__maibot_api_name__", entry.name)
                invoke_args.setdefault("__maibot_api_full_name__", entry.full_name)
                invoke_args.setdefault("__maibot_api_version__", entry.version)

            response = await supervisor.invoke_api(
                plugin_id=descriptor.plugin_id,
                component_name=entry.handler_name,
                args=invoke_args,
            )
            if response.error:
                raise RuntimeError(
                    f"调用世界 API 失败: world={descriptor.name} api={api_name} "
                    f"error={response.error.get('message', '插件运行时返回错误')}"
                )

            payload = response.payload if isinstance(response.payload, dict) else {}
            if not bool(payload.get("success", False)):
                raise RuntimeError(
                    f"调用世界 API 失败: world={descriptor.name} api={api_name} "
                    f"error={payload.get('result')}"
                )

            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"世界 API 返回结构不合法: world={descriptor.name} api={api_name} payload={result!r}"
                )
            return result

        raise RuntimeError(
            f"未找到世界插件公开的 API: world={descriptor.name} "
            f"plugin_id={descriptor.plugin_id} api={api_name}"
        )

    async def _poll_loop(self) -> None:
        """按 ``worlds.change_poll_seconds`` 轮询声明了 ``polls_changes`` 的世界。"""

        while True:
            interval = float(global_config.worlds.change_poll_seconds)
            await asyncio.sleep(interval if interval > 0 else 1.0)
            if interval <= 0:
                continue
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单个世界轮询失败不应终止整条轮询链
                logger.exception("世界变更轮询失败")

    async def _poll_once(self) -> None:
        """对所有轮询型世界各做一次变更检测。"""

        for descriptor in self._registry.descriptors():
            if not descriptor.polls_changes:
                continue
            payload = await self._invoke_world_api(descriptor, WORLD_POLL_API)
            if not bool(payload.get("changed")):
                continue
            self.update_state(descriptor.name, str(payload.get("snapshot") or ""))
            # 延迟视图随实时视图一起刷新：世界在 world_observe_delayed 里
            # 返回「观众此刻看到的画面」，做不到的世界不会声明 supports_delayed
            if self.delayed_seconds_for(descriptor.name) is not None:
                try:
                    await self.observe_delayed(descriptor.name)
                except Exception:  # noqa: BLE001 - 延迟视图失败不应影响实时视图
                    logger.exception(
                        f"拉取世界延迟视图失败: world={descriptor.name} api={WORLD_OBSERVE_DELAYED_API}"
                    )


_world_manager: Optional[WorldManager] = None


def get_world_manager() -> WorldManager:
    """获取世界管理器单例（首次调用时按当前配置构建）。"""

    global _world_manager
    if _world_manager is None:
        _world_manager = WorldManager()
    return _world_manager
