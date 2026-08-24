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

from ev.utils import config, console


def _normalize(path: str) -> str:
    """插件相对路径统一用正斜杠（跨平台一致）。"""
    return path.replace("\\", "/")


# ==================== 插件目录与启用列表的公共文件操作 ====================
# 主程序（PluginManager）与控制中心（plugin_handler）是两个进程，
# 都直接读写 plugins/ 目录，这里统一文件 IO，避免逻辑分叉。
#
# 瘦身 6.1：优先读写 configs/plugins/{enabled,disabled}.json（双文件），
# 同时读写旧路径 plugins/enabled_plugins.json（单文件）保持兼容。
_CONFIGS_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configs", "plugins")
_CONFIGS_ENABLED_JSON = os.path.join(_CONFIGS_PLUGIN_DIR, "enabled.json")
_CONFIGS_DISABLED_JSON = os.path.join(_CONFIGS_PLUGIN_DIR, "disabled.json")


def _read_json_plugins(path: str) -> set:
    """读取一个 {"plugins": [...]} JSON 文件并归一化去重；损坏/缺失返空集。"""
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    return {_normalize(p) for p in data.get("plugins", [])}


def _write_json_plugins(path: str, plugins) -> None:
    """写 {"plugins": [...]}；自动建上级目录。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"plugins": sorted(_normalize(p) for p in plugins)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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


def load_plugin_sets(plugins_dir: str) -> tuple:
    """读取启用/禁用相对路径集合：(enabled, disabled)（缺失/损坏返回空集）。

    优先读 configs/plugins/{enabled,disabled}.json（新双文件格式）；
    与旧格式 plugins/enabled_plugins.json 做并集，保证迁移无遗漏。
    plugins 为 opt-in 启用清单；disabled 为显式禁用清单（禁用后移入，
    永不因自动登记重新启用）。相对路径统一正斜杠。
    """
    enabled = set()
    disabled = set()
    # 1) 新目录双文件
    enabled |= _read_json_plugins(_CONFIGS_ENABLED_JSON)
    disabled |= _read_json_plugins(_CONFIGS_DISABLED_JSON)
    # 2) 旧格式单文件 fallback（包含 enabled 主表 + 内嵌 disabled 扩展）
    old_path = os.path.join(plugins_dir, "enabled_plugins.json")
    if os.path.isfile(old_path):
        try:
            with open(old_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            enabled |= {_normalize(p) for p in data.get("plugins", [])}
            disabled |= {_normalize(p) for p in data.get("disabled", [])}
        except (OSError, ValueError):
            pass
    return enabled, disabled


def save_plugin_sets(plugins_dir: str, enabled, disabled=None) -> None:
    """写入启用/禁用清单：双写新目录 + 旧目录，保持两边同步。

    空禁用清单不写入旧单文件的 disabled 字段（保持原格式兼容）。
    """
    enabled_norm = sorted(_normalize(p) for p in enabled)
    disabled_norm = sorted(_normalize(p) for p in disabled) if disabled else []
    # 新目录双文件
    _write_json_plugins(_CONFIGS_ENABLED_JSON, enabled_norm)
    if disabled is not None:
        _write_json_plugins(_CONFIGS_DISABLED_JSON, disabled_norm)
    # 旧格式单文件
    old_path = os.path.join(plugins_dir, "enabled_plugins.json")
    os.makedirs(os.path.dirname(old_path) or ".", exist_ok=True)
    data = {"plugins": enabled_norm}
    if disabled:
        data["disabled"] = disabled_norm
    with open(old_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_enabled_plugins(plugins_dir: str) -> set:
    """读取 enabled_plugins.json 的启用相对路径集合（兼容旧调用）。"""
    enabled, _ = load_plugin_sets(plugins_dir)
    return enabled


def save_enabled_plugins(plugins_dir: str, plugins) -> None:
    """写入启用清单，保留既有禁用清单（兼容旧调用）。"""
    _, disabled = load_plugin_sets(plugins_dir)
    save_plugin_sets(plugins_dir, plugins, disabled)


class PluginManager:
    """插件生命周期与钩子分发中心。app 为 Application 实例（run 时注入）。"""

    def __init__(self, app=None, plugins_dir: str | None = None, kernel=None) -> None:
        """app 为 Application / RuntimeContext；kernel 为 5.0 Kernel 实例（5.0 外旧路径可为 None）。"""
        self.app = app
        self.kernel = kernel   # 新增：None = 4.x 路径；有值 = 5.0 路径（Profile 驱动）
        # 插件目录即本包目录：example/ 等插件、enabled_plugins.json 与框架代码同处
        self.plugins_dir = plugins_dir or os.path.join(
            config.cfg.PROJECT_ROOT, "plugins")
        # 插件名 -> {"plugin", "metadata", "dir", "rel"}
        self._plugins: dict = {}
        self._enabled: set | None = None   # 已启用相对路径集合（None = 尚未读取）
        self._disabled: set | None = None  # 已禁用相对路径集合（显式禁用，永不自动登记）
        self._system_prompt_patches: dict = {}   # patch_id -> 文本
        self._dynamic_tools: dict = {}           # 插件名 -> [tool_def]
        self._event_handlers: dict = {}          # 事件名 -> [(插件名, handler)]
        self._memory_providers: dict = {}        # 插件名 -> 记忆 provider（3.11）
        self._dispatching_llm_request = False    # 防 on_llm_request 递归调 LLM
        # 新增：子命令注册表（register_subcommand 写入）
        if not hasattr(self, "_subcommands"):
            self._subcommands: dict[str, dict] = {}

    # ==================== enabled_plugins.json ====================

    def _load_enabled_list(self, force: bool = False) -> None:
        """读取 enabled_plugins.json 的启用/禁用集合（相对路径）。"""
        if self._enabled is not None and not force:
            return
        self._enabled, self._disabled = load_plugin_sets(self.plugins_dir)

    def _user_plugins_dir(self) -> str:
        """用户插件根目录（DATA_ROOT/plugins，可写数据根）。不存在返回空串。"""
        root = getattr(config.cfg, "DATA_ROOT", "") or ""
        return os.path.join(root, "plugins") if root else ""

    def _scan_all_plugin_dirs(self) -> dict:
        """扫描内建（plugins/）与用户（DATA_ROOT/plugins/）目录。

        3.11 发现顺序：bundled > user；同名插件以内建优先并告警（跳过
        用户目录版本），避免同名冲突无裁决规则。
        """
        result = scan_plugin_dirs(self.plugins_dir)
        user_dir = self._user_plugins_dir()
        if user_dir and user_dir != self.plugins_dir:
            for rel, path in scan_plugin_dirs(user_dir).items():
                if rel in result:
                    console.warn(
                        f"[插件] 同名插件 {rel} 已存在于内建目录，跳过用户目录版本")
                    continue
                result[rel] = path
        return result

    def _auto_register_new(self, plugin_dirs: dict) -> None:
        """新插件目录自动登记进启用清单（丢目录即用，4.x）。

        仅在既非启用也非禁用时登记一次；显式禁用过的插件不会被重新启用。
        登记落盘后与 load_all / sync_enabled_plugins 共用同一份清单。
        """
        changed = False
        for rel in plugin_dirs:
            if rel not in self._enabled and rel not in self._disabled:
                self._enabled.add(rel)
                changed = True
        if changed:
            try:
                save_plugin_sets(self.plugins_dir, self._enabled, self._disabled)
            except OSError as e:
                console.warn(f"[插件] 自动登记启用清单失败：{e}")

    # ==================== 加载 / 卸载 / 热重载 ====================

    async def load_by_profile(self, profile: dict) -> None:
        """按 profile.plugins 加载插件（5.0 路径）。builtin 插件按目录加载；pypi/git 留 stub（Task17 安装）。"""
        plugins_cfg: dict = profile.get("plugins", {}) if isinstance(profile, dict) else {}
        builtin_names: list[str] = list(plugins_cfg.get("builtin", []) or [])

        # 收集候选目录：内建根 plugins/（self.plugins_dir）+ 用户根（DATA_ROOT/plugins）
        candidate_roots: list[str] = [self.plugins_dir]
        user_dir = self._user_plugins_dir()
        if user_dir and user_dir not in candidate_roots:
            candidate_roots.append(user_dir)
        # 也加上 ~/.ev/plugins（Task17 ev plugin add 目标目录）
        import os as _os
        ev_user = _os.path.expanduser("~/.ev/plugins")
        if _os.path.isdir(ev_user) and ev_user not in candidate_roots:
            candidate_roots.append(ev_user)

        loaded_count = 0
        for name in builtin_names:
            found_path: str | None = None
            for root in candidate_roots:
                maybe = _os.path.join(root, name)
                if _os.path.isdir(maybe) and _os.path.isfile(_os.path.join(maybe, "metadata.json")):
                    found_path = maybe
                    break
            # 兼容 plugins/builtin/<name>/ 子目录（echo_llm/llm_openai_compat 放在 builtin 子目录）
            if found_path is None:
                for root in candidate_roots:
                    maybe = _os.path.join(root, "builtin", name)
                    if _os.path.isdir(maybe) and _os.path.isfile(_os.path.join(maybe, "metadata.json")):
                        found_path = maybe
                        break
            if found_path is None:
                console.warn(f"[插件] 找不到 profile 声明的内建插件目录：{name}（已搜索 {candidate_roots}）")
                continue
            try:
                result = await self.load(found_path)   # 复用原 load(plugin_dir) 返回插件名或 None；成功则计数
                if result is not None:
                    loaded_count += 1
            except Exception as e:
                console.warn(f"[插件] 按 profile 加载 {name} 失败：{e}")

        # pypi/git 远端：当前 stub，等待 Task17
        remote_count = len(plugins_cfg.get("pypi", [])) + len(plugins_cfg.get("git", []))
        if remote_count > 0:
            console.warn(f"[插件] 共 {remote_count} 项远端插件（pypi/git）需等待 Task17 实现，本次跳过")

        console.dim(f"[插件] profile 驱动加载完成：{loaded_count}/{len(builtin_names)} 个内建插件")

    async def load_all(self) -> None:
        """有 kernel 且 kernel 有 resolved profile → 走 profile；否则走旧路径（enabled_plugins.json）。"""
        if self.kernel is not None:
            try:
                prof = self.kernel.profile  # Kernel.profile 属性会自动 resolve
                if isinstance(prof, dict) and prof.get("plugins"):
                    await self.load_by_profile(prof)
                    return
            except Exception as e:
                console.warn(f"[插件] profile 驱动加载失败，回退旧路径：{e}")
        # 回退：旧路径（enabled_plugins.json + 自动登记）
        await self._load_all_legacy()

    async def _load_all_legacy(self) -> None:
        """旧 load_all 逻辑：扫描 plugins 目录 + enabled_plugins.json + 自动登记新目录。"""
        self._load_enabled_list()
        plugin_dirs = self._scan_all_plugin_dirs()
        self._auto_register_new(plugin_dirs)
        for plugin_dir in sorted(plugin_dirs.values()):
            try:
                await self.load(plugin_dir)
            except Exception as e:
                console.warn(f"[插件] 加载失败（{os.path.basename(plugin_dir)}）：{e}")

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
        # 3.11 双目录发现：相对路径以各自根目录为基准（内建 vs 用户）
        if os.path.commonpath([plugin_dir, self.plugins_dir]) == self.plugins_dir:
            rel_root = self.plugins_dir
        else:
            rel_root = self._user_plugins_dir()
        rel = _normalize(os.path.relpath(plugin_dir, rel_root))

        # profile 路径（kernel 存在）：不依赖 enabled_plugins.json，profile 声明即启用
        if self.kernel is None:
            self._load_enabled_list()
            if rel in self._disabled:
                return None      # 显式禁用：跳过
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

        # ---- 5.0 新风格：如果 module 顶层有 register(ctx) 函数 → 调用 ----
        if hasattr(module, "register") and callable(module.register):
            try:
                # 创建 PluginContext（复用旧的 context 构造）
                ctx = PluginContext(self, plugin_dir, name)
                # （context.slots / jobs / session / config 已由 context.py 的
                #  5.0 属性自动从 self.kernel 读取，无需手动注入）
                # 把 context 的 self._plugin_config 先尝试加载旧 plugin_config.json（如果目录里有）
                pc_path = os.path.join(plugin_dir, "plugin_config.json")
                if os.path.isfile(pc_path):
                    try:
                        with open(pc_path, "r", encoding="utf-8") as f:
                            ctx._plugin_config = json.load(f)
                    except Exception:
                        ctx._plugin_config = {}
                # 调用 register(ctx)
                module.register(ctx)
                # 记录：作为"已加载"放入 self._plugins（让 list/info 命令能查到）
                # 为了让 start_all/钩子分发/unload 等旧逻辑不经修改就能兼容，
                # 这里不再把 plugin 写成 None，而是存一个继承 Plugin 的 Noop 空实例
                # （基类 Plugin 已实现所有 17 个钩子的空 stub）
                class _NoopPlugin(Plugin):
                    """register(function) 风格插件专属的 Plugin 占位桩，避免 plugin=None 导致
                    start_all/钩子分发/unload_all 出现 AttributeError。"""
                    pass
                _noop = _NoopPlugin()
                _noop.context = ctx
                _noop.metadata = metadata
                self._plugins[name] = {
                    "plugin": _noop,
                    "metadata": metadata,
                    "dir": plugin_dir,
                    "rel": rel,
                    "register_style": "function",
                }
                console.dim(
                    f"[插件] 已加载（register 风格）："
                    f"{metadata.get('displayName') or name} v{metadata.get('version', '?')}")
                return name   # 成功：返回插件名
            except Exception as e:
                raise RuntimeError(
                    f"插件 {name} (register 风格) 初始化失败：{e}") from e
        # ---- 旧风格：找 Plugin 子类（原逻辑保持一字不动）----

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

        # 3.11 可选编程式注册入口：插件包内声明 register(ctx) 即可用
        # （走到这里说明未命中新风格，兼容保留：共存式温和调用，异常只打警告）
        register_fn = getattr(module, "register", None)
        if callable(register_fn):
            try:
                register_fn(context)
            except Exception as e:
                console.warn(f"[插件] register(ctx) 执行失败（{name}）：{e}")

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
        self._memory_providers.pop(name, None)
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
        """同步 enabled_plugins.json：登记新插件、卸载被禁用的、加载新启用的并 on_start。"""
        self._load_enabled_list(force=True)
        all_dirs = self._scan_all_plugin_dirs()
        self._auto_register_new(all_dirs)
        loaded_by_rel = {entry["rel"]: name
                         for name, entry in self._plugins.items()}
        # 卸载被禁用的
        for rel, name in list(loaded_by_rel.items()):
            if rel not in self._enabled or rel in self._disabled:
                try:
                    await self.unload(name)
                except Exception as e:
                    console.warn(f"[插件] 卸载失败（{name}）：{e}")
        # 加载新启用的
        for rel in self._enabled:
            if rel in loaded_by_rel or rel in self._disabled:
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
        """启用/禁用插件：写 enabled_plugins.json 并热加载/卸载，返回结果文本。

        禁用移入 disabled 清单，确保下次自动登记不会重新启用。
        """
        rel = _normalize((rel_path or "").strip())
        enabled_set, disabled_set = load_plugin_sets(self.plugins_dir)
        if enabled:
            if rel in enabled_set and rel not in disabled_set:
                return f"插件已处于启用状态：{rel}"
            enabled_set.add(rel)
            disabled_set.discard(rel)
        else:
            if rel not in enabled_set and rel in disabled_set:
                return f"插件已处于禁用状态：{rel}"
            disabled_set.add(rel)
            enabled_set.discard(rel)
        try:
            save_plugin_sets(self.plugins_dir, enabled_set, disabled_set)
        except OSError as e:
            raise OSError(f"写入 enabled_plugins.json 失败：{e}") from e
        self._enabled = enabled_set
        self._disabled = disabled_set
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

    # ==================== 钩子分发（新增 7 个 + 通用 dispatch） ====================

    async def dispatch_hook(self, name: str, *args, **kwargs):
        """通用钩子分发：按名字 getattr 调用（兼容 17 个钩子全部）。

        支持 plugin=Plugin 子类实例（旧风格）以及通过事件总线编程式注册
        的 handler（register_hook → on() → emit 路径）。register 风格
        插件无 Plugin 实例，其钩子通过 register_hook 注册到事件总线
        后走同名事件分发（此处同步转发一份到 emit 路径）。
        """
        # 1) Plugin 子类实例：getattr 调用
        for pname, entry in list(self._plugins.items()):
            plugin = entry.get("plugin")
            if plugin is None:
                continue
            fn = getattr(plugin, name, None)
            if fn is None or not callable(fn):
                continue
            try:
                ret = fn(*args, **kwargs)
                if asyncio.iscoroutine(ret):
                    ret = await ret
                # on_tts_text 链式：返回字符串时作为下一个插件的第一参数
                if name == "on_tts_text" and isinstance(ret, str):
                    if args:
                        args = (ret,) + tuple(args[1:])
            except Exception as e:
                console.warn(f"[插件] {name} 错误（{pname}）：{e}")
        # 2) 编程式注册钩子（register_hook → on() 事件总线）：按同名事件分发
        #    这样 register 风格插件通过 ctx.register_hook 注册的回调也能被
        #    dispatch_hook 命中
        if self._event_handlers.get(name):
            data = args[0] if len(args) == 1 else (args if args else kwargs)
            await self.emit(name, data)
        # 3) on_tts_text 的返回值：当只有一个 arg（text）时返回链式结果
        if name == "on_tts_text" and len(args) >= 1:
            return args[0]
        return None

    async def run_slot_activate_hooks(self, event) -> None:
        """执行所有插件的 on_slot_activate（Slot 切换事件）。"""
        await self.dispatch_hook("on_slot_activate", event)

    async def run_session_start_hooks(self) -> None:
        """执行所有插件的 on_session_start（会话开始）。"""
        await self.dispatch_hook("on_session_start")

    async def run_session_end_hooks(self) -> None:
        """执行所有插件的 on_session_end（会话结束）。"""
        await self.dispatch_hook("on_session_end")

    async def run_danmaku_hooks(self, event) -> None:
        """执行所有插件的 on_danmaku（弹幕到达）。"""
        await self.dispatch_hook("on_danmaku", event)

    async def run_emotion_decide_hooks(self, event) -> None:
        """执行所有插件的 on_emotion_decide（情绪分类决策）。"""
        await self.dispatch_hook("on_emotion_decide", event)

    async def run_proactive_decide_hooks(self, event) -> None:
        """执行所有插件的 on_proactive_decide（主动对话决策）。"""
        await self.dispatch_hook("on_proactive_decide", event)

    async def run_config_reload_hooks(self, new_config: dict) -> None:
        """执行所有插件的 on_config_reload（配置热更新）。"""
        await self.dispatch_hook("on_config_reload", new_config)

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

    # ==================== 记忆 provider 暂存（3.11） ====================

    def register_memory_provider(self, plugin_name: str, provider) -> None:
        """暂存插件编程式注册的记忆 provider（同名后写胜出）。"""
        self._memory_providers[plugin_name] = provider

    def get_memory_providers(self) -> dict:
        """返回全部已注册记忆 provider 的 {插件名: provider} 快照。"""
        return dict(self._memory_providers)

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


async def handle_plugins_command(text: str) -> tuple:
    """处理控制台 !plugins 子命令，返回 (handled, result)。

    用法：!plugins list | sync | enable <相对路径> | disable <相对路径>
          | reload <插件名>；未知子命令返回用法说明。
    """
    parts = (text or "").strip().split()
    if not parts or parts[0] != "!plugins":
        return False, ""
    manager = get_default_manager()
    if manager is None:
        return True, "插件系统未初始化"
    sub = parts[1] if len(parts) > 1 else "help"
    if sub == "list":
        rows = manager.get_plugin_list()
        if not rows:
            return True, "当前没有已加载的插件"
        lines = [f"{r['displayName']} v{r['version']}（{r['rel']}）"
                 for r in rows]
        return True, "\n".join(lines)
    if sub == "sync":
        await manager.sync_enabled_plugins()
        return True, f"插件同步完成，当前共 {len(manager.get_plugin_list())} 个"
    if sub in ("enable", "disable") and len(parts) >= 3:
        result = await manager.apply_enabled(parts[2], sub == "enable")
        return True, result
    if sub == "reload" and len(parts) >= 3:
        try:
            await manager.reload(parts[2])
        except KeyError as e:
            return True, str(e)
        return True, f"插件已热重载：{parts[2]}"
    return True, ("用法：!plugins list | sync | enable <相对路径> "
                  "| disable <相对路径> | reload <插件名>")
