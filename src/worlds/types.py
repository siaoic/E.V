"""世界事件与世界描述符的公共类型。

World 框架的基座部分位于主程序（``src/worlds/``），各具体世界由插件实现
（例如 ``plugins/world-bilibili``）：插件通过 ``world.register`` /
``world.event`` / ``world.state`` 能力向本模块上报，本模块通过插件运行时的
``invoke_api`` 回呼世界的 ``world_poll`` / ``world_observe`` API。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class WorldEventTrigger(str, Enum):
    """世界事件的投递语义。

    语义对齐 Cortico 的事件优先级设计：
    - PREEMPT：急事，打断当前模型调用并立即投递；
    - FLUSH：立即投递并绕过频率门限；
    - DEBOUNCE：参与正常合批，受频率阈值门控；
    - PIGGYBACK：只入缓存，不主动调度，随其他批次投递。
    """

    PREEMPT = "preempt"
    FLUSH = "flush"
    DEBOUNCE = "debounce"
    PIGGYBACK = "piggyback"


@dataclass(slots=True)
class WorldEvent:
    """一条来自世界的事件。

    Attributes:
        world_name: 世界的机器名，例如 ``minecraft``。
        event_type: 世界自定义的事件类型，例如 ``damage``。
        text: 已渲染的中文事件文本，直接进入 AI 上下文。
        trigger: 投递语义。
        delay_seconds: 延迟投递秒数，用于防「先知穿帮」。
        payload: 世界自定义的结构化附加信息。
    """

    world_name: str
    event_type: str
    text: str
    trigger: WorldEventTrigger = WorldEventTrigger.DEBOUNCE
    delay_seconds: float = 0.0
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorldDescriptor:
    """一个已注册世界的元信息。

    Attributes:
        name: 世界的机器名，全局唯一，也是工具名与 API 寻址的依据。
        plugin_id: 提供该世界的插件 ID，用于插件卸载时批量注销。
        display_name: 中文显示名，用于事件文本前缀与状态快照标题。
        env_prompt: 静态世界说明，随世界清单一起注入一次。
        polls_changes: 为 True 时由框架按 ``change_poll_seconds`` 轮询其 ``world_poll`` API。
        supports_delayed: 为 True 时世界能重建「N 秒前的状态」并实现了可选的
            ``world_observe_delayed`` API；只有同时出现在 ``worlds.delayed_sources``
            里的世界才会被注入延迟视图（防「先知穿帮」）。
    """

    name: str
    plugin_id: str
    display_name: str
    env_prompt: str = ""
    polls_changes: bool = False
    supports_delayed: bool = False
