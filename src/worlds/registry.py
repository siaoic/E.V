"""世界注册表：按机器名管理已注册的世界描述符。

世界由各自的插件通过 ``world.register`` 能力注册进来，插件 ID 与世界的归属
关系由此表维护，用于「插件卸载时批量注销其名下全部世界」。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .types import WorldDescriptor


class WorldRegistry:
    """维护世界描述符的注册与查询。"""

    def __init__(self) -> None:
        """初始化空注册表。"""

        self._descriptors: Dict[str, WorldDescriptor] = {}

    def register(self, descriptor: WorldDescriptor) -> None:
        """注册一个世界。

        Args:
            descriptor: 世界描述符。

        Raises:
            ValueError: 机器名已被占用时抛出——机器名是工具名与 API 寻址依据，
                重复注册会静默改变语义，因此这里直接报错而不覆盖。
        """

        existing = self._descriptors.get(descriptor.name)
        if existing is not None:
            raise ValueError(
                f"世界机器名重复: {descriptor.name!r} "
                f"（已由插件 {existing.plugin_id!r} 注册，插件 {descriptor.plugin_id!r} 重复注册）"
            )
        self._descriptors[descriptor.name] = descriptor

    def unregister(self, name: str) -> Optional[WorldDescriptor]:
        """注销指定世界，返回被注销的描述符；未注册时返回 ``None``。"""

        return self._descriptors.pop(name, None)

    def unregister_plugin(self, plugin_id: str) -> List[str]:
        """注销某个插件注册的全部世界，返回被注销的机器名列表。"""

        names = [name for name, descriptor in self._descriptors.items() if descriptor.plugin_id == plugin_id]
        for name in names:
            del self._descriptors[name]
        return names

    def get(self, name: str) -> Optional[WorldDescriptor]:
        """按机器名取世界描述符；未注册时返回 ``None``。"""

        return self._descriptors.get(name)

    def require(self, name: str) -> WorldDescriptor:
        """按机器名取世界描述符；未注册时直接报错。

        Raises:
            KeyError: 指定世界未注册时抛出。
        """

        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise KeyError(f"未注册的世界: {name!r}")
        return descriptor

    def names(self) -> List[str]:
        """按注册顺序返回全部世界机器名。"""

        return list(self._descriptors.keys())

    def descriptors(self) -> List[WorldDescriptor]:
        """按注册顺序返回全部世界描述符。"""

        return list(self._descriptors.values())

    def __len__(self) -> int:
        """已注册世界的数量。"""

        return len(self._descriptors)
