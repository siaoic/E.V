"""VTube 模式网页在线字幕服务器（参考桌宠模式 BubbleSub 接口设计）。

提供 HTTP + SSE 的轻量 Web 字幕层：
- 浏览器打开 http://127.0.0.1:{port}/ 即可看到透明字幕层
- push("text", text) 推送字幕累积文本（打字机效果）
- push("clear", "") 清除字幕并触发淡出
- push("user", text) 用户/观众发言（弹幕、键盘输入）——按需求隐藏，字幕页只显示 AI 播报
- 字幕持续显示，直到 push("clear", "") 触发淡出（回复结束才隐藏，句间不闪烁）

字体：
- 中文：字魂布丁体（src/utils/字魂布丁体(商用需授权).ttf）
- 英文：ArtierEN-2（src/utils/ArtierEN-2.ttf）
@font-face + unicode-range 按字符自动分流。
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from typing import Callable, Optional

from src.utils import config, console


# 中文字体文件（项目根路径）
_FONT_CN_NAME = "字魂布丁体(商用需授权).ttf"
_FONT_EN_NAME = "ArtierEN-2.ttf"

# 字幕页面文件（与本文件同目录）
_HTML_NAME = "字幕.html"

# 默认端口：与弹幕服务 8766 错开
_DEFAULT_PORT = 8765


def _font_dir() -> str:
    """字体文件目录：src/utils/（与本文件同目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def _html_path() -> str:
    """字幕页面文件路径：ui/字幕.html（网页资源统一放在项目 ui/ 目录）。"""
    return os.path.join(config.cfg.PROJECT_ROOT, "ui", _HTML_NAME)


def _font_cn_path() -> str:
    return os.path.join(_font_dir(), _FONT_CN_NAME)


def _font_en_path() -> str:
    return os.path.join(_font_dir(), _FONT_EN_NAME)


# ----------------------------- 同步 SSE 广播管理（threading.Queue） -----------------------------

