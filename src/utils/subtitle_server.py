"""字幕网页服务器：SSE 流推送（与 TTS 引擎的字级时间戳字幕一致）。

浏览器打开 http://127.0.0.1:8765/ 即可看到虚拟主播的字幕：
- AI 回复按「字级时间戳」逐字浮现（由 TTS 引擎驱动，对齐语音，非前端估速）
- 历史记录向上滚动（小字号）
- 状态栏显示连接状态（自动重连）

实现：ThreadingHTTPServer（标准库），两个端点：
- GET /        → 字幕网页（HTML + JS 直接显示 + EventSource）
- GET /events  → SSE 流（text/event-stream），广播 {type, text} 消息

与 engine.py 一致：引擎每推进一个字级时间戳，就 push 一次「累积到当前字的
整句文本」，前端直接替换显示——逐字浮现完全由真实音频时间戳驱动，
前端不做估算打字机。

用法：
    server = SubtitleServer().start()
    server.push("text", "你好呀～")   # 累积文本（每字推进推送一次）
    server.push("user", "在吗")      # 用户输入
    server.stop()
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from src.utils import console

# 默认端口，可用环境变量 SUBTITLE_PORT 覆盖
_PORT = int(os.getenv("SUBTITLE_PORT", "8765"))

# SSE 心跳间隔（秒，防止代理/浏览器断开空闲连接）
_HEARTBEAT_S = 15
# 历史记录保留条数（新连接先收到历史再接收实时）
_MAX_HISTORY = 200

# 字幕字体：与服务器同目录的 ArtierEN-2.ttf（缺失时回退系统字体，不影响功能）
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ArtierEN-2.ttf")
_FONT_BYTES: bytes | None = None
try:
    with open(_FONT_PATH, "rb") as _f:
        _FONT_BYTES = _f.read()
except OSError:
    _FONT_BYTES = None

_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 虚拟主播字幕</title>
<style>
  @font-face {
    font-family: 'ArtierEN';
    src: url('/font.ttf') format('truetype');
    font-display: swap;
  }
  :root { --accent:#58a6ff; }
  * { box-sizing: border-box; }
  /* 透明背景：可直接叠加在 OBS/直播画面上；白字+黑描边保证任意背景可读 */
  body { background:transparent; color:#ffffff;
         font-family:'ArtierEN','Microsoft YaHei','PingFang SC',sans-serif;
         margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         padding:48px; }
  .stage { width:100%; max-width:1100px; text-align:center; }
  .subtitle { font-size:46px; line-height:1.7; font-weight:600; min-height:1.7em;
              text-shadow:0 2px 10px rgba(0,0,0,.75), 0 0 4px rgba(0,0,0,.6);
              word-break:break-all; }
</style>
</head>
<body>
  <div class="stage">
    <div class="subtitle" id="subtitle"></div>
  </div>

<script>
  const es = new EventSource('/events');
  const subtitle = document.getElementById('subtitle');

  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'clear') {
      // 收到清除指令 → 立即清空字幕
      subtitle.textContent = '';
      return;
    }
    // 只显示 AI 正在说的那句，忽略用户输入
    if (msg.type === 'text') {
      // 字级时间戳驱动：TTS 引擎每推进一个字，就推送「累积到当前字的整句文本」，
      // 前端直接替换显示即可（逐字浮现的节奏由真实音频时间戳决定，前端不估速）。
      subtitle.textContent = msg.text;
    }
  };
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    """字幕页面 + SSE 流。"""

    def __init__(self, server: "SubtitleServer", *args, **kwargs) -> None:
        self.sub = server
        super().__init__(*args, **kwargs)

    # 关闭默认访问日志（避免刷屏）
    def log_message(self, fmt: str, *args) -> None:
        pass

    def handle(self) -> None:
        """浏览器可能在请求中途断开（刷新/关闭标签页/取消字体下载），
        静默忽略，避免 socketserver 线程打印 traceback 刷屏。"""
        try:
            super().handle()
        except Exception:
            pass

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._serve_page()
        elif self.path == "/events":
            self._serve_events()
        elif self.path == "/font.ttf":
            self._serve_font()
        else:
            self.send_error(404)

    def _serve_font(self) -> None:
        """返回 ArtierEN-2.ttf 字体文件（@font-face 引用）。"""
        if not _FONT_BYTES:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "font/ttf")
        self.send_header("Content-Length", str(len(_FONT_BYTES)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(_FONT_BYTES)

    def _serve_page(self) -> None:
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 禁止缓存：避免浏览器加载旧版页面（如残留的"已连接"状态栏）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        """SSE：先发历史，再实时推送；15s 心跳保活。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            idx = 0  # 历史游标
            with self.sub._cond:
                while True:
                    # 1) 推送所有新消息
                    while idx < len(self.sub._history):
                        msg = self.sub._history[idx]
                        idx += 1
                        self.wfile.write(
                            f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    # 2) 心跳保活（等待新消息或超时）
                    self.sub._cond.wait(timeout=_HEARTBEAT_S)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 浏览器断开，正常结束
        except Exception:
            pass


class SubtitleServer:
    """后台线程运行的字幕服务器（线程安全 push）。"""

    def __init__(self, port: int = _PORT) -> None:
        self.port = port
        self._cond = threading.Condition()
        self._history: List[dict] = []
        self._httpd: ThreadingHTTPServer = None
        self._thread: threading.Thread = None

    def start(self) -> "SubtitleServer":
        handler = lambda *a, **kw: _Handler(self, *a, **kw)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        console.info(f"字幕网页已启动：http://127.0.0.1:{self.port}/")
        return self

    def push(self, type_: str, text: str, speed_ms: Optional[int] = None) -> None:
        """推送一条字幕消息。

        type_=text（AI 回复）/ user（用户输入，前端忽略）。
        与 engine.py 字级时间戳字幕一致：引擎每推进一个字调用一次
        push("text", <累积文本>)，前端直接替换显示——逐字浮现由真实
        音频时间戳驱动。speed_ms 参数仅保留兼容（前端不再用于估速打字机）。
        """
        text = (text or "").strip()
        if not text:
            return
        msg = {"type": type_, "text": text}
        if speed_ms:
            msg["speed_ms"] = int(speed_ms)
        with self._cond:
            self._history.append(msg)
            if len(self._history) > _MAX_HISTORY:
                del self._history[:-_MAX_HISTORY]
            self._cond.notify_all()

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None


if __name__ == "__main__":
    import time
    s = SubtitleServer().start()
    print("推送测试：打开 http://127.0.0.1:%d/ 观察打字机效果" % s.port)
    s.push("user", "测试：你好！")
    s.push("text", "大家好呀，欢迎来到我的直播间！这是字幕打字机效果演示。")
    time.sleep(30)
    s.stop()
