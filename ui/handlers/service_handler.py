"""外部服务进程托管（mixin）：mindcraft 等插件页启停的服务。"""

import json
import os

from PySide6.QtCore import QProcess, QTimer

from ui.utils.constants import _MINDCRAFT_DEFAULT_PERSONA


class ServiceHandler:
    """外部服务进程（QProcess 托管）：启动/停止/日志/退出回调。"""

    def _service_running(self, service_id: str) -> bool:
        """外部服务进程是否在运行。"""
        proc = self._svc_procs.get(service_id)
        return proc is not None and proc.state() != QProcess.NotRunning

    def _start_mindcraft(self) -> None:
        """启动 mindcraft（live-2d 重构版引擎）。

        先写 keys.json / andy.json / settings.js（复用本项目 LLM 与 MC 连接
        配置），再拉起 node main.js：引擎自带 MindServer + 自动注册 bot agent；
        主播程序通过 socket.io 双向桥连入（MINDCRAFT_BRIDGE_ENABLED=true 时）。
        """
        mdir = str(self.cfg.MINDCRAFT_PATH or "")
        if not os.path.isfile(os.path.join(mdir, "main.js")):
            self._log("[控制中心] Mindcraft 未安装，无法启动（先部署 plugins/mindcraft）\n")
            return
        if self._service_running("mindcraft"):
            self._log("[控制中心] Mindcraft 已在运行\n")
            return
        try:
            self._write_mindcraft_keys(mdir)
            self._write_mindcraft_profile(mdir)
            self._write_mindcraft_settings(mdir)
        except OSError as e:
            self._log(f"[控制中心] Mindcraft 配置写入失败：{e}\n")
            return
        proc = QProcess(self.ui)
        proc.setWorkingDirectory(mdir)
        # 优先用引擎自带的 node（已实体化到 plugins/mindcraft），缺失才回退系统 node
        node_exe = os.path.join(mdir, "node", "node.exe")
        proc.setProgram(node_exe if os.path.isfile(node_exe) else "node")
        proc.setArguments(["main.js"])
        # 不设 language（保持默认 en=不翻译）：GLM 原生支持中文，MC 聊天中文
        # 直接进 LLM，回复也是中文，避免引入 Google Translate 翻译链路。
        proc.readyReadStandardOutput.connect(
            lambda: self._log_svc_output(proc, "Mindcraft"))
        proc.readyReadStandardError.connect(
            lambda: self._log_svc_output(proc, "Mindcraft"))
        proc.errorOccurred.connect(
            lambda err: self._log(
                f"[控制中心] Mindcraft 启动失败：{err}\n"))
        proc.finished.connect(
            lambda code, _st: self._on_svc_finished("mindcraft", code))
        self._svc_procs["mindcraft"] = proc
        self._log(f"[控制中心] 正在启动 Mindcraft（{mdir}）…\n")
        proc.start()
        self._log("[控制中心] Mindcraft 已启动（请确保 Minecraft 已开服并开放 LAN）\n")

    def _write_mindcraft_keys(self, mdir: str) -> None:
        """写 mindcraft/keys.json：把本项目 LLM_API_KEY 映射为 OPENAI_API_KEY。"""
        keys_path = os.path.join(mdir, "keys.json")
        with open(keys_path, "w", encoding="utf-8") as f:
            json.dump({"OPENAI_API_KEY": getattr(self.cfg, "LLM_API_KEY", "") or ""},
                      f, ensure_ascii=False, indent=4)

    def _write_mindcraft_profile(self, mdir: str) -> None:
        """写 andy.json（live-2d 引擎默认 profile）：复用本项目 LLM + 本地 Embedding。

        model.url 指向本项目 LLM_BASE_URL（OpenAI 兼容端点），embedding 指向
        本地 EMBEDDING_BASE_URL；bot 与主播 AI 同大脑同记忆。
        """
        bot_name = str(getattr(self.cfg, "MINDCRAFT_BOT_NAME", "") or "vtuber")
        profile = {
            "name": bot_name,
            "model": {
                "api": "openai",
                "model": str(getattr(self.cfg, "MINDCRAFT_LLM_MODEL", "")
                             or "glm-4-flash-250414"),
                "url": str(getattr(self.cfg, "MINDCRAFT_LLM_BASE_URL", "") or ""),
            },
            "conversing": str(getattr(self.cfg, "MINDCRAFT_BOT_PERSONA", "")
                              or _MINDCRAFT_DEFAULT_PERSONA),
        }
        emb_url = getattr(self.cfg, "EMBEDDING_BASE_URL", "") or ""
        emb_model = getattr(self.cfg, "EMBEDDING_MODEL", "") or ""
        if emb_url and emb_model:
            profile["embedding"] = {
                "api": "openai", "url": emb_url, "model": emb_model}
        with open(os.path.join(mdir, "andy.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=4)

    def _write_mindcraft_settings(self, mdir: str) -> None:
        """写 settings.js（live-2d 引擎读取）：把 MC 连接与 MindServer 端口固化。

        替代旧版 SETTINGS_JSON 覆盖机制；language 保持 en=不翻译（见上）。
        """
        lines = [
            "const settings = {",
            '    "minecraft_version": "auto",',
            f'    "host": "{str(self.cfg.MINDCRAFT_HOST or "127.0.0.1")}",',
            f'    "port": {int(self.cfg.MINDCRAFT_PORT or 55916)},',
            f'    "auth": "{str(self.cfg.MINDCRAFT_AUTH or "offline")}",',
            f'    "mindserver_port": {int(getattr(self.cfg, "MINDCRAFT_MINDSERVER_PORT", "") or 8080)},',
            '    "auto_open_ui": false,',
            '    "base_profile": "assistant",',
            '    "profiles": ["./andy.json"],',
            '    "load_memory": false,',
            '    "init_message": null,',
            '    "only_chat_with": [],',
            '    "speak": false,',
            '    "chat_ingame": true,',
            '    "language": "en",',
            '    "render_bot_view": false,',
            '    "allow_insecure_coding": false,',
            '    "allow_vision": false,',
            '    "max_messages": 15,',
            '    "num_examples": 2,',
            '    "max_commands": 8,',
            '    "show_command_syntax": "none",',
            '    "narrate_behavior": false,',
            '    "chat_bot_messages": true,',
            '    "log_all_prompts": false,',
            "};",
            "",
            "export default settings;",
        ]
        with open(os.path.join(mdir, "settings.js"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _stop_service(self, service_id: str) -> None:
        """停止外部服务进程（terminate 优雅退出，超时后异步强杀）。"""
        proc = self._svc_procs.get(service_id)
        if proc is None or proc.state() == QProcess.NotRunning:
            self._svc_procs.pop(service_id, None)
            return
        self._log(f"[控制中心] 正在停止 {service_id}…\n")
        proc.terminate()
        # 异步超时强杀：waitForFinished 会在 UI 线程同步阻塞最长 8 秒（界面冻结），
        # 改用定时器延迟检查；进程正常退出走 finished 信号清理记录。
        timer = QTimer(self.ui)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._force_kill_service(proc))
        timer.start(5000)

    def _force_kill_service(self, proc: QProcess) -> None:
        """terminate 超时仍未退出时的强杀兜底（非阻塞）。"""
        try:
            if proc.state() != QProcess.NotRunning:
                proc.kill()
        except RuntimeError:
            pass  # 进程对象已随窗口销毁（timer 挂在 ui 上，理论上不会发生）

    def _on_svc_finished(self, service_id: str, exit_code: int) -> None:
        """外部服务进程退出：移除记录并重建卡片刷新状态。"""
        self._svc_procs.pop(service_id, None)
        if self._closing:  # UI 已销毁：只清记录，不再碰控件
            return
        self._log(f"[控制中心] {service_id} 已退出（code={exit_code}）\n")
        self._fill_plugin_cards()

    def _log_svc_output(self, proc: QProcess, tag: str) -> None:
        """把外部服务子进程的 stdout/stderr 转发到日志区。"""
        if proc is None:
            return
        data = bytes(proc.readAllStandardOutput())
        data += bytes(proc.readAllStandardError())
        if data:
            self._log(f"[{tag}] {data.decode('utf-8', errors='replace')}\n")
