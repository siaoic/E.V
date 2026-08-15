"""插件管理器：扫描 plugins/ 目录，按 enabled_plugins.json 加载/卸载/热重载，
聚合插件工具，分发生命周期与消息钩子。

对标 live-2d 的 plugin-manager.js：目录约定（metadata.json +
enabled_plugins.json）、加载/卸载/热重载、钩子分发、工具聚合保持一致；
区别是本项目插件为 Python 同进程 async 运行时（importlib 直接加载，
钩子为 async 协程，无需子进程 JSON-RPC）。
"""

import asyncio
import importlib.util
import json
import os

from src.utils import config, console


def _normalize(path: str) -> str:
    """插件相对路径统一用正斜杠（跨平台一致）。"""
    return path.replace("\\", "/")


# ==================== 插件目录与启用列表的公共文件操作 ====================
# 主程序（PluginManager）与控制中心（plugin_handler）是两个进程，
# 都直接读写 plugins/ 目录，这里统一文件 IO，避免逻辑分叉。

def tool_name(tool_def) -> str | None:
    """兼容两种工具格式的取名：{name} 或 {function: {name}}。"""
    if isinstance(tool_def, dict):
        fn = tool_def.get("function")
        if isinstance(fn, dict):
            return fn.get("name")
        return tool_def.get("name")
    return None


def scan_plugin_dirs(plugins_dir: str) -> dict:
    """扫描插件根目录下带 metadata.json 的一级目录，返回 {相对路径: 目录绝对路径}。"""
    result = {}
    if not os.path.isdir(plugins_dir):
        return result
    for entry in os.listdir(plugins_dir):
        plugin_dir = os.path.join(plugins_dir, entry)
        if os.path.isdir(plugin_dir) and os.path.isfile(
                os.path.join(plugin_dir, "metadata.json")):
            result[entry] = plugin_dir
    return result


def load_enabled_plugins(plugins_dir: str) -> set:
    """读取 enabled_plugins.json 的启用相对路径集合（缺失/损坏返回空集）。"""
    path = os.path.join(plugins_dir, "enabled_plugins.json")
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {_normalize(p) for p in data.get("plugins", [])}
    except (OSError, ValueError):
        return set()


def save_enabled_plugins(plugins_dir: str, plugins) -> None:
    """写入 enabled_plugins.json（相对路径统一正斜杠）。"""
    path = os.path.join(plugins_dir, "enabled_plugins.json")
    normalized = [_normalize(p) for p in plugins]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"plugins": normalized}, f, ensure_ascii=False, indent=2)


