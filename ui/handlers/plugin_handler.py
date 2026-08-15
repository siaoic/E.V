"""插件卡片逻辑（mixin）：工具行数据收集 / 启停 / MCP 服务器配置 / 热通知。"""

import json
import os

from PySide6.QtCore import QProcess

from src.utils import config
from ui.utils import env_helpers


class PluginHandler:
    """插件页数据与启停逻辑：收集工具行、切换启用状态、读写 mcp_config.json。"""

    def _tool_rows(self) -> list:
        """收集工具行数据：(id, name, kind, enabled, checkable, status, desc)。

        id = 对应 .env 的 TOOL_*_ENABLED / MCP_ENABLED，或 "mcp_server:<json key>"
        （含 _disabled 后缀），点击切换时据此写回配置。
        """
        cfg = self.cfg
        rows: list = []

        def add(tid, name, kind, enabled, checkable, status, desc):
            rows.append({"id": tid, "name": name, "kind": kind,
                         "enabled": enabled, "checkable": checkable,
                         "status": status, "desc": desc})

        master_on = bool(cfg.TOOLS_ENABLED)  # 设置页「启动工具」总开关

        def add_local(tid, name, enabled, checkable, status, desc):
            """本地工具行：总开关（启动工具）关闭时强制显示已关、不可单独切换。"""
            if not master_on:
                enabled, checkable, status = False, False, "⭕ 总开关已关"
            add(tid, name, "本地", enabled, checkable, status, desc)

        # ---- 本地 Function Call 工具 ----
        if cfg.TOOL_WEB_SEARCH_ENABLED:
            if cfg.TAVILY_API_KEY:
                add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", True, True,
                    "✅ 已启用", "联网搜索（Tavily）")
            else:
                add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", True, True,
                    "⚠️ 未配置 key", "需在 .env 设置 TAVILY_API_KEY")
        else:
            add_local("TOOL_WEB_SEARCH_ENABLED", "web_search", False, True,
                "⭕ 已关闭", "联网搜索（Tavily）")
        if cfg.TOOL_GET_CURRENT_TIME_ENABLED:
            add_local("TOOL_GET_CURRENT_TIME_ENABLED", "get_current_time",
                True, True, "✅ 已启用", "获取当前时间（无外部依赖）")
        else:
            add_local("TOOL_GET_CURRENT_TIME_ENABLED", "get_current_time",
                False, True, "⭕ 已关闭", "获取当前时间（无外部依赖）")
        if cfg.TOOL_GET_WEATHER_ENABLED:
            if cfg.OPENWEATHERMAP_API_KEY:
                add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", True, True,
                    "✅ 已启用", "查询天气（OpenWeatherMap）")
            else:
                add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", True, True,
                    "⚠️ 未配置 key", "需在 .env 设置 OPENWEATHERMAP_API_KEY")
        else:
            add_local("TOOL_GET_WEATHER_ENABLED", "get_weather", False, True,
                "⭕ 已关闭", "查询天气（OpenWeatherMap）")
        skill_n = self._count_skills()
        if cfg.TOOL_LOAD_SKILL_ENABLED:
            if skill_n > 0:
                add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", True, True,
                    f"✅ 已启用（{skill_n} 个技能）", "按需加载技能 SKILL.md")
            else:
                add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", True, True,
                    "⚠️ 无技能", "SKILLS_DIR 目录下没有 SKILL.md")
        else:
            add_local("TOOL_LOAD_SKILL_ENABLED", "load_skill", False, True,
                "⭕ 已关闭", "按需加载技能 SKILL.md")
        if cfg.TOOL_LOOK_SCREEN_ENABLED:
            add_local("TOOL_LOOK_SCREEN_ENABLED", "look_at_screen",
                True, True, "✅ 已启用", "屏幕截图 + 视觉模型描述画面")
        else:
            add_local("TOOL_LOOK_SCREEN_ENABLED", "look_at_screen",
                False, True, "⭕ 已关闭", "屏幕截图 + 视觉模型描述画面")
        if cfg.TOOL_PLAY_SFX_ENABLED:
            add_local("TOOL_PLAY_SFX_ENABLED", "play_sound_effect",
                True, True, "✅ 已启用", "播放音效增强表现力（本地 wav）")
        else:
            add_local("TOOL_PLAY_SFX_ENABLED", "play_sound_effect",
                False, True, "⭕ 已关闭", "播放音效增强表现力（本地 wav）")

        # ---- MCP（外部工具服务器） ----
        # MCP 未启用（MCP_ENABLED 为空）时，运行时不会加载 MCP 工具
        # （application.py 中 MCPManager 仅在 MCP_ENABLED && TOOLS_ENABLED
        # 时才创建），因此这里也不显示服务器行，避免与本地工具混排。
        # 在 .env 开启 MCP_ENABLED 后，服务器行自动恢复显示与启停管理。
        if master_on and cfg.MCP_ENABLED:
            for key in self._all_mcp_servers():
                display = key[:-len("_disabled")] if key.endswith("_disabled") else key
                enabled = not key.endswith("_disabled")
                if enabled:
                    add(f"mcp_server:{key}", f"MCP：{display}", "MCP", True, True,
                        "✅ 已配置", "mcp_config.json 外部工具服务器")
                else:
                    add(f"mcp_server:{key}", f"MCP：{display}", "MCP", False, True,
                        "⭕ 已禁用", "mcp_config.json 外部工具服务器（点击恢复）")

        # ---- 外部服务（插件页进程托管，独立于工具总开关） ----
        # mindcraft：LLM 驱动的 Minecraft bot。已 clone + npm install（有 main.js）
        # 才允许启停；未安装时提示手动执行 git clone + npm install。
        mc_dir = str(self.cfg.MINDCRAFT_PATH or "")
        mc_installed = (os.path.isfile(os.path.join(mc_dir, "main.js"))
                        and os.path.isfile(os.path.join(mc_dir, "package.json")))
        if mc_installed:
            if self._service_running("mindcraft"):
                add("service:mindcraft", "Mindcraft", "外部", True, True,
                    "✅ 运行中", "LLM 驱动的 Minecraft bot（复用本项目 LLM）")
            else:
                add("service:mindcraft", "Mindcraft", "外部", False, True,
                    "⭕ 已停止", "LLM 驱动的 Minecraft bot（复用本项目 LLM）")
        else:
            add("service:mindcraft", "Mindcraft", "外部", False, False,
                "⚠️ 未安装", "未找到 main.js，请先 git clone + npm install")
        # neuro-sdk：Neuro-sama 游戏接入协议 + SDK，无可托管进程 → 信息卡片
        add("service:neuro_sdk", "Neuro SDK", "外部", False, False,
            "ℹ️ 参考", "Neuro-sama 游戏接入协议+SDK（文档，无独立服务）")
        return rows

    def _on_plugin_toggle(self, idx: int, checked: bool) -> None:
        """点击插件卡片/切换按钮 → 启用/关闭（不可切换的卡片忽略）。"""
        if idx < 0 or idx >= len(self._plugin_rows):
            return
        if not self._plugin_rows[idx]["checkable"]:
            return
        self._apply_plugin_toggle(idx, checked)

    def _apply_plugin_toggle(self, idx: int, checked: bool) -> None:
        """启用/关闭一个插件：写 .env / mcp_config.json，或启停外部服务进程。"""
        tid = self._plugin_rows[idx]["id"]
        if tid == "service:mindcraft":
            # 外部服务：启用 = 启动 mindcraft 子进程；关闭 = 停止。
            # 进程状态不写 .env（实时维护在 _svc_procs），不触发工具热更新。
            if checked:
                self._start_mindcraft()
            else:
                self._stop_service("mindcraft")
            self._fill_plugin_cards()
            return
        try:
            if tid.startswith("mcp_server:"):
                key = tid[len("mcp_server:"):]
                if not self._toggle_mcp_server(key, checked):
                    raise OSError(
                        "MCP 服务器重命名失败（同名冲突或配置文件不可写）")
                self._log(f"[控制中心] MCP 服务器已{'启用' if checked else '禁用'}："
                          f"{key[:-len('_disabled')] if key.endswith('_disabled') else key}\n")
            else:
                env_helpers._update_env(tid, "true" if checked else "false")
                self._log(f"[控制中心] 插件开关已保存：{tid}="
                          f"{'true' if checked else 'false'}\n")
        except OSError as e:
            self._log(f"[控制中心] 写入配置失败：{e}\n")
            # 回滚显示（配置未写入，重建卡片恢复原状）
            self._fill_plugin_cards()
            return
        # 本进程配置刷新（插件页/设置页显示用），随后重建卡片与状态
        config.reload_tool_runtime()
        if getattr(self.ui, "cb_mcp", None) is not None:
            self.cb_mcp.setChecked(bool(self.cfg.TOOLS_ENABLED))
        self._fill_plugin_cards()
        self._notify_main_tools()

    def _mcp_server_json(self, key: str) -> str:
        """读取 mcp_config.json 中指定服务器的 JSON 段（找不到返回空对象）。"""
        try:
            with open(self.cfg.MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return "{}"
        return json.dumps(data.get(key, {}), ensure_ascii=False, indent=2)

    def _save_mcp_server_json(self, key: str) -> None:
        """把配置页的 JSON 写回 mcp_config.json 中对应服务器条目。"""
        if self._config_mcp_editor is None:
            return
        new_entry = json.loads(self._config_mcp_editor.toPlainText())  # 非法 JSON → ValueError
        if not isinstance(new_entry, dict):
            raise ValueError("MCP 服务器配置必须是 JSON 对象")
        path = self.cfg.MCP_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except OSError:
            data = {}
        data[key] = new_entry
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _toggle_mcp_server(self, key: str, enabled: bool) -> bool:
        """启/停一个 MCP 服务器：在 mcp_config.json 中加/去 _disabled 后缀。

        幂等：key 已是目标状态或已不存在（被改名）时直接返回 True。
        """
        path = self.cfg.MCP_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        if enabled:
            if not key.endswith("_disabled"):
                return True  # 已是启用状态
            if key not in data:
                return True  # 旧 key 已被改名/删除，视为已恢复
            target = key[:-len("_disabled")]
            if target in data:  # 原名已被占用，改名冲突
                return False
            data[target] = data.pop(key)
        else:
            if key.endswith("_disabled"):
                return True  # 已是禁用状态
            if key not in data:
                return True  # 旧 key 已被改名/删除，视为已禁用
            target = key + "_disabled"
            if target in data:  # 目标名已被占用，改名冲突
                return False
            data[target] = data.pop(key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            return False
        return True

    def _all_mcp_servers(self) -> list:
        """读取 mcp_config.json 的全部服务器 key（含 _disabled 后缀的禁用项）。"""
        try:
            with open(self.cfg.MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                return sorted(json.load(f).keys())
        except (OSError, json.JSONDecodeError):
            return []

    def _notify_main_tools(self) -> None:
        """主程序运行中 → 发 !tools 命令热生效；未运行则下次启动生效。"""
        running = (self.proc is not None
                   and self.proc.state() == QProcess.ProcessState.Running)
        if running:
            self.proc.write(b"!tools\n")
            self._log("[控制中心] 已通知主程序热更新工具配置\n")
        else:
            self._log("[控制中心] 主程序未运行，工具配置将在下次启动生效\n")

    def _count_skills(self) -> int:
        """统计 SKILLS_DIR（逗号分隔多根）下的技能数（<技能名>/SKILL.md）。"""
        base = (self.cfg.SKILLS_DIR or "").strip()
        roots = [r.strip() for r in base.split(",") if r.strip()] if base else []
        total = 0
        for root in roots:
            d = root if os.path.isabs(root) else os.path.join(
                self.cfg.PROJECT_ROOT, root)
            if os.path.isdir(d):
                for entry in os.listdir(d):
                    if os.path.isfile(os.path.join(d, entry, "SKILL.md")):
                        total += 1
        return total
