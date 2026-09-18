"""插件运行时的世界框架能力。

世界基座位于 ``src.worlds``，各具体世界由插件实现。本组能力是插件与基座之间
唯一的写入口：插件注册/注销世界、投递世界事件、写入状态快照，基座则通过
插件运行时的 ``invoke_api`` 回呼世界的 ``world_poll`` / ``world_observe``。

所有能力都带插件归属校验：插件只能操作自己注册的世界，避免「A 插件冒充 B
世界投递事件」这类越权。
"""

from typing import TYPE_CHECKING, Any, Dict

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.worlds.types import WorldDescriptor

logger = get_logger("plugin_runtime.integration")


class RuntimeWorldCapabilityMixin:
    """插件运行时的世界框架能力混入。"""

    @staticmethod
    def _require_plugin_world(world_name: str, plugin_id: str) -> "WorldDescriptor":
        """校验世界已注册且归属当前插件。

        Args:
            world_name: 世界机器名。
            plugin_id: 调用该能力的插件 ID。

        Returns:
            WorldDescriptor: 通过校验的世界描述符。

        Raises:
            ValueError: 世界未注册，或不属于当前插件时抛出。
        """

        from src.worlds.manager import get_world_manager

        descriptor = get_world_manager().registry.get(world_name)
        if descriptor is None:
            raise ValueError(f"世界未注册: {world_name}")
        if descriptor.plugin_id != plugin_id:
            raise ValueError(
                f"世界 {world_name} 由插件 {descriptor.plugin_id} 注册，当前插件 {plugin_id} 无权操作"
            )
        return descriptor

    async def _cap_world_register(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """注册一个世界。

        Args:
            plugin_id: 调用该能力的插件 ID。
            capability: 当前能力名称。
            args: 能力调用参数，需含 ``name``，可选 ``display_name`` /
                ``env_prompt`` / ``polls_changes`` / ``supports_delayed``。

        Returns:
            Any: 标准化后的能力返回结构。
        """

        del capability

        name = str(args.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "缺少必要参数 name"}
        display_name = str(args.get("display_name") or "").strip() or name

        try:
            from src.worlds.manager import get_world_manager
            from src.worlds.types import WorldDescriptor

            world_manager = get_world_manager()
            world_manager.register_world(
                WorldDescriptor(
                    name=name,
                    plugin_id=plugin_id,
                    display_name=display_name,
                    env_prompt=str(args.get("env_prompt") or "").strip(),
                    polls_changes=bool(args.get("polls_changes", False)),
                    supports_delayed=bool(args.get("supports_delayed", False)),
                )
            )
            return {"success": True, "name": name, "worlds": world_manager.registry.names()}
        except Exception as exc:
            logger.error(f"[cap.world.register] 执行失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}

    async def _cap_world_unregister(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """注销一个世界；不传 ``name`` 时注销该插件注册的全部世界。

        Args:
            plugin_id: 调用该能力的插件 ID。
            capability: 当前能力名称。
            args: 能力调用参数，可选 ``name``。

        Returns:
            Any: 标准化后的能力返回结构。
        """

        del capability

        try:
            from src.worlds.manager import get_world_manager

            world_manager = get_world_manager()
            name = str(args.get("name") or "").strip()
            if not name:
                return {"success": True, "removed": world_manager.unregister_plugin(plugin_id)}

            self._require_plugin_world(name, plugin_id)
            removed = world_manager.unregister_world(name)
            return {"success": True, "removed": [name] if removed else []}
        except Exception as exc:
            logger.error(f"[cap.world.unregister] 执行失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}

    async def _cap_world_event(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """投递一条世界事件，由基座按 ``trigger`` 语义决定如何进入对话。

        Args:
            plugin_id: 调用该能力的插件 ID。
            capability: 当前能力名称。
            args: 能力调用参数，需含 ``world_name`` 与 ``text``，可选
                ``event_type`` / ``trigger`` / ``delay_seconds`` / ``payload``。

        Returns:
            Any: 标准化后的能力返回结构。
        """

        del capability

        from src.worlds.manager import get_world_manager
        from src.worlds.types import WorldEvent, WorldEventTrigger

        world_name = str(args.get("world_name") or args.get("name") or "").strip()
        if not world_name:
            return {"success": False, "error": "缺少必要参数 world_name"}
        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "缺少必要参数 text"}

        try:
            delay_seconds = max(float(args.get("delay_seconds") or 0.0), 0.0)
        except (TypeError, ValueError):
            return {"success": False, "error": f"delay_seconds 取值非法: {args.get('delay_seconds')!r}"}

        try:
            trigger = WorldEventTrigger(str(args.get("trigger") or WorldEventTrigger.DEBOUNCE.value).strip().lower())
        except ValueError:
            allowed = "、".join(item.value for item in WorldEventTrigger)
            return {"success": False, "error": f"trigger 取值非法，可选：{allowed}"}

        try:
            self._require_plugin_world(world_name, plugin_id)
            world_manager = get_world_manager()
            # 防「先知穿帮」：OBS 延迟是部署参数，只应配一处（worlds.delayed_seconds），
            # 由基座对延迟来源的世界强制覆写，世界侧无需感知 OBS 延迟
            delayed_seconds = world_manager.delayed_seconds_for(world_name)
            if delayed_seconds is not None:
                delay_seconds = delayed_seconds
            world_manager.publish_event(
                WorldEvent(
                    world_name=world_name,
                    event_type=str(args.get("event_type") or "").strip(),
                    text=text,
                    trigger=trigger,
                    delay_seconds=delay_seconds,
                    payload=args.get("payload") if isinstance(args.get("payload"), dict) else {},
                )
            )
            return {"success": True, "pending": world_manager.pending_event_count}
        except Exception as exc:
            logger.error(f"[cap.world.event] 执行失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}

    async def _cap_world_state(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """写入世界状态快照，供 Planner 请求按需注入。

        Args:
            plugin_id: 调用该能力的插件 ID。
            capability: 当前能力名称。
            args: 能力调用参数，需含 ``world_name`` 与 ``snapshot``，可选
                ``view``（``"realtime"`` 默认实时视图 / ``"delayed"``
                「观众此刻看到的画面」延迟视图，防「先知穿帮」）。

        Returns:
            Any: 标准化后的能力返回结构，``changed`` 表示快照相对旧值是否变化。
        """

        del capability

        world_name = str(args.get("world_name") or args.get("name") or "").strip()
        if not world_name:
            return {"success": False, "error": "缺少必要参数 world_name"}
        if "snapshot" not in args:
            return {"success": False, "error": "缺少必要参数 snapshot"}
        view = str(args.get("view") or "realtime").strip().lower()
        if view not in ("realtime", "delayed"):
            return {"success": False, "error": f"view 取值非法（realtime / delayed）: {view!r}"}

        try:
            from src.worlds.manager import get_world_manager

            self._require_plugin_world(world_name, plugin_id)
            changed = get_world_manager().update_state(
                world_name,
                str(args.get("snapshot") or "").strip(),
                delayed=view == "delayed",
            )
            return {"success": True, "changed": changed}
        except Exception as exc:
            logger.error(f"[cap.world.state] 执行失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}
