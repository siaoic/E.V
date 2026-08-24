"""PluginManager 创建 + load_all/start_all（L408-420）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """PluginManager 创建 + load_all/start_all。

    注意：5.0 kernel 分支在进入 4.x 路径前已 return，此处一定 kernel=None，
    self.plugin_manager 不会与 kernel 分支的赋值冲突，可直接原样搬。
    """
    from ev.utils import console
    from plugins import PluginManager

    # 插件系统：加载 plugins/ 下启用的插件（Python 同进程 async 运行时，
    # 对标 live-2d 插件体系——钩子 / 工具 / 定时器，目录约定见 plugins/README.md）
    try:
        from plugins.manager import set_default_manager
        runtime.plugin_manager = PluginManager(runtime)
        await runtime.plugin_manager.load_all()
        await runtime.plugin_manager.start_all()
        # 供工具合并（get_merged_tools）与 speak_text 读取
        runtime.brain.plugin_manager = runtime.plugin_manager
        set_default_manager(runtime.plugin_manager)
    except Exception as e:
        runtime.plugin_manager = None
        console.warn(f"[插件] 插件系统初始化失败（不影响运行）：{e}")


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 中 plugin_manager.stop_all 段（第一个执行）。"""
    if runtime.plugin_manager is not None:
        try:
            await runtime.plugin_manager.stop_all()
        except Exception:
            pass