class _SSEBroker:
    """简易 SSE 广播：任意线程 push → 所有连接的浏览器收到。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[Queue] = []

    def attach(self) -> Queue:
        q: Queue = Queue(maxsize=64)
        with self._lock:
            self._queues.append(q)
        return q

    def detach(self, q: Queue) -> None:
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def broadcast(self, data: str, event: str = "message") -> None:
        """跨线程安全：把 SSE 帧写入每路连接的队列。"""
        msg = f"event: {event}\ndata: {data}\n\n"
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(msg)
            except Exception:
                # 队列满（浏览器卡死不读）→ 直接忽略，不阻塞广播
                pass


# ----------------------------- 页面：字幕.html（独立文件） -----------------------------
# 字幕页面已抽离为 ui/字幕.html，按需从磁盘读取（改样式无需重启服务）。


# ----------------------------- HTTP Request Handler -----------------------------

class _SubtitleHandler(BaseHTTPRequestHandler):
    """HTTP 请求路由：/ 返回 HTML、/events 返回 SSE、/font/*.ttf 返回字体。"""

    server_version = "EVSubtitle/1.0"

    # 静态共享：由 SubtitleServer 启动时注入
    broker: Optional[_SSEBroker] = None
    font_cn: bytes = b""
    font_en: bytes = b""

    # ---------- 工具：简化响应 ----------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    # ---------- SSE ----------

    def _do_events(self) -> None:
        broker = _SubtitleHandler.broker
        if broker is None:
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q: Optional[Queue] = None
        try:
            q = broker.attach()
            # 初始心跳（SSE 规范：注释帧保持连接）
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except Exception:
                return
            # 循环读取队列：阻塞 2s 超时 → 发注释帧保活；异常/断开 → 退出
            while True:
                try:
                    msg = q.get(timeout=2.0)
                except Empty:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    except Exception:
                        return
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    # 浏览器断开连接（wfile 写失败）
                    return
        except Exception:
            pass
        finally:
            if q is not None and broker is not None:
                broker.detach(q)

    # ---------- 路由分发 ----------

    def do_GET(self) -> None:  # noqa: N802 (HTTP 方法命名)
        path = (self.path or "").split("?", 1)[0]
        if path == "/" or path == "/index.html":
            # 每次请求从磁盘读取，改样式/JS 刷新即生效，无需重启服务
            try:
                with open(_html_path(), "rb") as f:
                    body = f.read()
            except OSError:
                body = f"未找到字幕页面：{_HTML_NAME}".encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/events":
            self._do_events()
        elif path == "/font/cn.ttf":
            if _SubtitleHandler.font_cn:
                self._send(200, _SubtitleHandler.font_cn, "font/ttf")
            else:
                self._not_found()
        elif path == "/font/en.ttf":
            if _SubtitleHandler.font_en:
                self._send(200, _SubtitleHandler.font_en, "font/ttf")
            else:
                self._not_found()
        else:
            self._not_found()

    def log_message(self, format, *args) -> None:  # noqa: A002 (基类签名)
        # 静默：不打印请求日志，避免刷屏
        return


# ----------------------------- 对外 SubtitleServer -----------------------------

class SubtitleServer:
    """VTube 模式网页字幕层（与桌宠模式 BubbleSub 接口对齐）。

    用法：
        sub = SubtitleServer().start()
        sub.push("text", spoken_text)   # 打字机累积显示
        sub.push("clear", "")           # 对话结束：2.5 秒后淡出
        sub.stop()                      # 进程退出前清理
    """

    def __init__(self) -> None:
        self.port: int = _DEFAULT_PORT
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._th: Optional[threading.Thread] = None
        self._broker = _SSEBroker()
        self._last_text: str = ""

    # ---------- 生命周期 ----------

    def start(self) -> "SubtitleServer":
        """启动 HTTP 服务器（绑定成功后返回 self，port 属性可用）。"""
        # 预加载字体（二进制，避免请求时重复读文件）
        for path, attr, name in [
            (_font_cn_path(), "font_cn", _FONT_CN_NAME),
            (_font_en_path(), "font_en", _FONT_EN_NAME),
        ]:
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        setattr(_SubtitleHandler, attr, f.read())
                except Exception as e:
                    console.warn(f"字幕字体 {name} 读取失败：{e}")
            else:
                console.warn(f"字幕字体 {name} 不存在：{path}")
        _SubtitleHandler.broker = self._broker

        # 尝试绑定：默认端口 +1 递增，最多试 16 次
        last_err = None
        for offset in range(16):
            port = _DEFAULT_PORT + offset
            try:
                self._httpd = ThreadingHTTPServer(
                    ("127.0.0.1", port), _SubtitleHandler)
                self.port = port
                break
            except OSError as e:
                last_err = e
                self._httpd = None
        if self._httpd is None:
            console.warn(f"字幕服务器启动失败：{last_err}")
            return self

        def _serve():
            try:
                self._httpd.serve_forever(poll_interval=0.5)
            except Exception:
                pass

        self._th = threading.Thread(target=_serve, name="SubtitleServer", daemon=True)
        self._th.start()
        return self

    def stop(self) -> None:
        """停止 HTTP 服务器（进程退出 finally 统一调用）。"""
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None
        self._broker = _SSEBroker()  # 丢弃旧连接
        _SubtitleHandler.broker = None

    # ---------- 字幕接口（与 BubbleSub 对齐） ----------

    def push(self, kind: str, text: str = "", speed_ms: int = 0) -> None:
        """推送字幕事件（任意线程安全）。

        kind:
          - "text": 累积字幕文本（打字机效果），sent = 新显示的整句
          - "user": 用户/观众发言（弹幕、键盘输入）——字幕页不显示，忽略
          - "clear": 清除（对话结束，浏览器端淡出隐藏）
        """
        if kind == "clear":
            # 广播空事件：浏览器端保留文字并触发 CSS 淡出（不立即清空，过渡更平滑）
            self._broker.broadcast("", event="clear")
            self._last_text = ""
            return
        if kind == "user":
            # 用户/观众发言（弹幕、键盘输入、语音识别）不在字幕页显示，
            # 字幕页只呈现 AI 播报内容（按需求隐藏，接口保留）
            return
        if kind == "text":
            t = str(text or "")
            self._last_text = t
            self._broker.broadcast(t, event="text")
            return
        # 未知类型忽略
