"""主进程管理（mixin）：启动/优雅停止/超时强杀/日志分栏/stdin 输入。"""

import codecs
import os
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from ev.utils.console import CHAT_TAG
from ui.utils.ansi_helpers import strip_ansi
from ui.utils import env_helpers
from ui.utils.path_helpers import _find_project_root


class _StdoutLineRouter:
    """主程序 stdout/stderr 流的按行分栏路由器（无状态，逐帧判归属）。

    主程序在管道模式下把对话内容成帧输出（console.chat：每帧
    "TAG 内容 TAG\\n"，见 ev/utils/console.py）。本路由器把字节流按
    \\n 切成完整行，**按行独立判归属**：含 CHAT_TAG 的行 → 对话栏
    （剥标记，不带换行，保持打字机累积显示）；否则 → 工具日志栏。

    与旧「标记交替」方案的区别：旧方案每遇到一个标记切换一次归属，
    一旦管道读取从 write 中间截断（LLM 流式 + TTS 合成抢 CPU 时频繁
    发生），标记配对错位会让之后所有内容左右栏整体对调——实测回复被
    劈成两半分落两栏（「缺字」假象）、日志成段挤进对话栏。按行路由
    无状态可累加，任何截断最多延迟一行，永不扩散。

    细节：
    - 增量 UTF-8 解码：多字节字符（含 3 字节的 CHAT_TAG 本身）被块边界
      劈开时留在解码器里等下一块补全，不会变成 U+FFFD 丢标记；
    - 尾部不成行：含完整帧（≥2 标记）→ 立即路由（帧后理应有 \\n，
      提前上屏保证流式观感）；只有 1 个标记 → 被劈开的帧，留缓冲等
      补全；无标记 → 立即上屏（「你 > 」提示符等无换行输出不能延迟）；
    - 空对话帧（TAGTAG）= 子进程的「换行」信号 → 对话面板追加换行。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._skip_nl = False  # 刚提前上屏过完整帧：下一块开头的 \n 是帧终止符

    def feed(self, data: bytes) -> tuple[str, str]:
        """喂入新到字节，返回 (对话栏增量, 工具栏增量)。"""
        text = self._decoder.decode(data)
        # Windows 管道 \r\n 归一化（防御：老版本主程序仍会发出 \r\n），
        # \r 留在行文本里会被 Qt 当作换行 → 流式块逐块断行
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._buffer += text
        return self._route()

    def flush(self) -> tuple[str, str]:
        """流结束（进程退出）：冲刷解码器残余并按工具栏兜底收尾。"""
        self._buffer += self._decoder.decode(b"", final=True)
        chat, tool = "", ""
        if self._buffer:
            if CHAT_TAG in self._buffer:
                chat = self._frame_text(self._buffer)
            else:
                tool = strip_ansi(self._buffer)
            self._buffer = ""
        return chat, tool

    # --------------------------- 内部 ---------------------------

    def _route(self) -> tuple[str, str]:
        # 上一轮提前上屏过完整帧：其帧后换行符是本块开头的第一个字节，
        # 吞掉（它是对话帧的终止符，不是工具日志的空行）
        if self._skip_nl and self._buffer:
            if self._buffer.startswith("\n"):
                self._buffer = self._buffer[1:]
            self._skip_nl = False
        chat_parts: list[str] = []
        tool_parts: list[str] = []
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self._route_line(line, chat_parts, tool_parts)
        # 尾部不成行的残余
        tail = self._buffer
        if CHAT_TAG in tail:
            if tail.count(CHAT_TAG) >= 2:
                # 完整帧已到、仅帧后换行符未到：立即上屏（流式观感），
                # 并记下下一块开头的 \n 是帧终止符，不当空行显示
                self._route_line(tail, chat_parts, tool_parts)
                self._buffer = ""
                self._skip_nl = True
            # 只有 1 个标记：帧被劈开，留缓冲等下一块补全
        elif tail:
            # 无标记残余：提示符「你 > 」等无换行输出，立即上屏不能延迟
            tool_parts.append(strip_ansi(tail))
            self._buffer = ""
        return "".join(chat_parts), "".join(tool_parts)

    @staticmethod
    def _route_line(line: str, chat_parts: list, tool_parts: list) -> None:
        if CHAT_TAG in line:
            text = strip_ansi(line.replace(CHAT_TAG, ""))
            if text:
                chat_parts.append(text)
            else:
                chat_parts.append("\n")  # 空帧 = 换行信号
        elif line:
            # 工具行；纯空行丢弃（对话帧内部的换行——如段落帧 TAG\n\nTAG
            # 的中段——不应在工具栏产生空行）
            tool_parts.append(strip_ansi(line) + "\n")

    @staticmethod
    def _frame_text(text: str) -> str:
        return strip_ansi(text.replace(CHAT_TAG, ""))


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
        # 新进程新流：行路由器（解码器 + 未成行缓冲）一并复位
        self._router_out = None
        self._router_err = None
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
            chat, tool = self._stdout_router().feed(
                bytes(self.proc.readAllStandardOutput()))
            self._append_log(self.ui.log_chat, chat)
            self._append_log(self.ui.log_tool, tool)

    def _on_stderr(self) -> None:
        if self.proc is not None and not self._closing:
            chat, tool = self._stderr_router().feed(
                bytes(self.proc.readAllStandardError()))
            self._append_log(self.ui.log_chat, chat)
            self._append_log(self.ui.log_tool, tool)

    # ---- stdout/stderr 行路由器（懒初始化；mixin 无 __init__） ----

    def _stdout_router(self) -> "_StdoutLineRouter":
        router = getattr(self, "_router_out", None)
        if router is None:
            router = _StdoutLineRouter()
            self._router_out = router
        return router

    def _stderr_router(self) -> "_StdoutLineRouter":
        router = getattr(self, "_router_err", None)
        if router is None:
            router = _StdoutLineRouter()
            self._router_err = router
        return router

    def _flush_routers(self) -> None:
        """进程退出时冲刷解码器与未成行的残余缓冲，避免尾部输出丢失。"""
        for getter in (self._stdout_router, self._stderr_router):
            try:
                chat, tool = getter().flush()
            except Exception:
                continue
            self._append_log(self.ui.log_chat, chat)
            self._append_log(self.ui.log_tool, tool)

    def _on_finished(self) -> None:
        if self._closing:
            return
        self._flush_routers()
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
        # 行路由器的未成行缓冲一并丢弃
        self._router_out = None
        self._router_err = None

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
        """控制中心自己产生的日志（非主程序 stdout）：直接进工具日志栏。"""
        self._append_log(self.ui.log_tool, strip_ansi(text))
