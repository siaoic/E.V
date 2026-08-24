"""主进程管理（mixin）：启动/优雅停止/超时强杀/日志分栏/stdin 输入。"""

import os
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from ev.utils.console import CHAT_TAG
from ui.utils.ansi_helpers import strip_ansi
from ui.utils import env_helpers
from ui.utils.path_helpers import _find_project_root


class ProcessHandler:
    """主程序 QProcess 托管：启动/停止/日志/输入，供启动页、配置页等共用。"""

    def _toggle(self) -> None:
        """启动/停止切换：主程序未运行 → 启动；运行中 → 停止。"""
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            return
        mode = "pet" if self.radio_pet.isChecked() else "vtuber"
        # 打包只含 UI 启动器（无 VtuberMain.exe）：项目根（main.py 所在目录）
        # 可能 ≠ exe 目录，RUN_MODE / PET_MODEL_PATH 必须写进项目根 .env，
        # 主程序才能读到（frozen 时 config.PROJECT_ROOT 指向 exe 目录）。
        root = _find_project_root() if getattr(sys, "frozen", False) else ""
        if getattr(sys, "frozen", False) and not root:
            self._log("[控制中心] 未找到项目根（main.py），无法启动主程序。\n"
                      "请把 ControlCenter.exe 放在项目目录或其子目录（如 dist/）内。\n")
            return
        try:
            env_helpers._update_env("RUN_MODE", mode, root=root)
            if mode == "pet":
                env_helpers._update_env(
                    "PET_MODEL_PATH", self.combo_models.currentText().strip(),
                    root=root)
        except OSError as e:
            self._log(f"[控制中心] 写入 .env 失败：{e}\n")
            return

        env = QProcessEnvironment.systemEnvironment()
        env.insert("RUN_MODE", mode)
        if mode == "pet":
            # 关键：systemEnvironment() 会继承本进程旧环境（import config 时
            # load_dotenv 把当时的 PET_MODEL_PATH 写进了进程环境），子进程
            # load_dotenv 默认不覆盖已继承的环境变量 → 启动会加载旧模型。
            # 用最新值显式覆盖，保证「切模型 → 启动」立即生效，无需重启。
            env.insert("PET_MODEL_PATH", self.combo_models.currentText().strip())
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")  # 源码运行时逐行输出，日志实时滚动
        self.proc = QProcess()
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.cfg.PROJECT_ROOT)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(lambda *_: self._on_finished())
        # 启动失败（如 pythonw 缺失）必须在日志区可见，不能静默
        self.proc.errorOccurred.connect(
            lambda err: self._log(f"[控制中心] 启动主程序失败：{err}\n"))
        if getattr(sys, "frozen", False):
            # 打包环境只含 UI 启动器（不打包主程序）：用项目根 runtime venv
            # 的 pythonw 以源码方式启动 main.py。pythonw 是 windowed 子系统，
            # 不弹控制台黑框；QProcess 管道下 stdout/stderr/stdin 照常工作
            # （日志回显 + REPL 输入）。不用 python.exe——PySide6 的 QProcess
            # 没有 setCreateProcessArgumentsModifier，无法 CREATE_NO_WINDOW
            # 隐藏控制台窗口（该方法不存在，调用会抛 AttributeError）。
            py = os.path.join(root, "runtime", "Scripts", "pythonw.exe")
            if not os.path.isfile(py):
                self._log(f"[控制中心] 未找到 {py}，无法启动主程序。\n")
                return
            self.proc.setWorkingDirectory(root)
            self.proc.start(py, ["main.py"])
        else:
            self.proc.start(sys.executable, ["main.py"])

        self.btn_toggle.setText("停止")
        self._log(f"[控制中心] 已以「{mode}」模式启动\n")

    def _stop(self) -> None:
        """优雅停止：先发 /quit 让主程序走完归档/清理流程，超时未退出再强杀。

        直接 kill() 会跳过 main.py 的 finally——会话摘要/记忆蒸馏归档、TTS
        排空、MCP/STT 清理全部丢失（本轮对话记忆不落库）。先给 30 秒优雅
        窗口：归档包含两轮 LLM 调用（会话摘要 + 蒸馏），LLM 响应慢时可达
        十几秒，5 秒窗口会导致归档未完成就被强杀（记忆丢失 + Crashed）。
        """
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        if getattr(self, "_stopping", False):
            return  # 已在停止流程中（防连点重复发 /quit、重复起定时器）
        self._stopping = True
        self._log("[控制中心] 正在优雅停止主程序…（等待记忆归档完成，最长约 30 秒）\n")
        try:
            self.proc.write(b"/quit\n")
        except Exception:
            pass  # stdin 已关闭等极端情况：走下方超时强杀兜底
        timer = QTimer(self.ui)
        timer.setSingleShot(True)
        timer.timeout.connect(self._force_kill)
        timer.start(30000)
        self._stop_kill_timer = timer

    def _force_kill(self) -> None:
        """优雅退出超时兜底：30 秒仍未退出则强制结束。"""
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self._log("[控制中心] 主程序未在 30 秒内退出，强制结束\n")
            try:
                self.proc.kill()
            except Exception:
                pass

    def _cleanup_on_close(self) -> None:
        """窗口（UI）销毁时的 QProcess 清理。

        主程序进程不随控制中心关闭而终止（直播/桌宠继续独立运行），这里只
        断开全部信号并把 QProcess 从窗口树中脱离，避免：
          - readyRead/finished 回调访问已销毁的 UI 对象 → shiboken RuntimeError
          - QProcess 随窗口析构时子进程仍在运行 → "Destroyed while process
            is still running" 告警
        """
        self._closing = True
        proc = self.proc
        if proc is not None:
            for signal_name in ("readyReadStandardOutput", "readyReadStandardError",
                                "finished", "errorOccurred"):
                try:
                    getattr(proc, signal_name).disconnect()
                except (RuntimeError, TypeError):
                    pass
            if proc.state() != QProcess.NotRunning:
                proc.setParent(None)
        # 外部服务子进程（mindcraft 等）同样脱离窗口树，子进程本体不终止
        # （同主程序：控制中心关闭后 bot 继续独立运行），仅断开信号防回调
        for svc_proc in getattr(self, "_svc_procs", {}).values():
            for signal_name in ("readyReadStandardOutput", "readyReadStandardError",
                                "finished", "errorOccurred"):
                try:
                    getattr(svc_proc, signal_name).disconnect()
                except (RuntimeError, TypeError):
                    pass
            if svc_proc.state() != QProcess.NotRunning:
                svc_proc.setParent(None)
        # 移除 qApp 级拖拽过滤器，避免窗口销毁后残留处理事件
        app = QApplication.instance()
        drag_filter = getattr(self, "_drag", None)
        if app is not None and drag_filter is not None:
            app.removeEventFilter(drag_filter)

    def _send_text(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        if self.proc is not None and self.proc.state() == QProcess.ProcessState.Running:
            self.proc.write((text + "\n").encode("utf-8"))
            # 主程序等待输入时已打印提示符「你 > 」（控制中心下 input() 无
            # 回显），这里回显「你 > 内容」：先把日志末尾的提示符删掉再拼成
            # 同一行，与原生终端（stdin echo 显示「你 > 内容」）保持一致。
            # 若末尾不是提示符（如正在播报被拒收），直接追加一行。
            tail = self.ui.log_tool.toPlainText()
            if tail.endswith("你 > "):
                cur = self.ui.log_tool.textCursor()
                cur.movePosition(QTextCursor.End)
                cur.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len("你 > "))
                cur.removeSelectedText()
            self._log(f"你 > {text}\n")
        else:
            self._log("[控制中心] 主程序未运行，无法发送\n")
        self.input_edit.clear()

    def _on_stdout(self) -> None:
        if self.proc is not None and not self._closing:
            self._log(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace"))

    def _on_stderr(self) -> None:
        if self.proc is not None and not self._closing:
            self._log(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"))

    def _on_finished(self) -> None:
        if self._closing:
            return
        self.btn_toggle.setText("启动")
        self._log("[控制中心] 主程序已退出\n")
        # 复位优雅停止状态并取消超时强杀定时器（进程已结束）
        self._stopping = False
        timer = getattr(self, "_stop_kill_timer", None)
        if timer is not None:
            timer.stop()
            self._stop_kill_timer = None

    def _clear_logs(self) -> None:
        """清空左右两个日志面板（「清空日志」按钮）。"""
        self.ui.log_chat.clear()
        self.ui.log_tool.clear()

    def _append_log(self, widget, text: str) -> None:
        """把文本追加到指定日志控件尾部并滚动到底部。"""
        if not text:
            return
        cur = widget.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        widget.setTextCursor(cur)
        widget.ensureCursorVisible()

    def _log(self, text: str) -> None:
        # 主程序 stdout 的 console.* 输出带 ANSI 颜色码（\x1b[90m 等），
        # 日志控件不支持渲染，写入前统一剥离。
        # 分栏：被 CHAT_TAG（console.chat 的零宽标记）包裹的片段属于左栏
        # 「对话」，其余归右栏「工具日志」——按标记出现顺序切分，每遇到
        # 一个标记就切换一次归属，可正确处理两类内容交错到达的片段。
        is_chat = False
        chat_parts = []
        tool_parts = []
        for seg in text.split(CHAT_TAG):
            seg = strip_ansi(seg)
            (chat_parts if is_chat else tool_parts).append(seg)
            is_chat = not is_chat
        self._append_log(self.ui.log_chat, "".join(chat_parts))
        self._append_log(self.ui.log_tool, "".join(tool_parts))