class PluginManager:
    """插件生命周期与钩子分发中心。app 为 Application 实例（run 时注入）。"""

    def __init__(self, app, plugins_dir: str | None = None) -> None:
        self.app = app
        # 插件目录即本包目录：example/ 等插件、enabled_plugins.json 与框架代码同处
        self.plugins_dir = plugins_dir or os.path.join(
            config.cfg.PROJECT_ROOT, "plugins")
        # 插件名 -> {"plugin", "metadata", "dir", "rel"}
        self._plugins: dict = {}
        self._enabled: set | None = None   # 已启用相对路径集合（None = 尚未读取）
        self._system_prompt_patches: dict = {}   # patch_id -> 文本
        self._dynamic_tools: dict = {}           # 插件名 -> [tool_def]
        self._event_handlers: dict = {}          # 事件名 -> [(插件名, handler)]
        self._dispatching_llm_request = False    # 防 on_llm_request 递归调 LLM

    # ==================== enabled_plugins.json ====================

    def _load_enabled_list(self, force: bool = False) -> None:
        """读取 enabled_plugins.json 到 self._enabled（相对路径集合）。"""
        if self._enabled is not None and not force:
            return
        self._enabled = load_enabled_plugins(self.plugins_dir)

    def _scan_all_plugin_dirs(self) -> dict:
        """扫描 plugins/ 下带 metadata.json 的一级目录，返回 {相对路径: 插件目录绝对路径}。"""
        return scan_plugin_dirs(self.plugins_dir)

    # ==================== 加载 / 卸载 / 热重载 ====================

    async def load_all(self) -> None:
        """加载 plugins/ 下所有已启用插件（on_init 后不自动 on_start）。"""
        self._load_enabled_list()
        if not os.path.isdir(self.plugins_dir):
            return
        for entry in sorted(os.listdir(self.plugins_dir)):
            plugin_dir = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue
            try:
                await self.load(plugin_dir)
            except Exception as e:
                console.warn(f"[插件] 加载失败（{entry}）：{e}")

    async def load(self, plugin_dir: str) -> str | None:
        """加载单个插件目录：读取 metadata.json + 入口，实例化并 on_init。

        返回插件名（未启用 / 已加载时返回 None）。
        """
        meta_path = os.path.join(plugin_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f"metadata.json 解析失败：{e}") from e
        name = metadata.get("name") or os.path.basename(plugin_dir)
        rel = _normalize(os.path.relpath(plugin_dir, self.plugins_dir))

        self._load_enabled_list()
        if rel not in self._enabled:
            return None      # 未启用：跳过
        if name in self._plugins:
            return None      # 已加载：跳过

        main = metadata.get("main") or "index.py"
        main_path = os.path.join(plugin_dir, main)
        if not os.path.isfile(main_path):
            raise FileNotFoundError(f"插件入口文件不存在：{main_path}")

        from plugins.base import Plugin
        from plugins.context import PluginContext
        module = self._load_module(main_path)
        plugin_class = self._find_plugin_class(module, Plugin)
        context = PluginContext(self, plugin_dir, name)
        # 插件自身配置 plugin_config.json（可选）
        pc_path = os.path.join(plugin_dir, "plugin_config.json")
        if os.path.isfile(pc_path):
            try:
                with open(pc_path, "r", encoding="utf-8") as f:
                    context._plugin_config = json.load(f)
            except (OSError, ValueError):
                context._plugin_config = {}

        plugin = plugin_class()
        plugin.context = context
        plugin.metadata = metadata
        await plugin.on_init()

        self._plugins[name] = {"plugin": plugin, "metadata": metadata,
                               "dir": plugin_dir, "rel": rel}
        display = metadata.get("displayName") or name
        console.ok(f"[插件] 已加载：{display} v{metadata.get('version', '?')}")
        return name

    def _load_module(self, main_path: str):
        """用 importlib 加载插件入口模块（每次全新执行，热重载不命中缓存）。"""
        module_name = "plugin_" + os.path.basename(
            os.path.dirname(main_path)) + "_" + hex(abs(hash(main_path)))[2:]
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块：{main_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _find_plugin_class(module, plugin_base):
        """在模块里找继承 Plugin 的类：优先模块声明的 __plugin_class__，
        否则取第一个 Plugin 子类（按定义顺序）。"""
        declared = getattr(module, "__plugin_class__", None)
        if declared is not None:
            return declared
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, plugin_base)
                    and obj is not plugin_base):
                return obj
        raise TypeError("插件入口未定义继承自 Plugin 的类")

    async def unload(self, name: str) -> None:
        """卸载插件：on_stop → on_destroy → 移除注册（含动态工具与事件订阅）。"""
        entry = self._plugins.get(name)
        if entry is None:
            return
        for hook in (entry["plugin"].on_stop, entry["plugin"].on_destroy):
            try:
                await hook()
            except Exception:
                pass
        self._plugins.pop(name, None)
        self._dynamic_tools.pop(name, None)
        for event in list(self._event_handlers):
            self._event_handlers[event] = [
                (pn, h) for pn, h in self._event_handlers[event] if pn != name]
            if not self._event_handlers[event]:
                del self._event_handlers[event]
        console.dim(f"[插件] 已卸载：{name}")

    async def reload(self, name: str) -> None:
        """热重载插件：卸载后重新加载并 on_start。"""
        entry = self._plugins.get(name)
        if entry is None:
            raise KeyError(f"插件不存在：{name}")
        plugin_dir = entry["dir"]
        await self.unload(name)
        await self.load(plugin_dir)
        new_entry = self._plugins.get(name)
        if new_entry is not None:
            await new_entry["plugin"].on_start()
        console.ok(f"[插件] 已热重载：{name}")

    async def reload_all(self) -> None:
        """重载所有已加载插件（逐个容错）。"""
        for name in list(self._plugins):
            try:
                await self.reload(name)
            except Exception as e:
                console.warn(f"[插件] 热重载失败（{name}）：{e}")

    async def sync_enabled_plugins(self) -> None:
        """同步 enabled_plugins.json：卸载被禁用的、加载新启用的并 on_start。"""
        self._load_enabled_list(force=True)
        all_dirs = self._scan_all_plugin_dirs()
        loaded_by_rel = {entry["rel"]: name
                         for name, entry in self._plugins.items()}
        # 卸载被禁用的
        for rel, name in list(loaded_by_rel.items()):
            if rel not in self._enabled:
                try:
                    await self.unload(name)
                except Exception as e:
                    console.warn(f"[插件] 卸载失败（{name}）：{e}")
        # 加载新启用的
        for rel in self._enabled:
            if rel in loaded_by_rel:
                continue
            plugin_dir = all_dirs.get(rel)
            if plugin_dir is None:
                continue
            try:
                loaded = await self.load(plugin_dir)
                if loaded is not None:
                    await self._plugins[loaded]["plugin"].on_start()
            except Exception as e:
                console.warn(f"[插件] 加载新启用插件失败（{rel}）：{e}")
        console.dim(f"[插件] 插件同步完成，当前共 {len(self._plugins)} 个")

    async def apply_enabled(self, rel_path: str, enabled: bool) -> str:
        """启用/禁用插件：写 enabled_plugins.json 并热加载/卸载，返回结果文本。"""
        rel = _normalize((rel_path or "").strip())
        plugins = load_enabled_plugins(self.plugins_dir)
        if enabled and rel in plugins:
            return f"插件已处于启用状态：{rel}"
        if not enabled and rel not in plugins:
            return f"插件已处于禁用状态：{rel}"
        if enabled:
            plugins.add(rel)
        else:
            plugins.remove(rel)
        try:
            save_enabled_plugins(self.plugins_dir, plugins)
        except OSError as e:
            raise OSError(f"写入 enabled_plugins.json 失败：{e}") from e
        self._enabled = set(plugins)
        if enabled:
            plugin_dir = self._scan_all_plugin_dirs().get(rel)
            if plugin_dir is None:
                return f"未找到插件目录：{rel}"
            loaded = await self.load(plugin_dir)
            if loaded is None:
                return f"插件加载失败或已加载：{rel}"
            await self._plugins[loaded]["plugin"].on_start()
            return f"插件已启用并加载：{rel}"
        name = next((n for n, e in self._plugins.items() if e["rel"] == rel), None)
        if name is not None:
            await self.unload(name)
        return f"插件已禁用：{rel}"

    # ==================== 启停 ====================

    async def start_all(self) -> None:
        """对所有已加载插件调用 on_start。"""
        for name, entry in list(self._plugins.items()):
            try:
                await entry["plugin"].on_start()
            except Exception as e:
                console.warn(f"[插件] on_start 错误（{name}）：{e}")

    async def stop_all(self) -> None:
        """对所有已加载插件调用 on_stop（应用退出前清理）。"""
        for name, entry in list(self._plugins.items()):
            try:
                await entry["plugin"].on_stop()
            except Exception as e:
                console.warn(f"[插件] on_stop 错误（{name}）：{e}")

    # ==================== 钩子分发 ====================

    async def run_user_input_hooks(self, event) -> None:
        """按顺序执行所有插件的 onUserInput；插件可 stop_propagation 中断。"""
        for name, entry in list(self._plugins.items()):
            if event.stopped:
                break
            try:
                await entry["plugin"].on_user_input(event)
            except Exception as e:
                console.warn(f"[插件] on_user_input 错误（{name}）：{e}")

    async def run_llm_request_hooks(self, request) -> None:
        """执行所有插件的 onLLMRequest（可修改 request.messages）。"""
        if self._dispatching_llm_request:
            return
        self._dispatching_llm_request = True
        try:
            for name, entry in list(self._plugins.items()):
                try:
                    await entry["plugin"].on_llm_request(request)
                except Exception as e:
                    console.warn(f"[插件] on_llm_request 错误（{name}）：{e}")
        finally:
            self._dispatching_llm_request = False

    async def run_llm_response_hooks(self, response) -> None:
        """执行所有插件的 onLLMResponse。"""
        for name, entry in list(self._plugins.items()):
            try:
                await entry["plugin"].on_llm_response(response)
            except Exception as e:
                console.warn(f"[插件] on_llm_response 错误（{name}）：{e}")

    async def run_tts_text_hooks(self, text: str) -> str:
        """链式执行 onTTSText：每个插件可改写送 TTS 的文本。"""
        result = text
        for name, entry in list(self._plugins.items()):
            try:
                modified = await entry["plugin"].on_tts_text(result)
                if isinstance(modified, str):
                    result = modified
            except Exception as e:
                console.warn(f"[插件] on_tts_text 错误（{name}）：{e}")
        return result

    async def run_tts_start_hooks(self, text: str) -> None:
        """执行所有插件的 onTTSStart。"""
        for name, entry in list(self._plugins.items()):
            try:
                await entry["plugin"].on_tts_start(text)
            except Exception as e:
                console.warn(f"[插件] on_tts_start 错误（{name}）：{e}")

    async def run_tts_end_hooks(self) -> None:
        """执行所有插件的 onTTSEnd。"""
        for name, entry in list(self._plugins.items()):
            try:
                await entry["plugin"].on_tts_end()
            except Exception as e:
                console.warn(f"[插件] on_tts_end 错误（{name}）：{e}")

    # ==================== 工具聚合 ====================

    def get_all_tools(self) -> list:
        """合并所有插件 get_tools + 动态注册工具（OpenAI function calling 格式）。"""
        tools = []
        for name, entry in self._plugins.items():
            try:
                plugin_tools = entry["plugin"].get_tools()
                if isinstance(plugin_tools, list):
                    tools.extend(plugin_tools)
            except Exception:
                pass
        for tool_list in self._dynamic_tools.values():
            tools.extend(tool_list)
        return tools

    async def execute_tool(self, name: str, params: dict) -> str | None:
        """路由工具调用到提供它的插件；无插件提供时返回 None（走本地兜底）。"""
        for pname, entry in self._plugins.items():
            defs = []
            try:
                plugin_tools = entry["plugin"].get_tools() or []
                if isinstance(plugin_tools, list):
                    defs.extend(plugin_tools)
            except Exception:
                pass
            defs.extend(self._dynamic_tools.get(pname, []))
            if any(tool_name(d) == name for d in defs):
                return await entry["plugin"].execute_tool(name, params or {})
        return None

    def register_dynamic_tool(self, plugin_name: str, tool_def: dict) -> None:
        """运行时动态注册工具（context.register_tool 调用），同名去重。"""
        registered = tool_name(tool_def)
        if not registered:
            raise ValueError("工具定义缺少 name（OpenAI function calling 格式）")
        self._dynamic_tools.setdefault(plugin_name, [])
        if not any(tool_name(t) == registered
                   for t in self._dynamic_tools[plugin_name]):
            self._dynamic_tools[plugin_name].append(tool_def)

    def unregister_dynamic_tool(self, plugin_name: str, name: str) -> None:
        """移除插件动态注册的工具。"""
        self._dynamic_tools[plugin_name] = [
            t for t in self._dynamic_tools.get(plugin_name, [])
            if tool_name(t) != name]

    # ==================== 系统提示补丁 ====================

    def add_system_prompt_patch(self, patch_id: str, text: str) -> None:
        """注入系统提示词补丁（直到 remove 或卸载插件）。"""
        if text:
            self._system_prompt_patches[patch_id] = text
        else:
            self._system_prompt_patches.pop(patch_id, None)

    def remove_system_prompt_patch(self, patch_id: str) -> None:
        self._system_prompt_patches.pop(patch_id, None)

    def system_prompt_patch_section(self) -> str:
        """拼装所有生效的系统提示补丁为一段（无补丁返回空串）。"""
        if not self._system_prompt_patches:
            return ""
        lines = "\n".join(f"- {t}" for t in self._system_prompt_patches.values())
        return ("### 插件注入的长期提示（addSystemPromptPatch 生效中）\n" + lines)

    # ==================== 事件总线 ====================

    def on(self, event: str, handler, plugin_name: str) -> None:
        """订阅事件总线（跨插件松耦合通信）。"""
        self._event_handlers.setdefault(event, [])
        if (plugin_name, handler) not in self._event_handlers[event]:
            self._event_handlers[event].append((plugin_name, handler))

    def off(self, event: str, handler, plugin_name: str) -> None:
        """退订事件总线。"""
        try:
            self._event_handlers[event].remove((plugin_name, handler))
        except ValueError:
            pass

    async def emit(self, event: str, data=None) -> None:
        """发布事件到总线（同步 / 异步 handler 均支持）。"""
        for name, handler in list(self._event_handlers.get(event, [])):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                console.warn(f"[插件] 事件处理错误（{event} @{name}）：{e}")

    # ==================== 查询 ====================

    def get_plugin(self, name: str):
        """获取插件实例（插件间通信）；不存在返回 None。"""
        entry = self._plugins.get(name)
        return entry["plugin"] if entry else None

    def get_plugin_list(self) -> list:
        """所有已加载插件信息列表（供 !plugins / UI 使用）。"""
        return [{
            "name": name,
            "displayName": entry["metadata"].get("displayName") or name,
            "version": entry["metadata"].get("version", "?"),
            "rel": entry["rel"],
            "dir": entry["dir"],
        } for name, entry in self._plugins.items()]


# ==================== 模块级单例 ====================

_default_manager: PluginManager | None = None


def set_default_manager(manager: PluginManager | None) -> None:
    """注册全局默认插件管理器（Application.run 时注入，供工具合并 / speak_text 读取）。"""
    global _default_manager
    _default_manager = manager


def get_default_manager() -> PluginManager | None:
    """获取全局默认插件管理器（未初始化返回 None）。"""
    return _default_manager
