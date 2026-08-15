"""多房间弹幕服务 + 本地 SSE/HTTP 服务器。

单职责：管理 N 个房间（BiliService 各跑一条 blivedm 线程） + 1 个
HTTP 服务器（/_SSEHandler），对外提供 push/broadcast/start/stop。
不关心弹幕怎么从 blivedm 出来（client.py 负责）、不关心谁来挑（picker.py 负责）。

依赖注入：
- BiliServiceManager 构造时接收 room_ids / port / html_path
- 头像字节缓存 AvatarImageCache 全局共享（同一批用户跨房间复用）
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

from src.danmaku.avatar import (
    AvatarImageCache, is_allowed_avatar_url, _UA,
)
from src.danmaku.broadcaster import Broadcaster
from src.utils import config
from src.utils import console


_HEARTBEAT_S = 15  # SSE 心跳（秒）


# ===== 单房间 =====

class BiliService:
    """单直播间弹幕连接：blivedm 客户端线程 + 弹幕广播。

    只负责本房间的 blivedm 连接与 SSE 广播数据；
    HTTP 服务器与头像缓存由 BiliServiceManager 统一管理。
    """

    def __init__(self, room_id: int, broadcaster: Broadcaster,
                 avatar_images: AvatarImageCache) -> None:
        self.room_id = room_id
        self.broadcaster = broadcaster
        self.avatar_images = avatar_images
        self._bili_thread: Optional[threading.Thread] = None
        # 客户端跑在哪个子模块的 import 函数（懒加载避免循环引用）
        self._start_client: Optional[callable] = None

    def set_client_starter(self, starter: callable) -> None:
        """由 service 层调用：注入 client.py 的线程启动函数。

        避免 service.py 直接 import client.py 形成循环：
        client.py 也要 import service.py 里的 BiliService 来构造 _BiliDanmakuClient。
        """
        self._start_client = starter

    def start(self) -> None:
        if self._bili_thread is not None and self._bili_thread.is_alive():
            return
        if self._start_client is None:
            raise RuntimeError("BiliService.set_client_starter 未调用")
        self._bili_thread = threading.Thread(
            target=self._start_client,
            args=(self.broadcaster, self.room_id, self.avatar_images),
            daemon=True)
        self._bili_thread.start()

    def stop(self) -> None:
        # blivedm 线程为 daemon，随进程退出自动结束（与原实现一致）
        self._bili_thread = None


# ===== 多房间 + HTTP 服务器 =====

class BiliServiceManager:
    """多直播间弹幕服务：一个 SSE 服务器 + 每房间一个 blivedm 连接。

    - 每个房间一个 BiliService（独立 broadcaster，弹幕互不串流）；
    - 头像字节缓存全局共享（同一批用户跨房间复用）；
    - SSE 网页 /events?room_id=X 订阅指定房间；不带 room_id 默认主房间（第一个），
      单房间用法与原实现完全一致。
    """

    def __init__(self, room_ids: List[int], port: int, html_path: str) -> None:
        self.html_path = html_path
        self.avatar_images = AvatarImageCache()  # 全局共享，HTTP 线程与弹幕线程共用
        self._services: List[BiliService] = []
        self._by_room: Dict[int, BiliService] = {}
        for room_id in room_ids:
            svc = BiliService(room_id, Broadcaster(), self.avatar_images)
            self._services.append(svc)
            self._by_room[room_id] = svc
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def broadcaster(self) -> Broadcaster:
        """主房间（第一个）广播：单房间调用方无感知。"""
        return self._services[0].broadcaster

    def broadcaster_for(self, room_id: int) -> Optional[Broadcaster]:
        """指定房间的弹幕广播；房间不存在返回 None（由调用方退回主房间）。"""
        svc = self._by_room.get(room_id)
        return svc.broadcaster if svc is not None else None

    def broadcast(self, msg: dict) -> None:
        """推给所有房间（如精选回复卡片跨房间可见）。"""
        for svc in self._services:
            svc.broadcaster.push(msg)

    def attach_client_starter(self, starter: callable) -> None:
        """把 client.py 的线程启动函数注入到每个 BiliService。"""
        for svc in self._services:
            svc.set_client_starter(starter)

    def start(self) -> None:
        if self._running:
            return
        if not self._services:
            return
        self._running = True
        ThreadingHTTPServer.daemon_threads = True
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", self._port),
            lambda *args, **kwargs: _SSEHandler(self, *args, **kwargs))
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        self._http_thread.start()

        if not config.cfg.BILI_SESSDATA:
            console.warn("[弹幕] 未配置 BILI_SESSDATA，可连接但用户名会打码")
        for svc in self._services:
            svc.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        if self._http_thread:
            self._http_thread.join(timeout=3)
        for svc in self._services:
            svc.stop()


# ===== HTTP server handler =====

class _SSEHandler(BaseHTTPRequestHandler):
    """气泡网页 + SSE 流（多房间：/events?room_id=X 订阅指定房间）。"""

    def __init__(self, mgr: BiliServiceManager, *args, **kwargs):
        self.mgr = mgr
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        pass  # 关闭默认访问日志

    def handle(self) -> None:
        try:
            super().handle()
        except Exception:
            pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/events":
            self._serve_events()
        elif path == "/avatar":
            self._serve_avatar()
        else:
            self.send_error(404)

    def _serve_avatar(self) -> None:
        """代理 B 站图床头像（防盗链：浏览器直连 hdslb.com 会被 403）。

        服务端用 B 站 Referer 拉图后转发；仅允许 hdslb.com 域名。
        先查共享字节缓存（预取/上次拉取已写入），命中直接回，秒开头像。
        """
        url = (parse_qs(urlparse(self.path).query).get("u") or [""])[0]
        if not is_allowed_avatar_url(url):
            self.send_error(400)
            return
        cached = self.mgr.avatar_images.get(url)
        if cached is not None:
            ctype, data = cached
        else:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA,
                              "Referer": "https://live.bilibili.com/"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read(2 * 1024 * 1024)
                    ctype = resp.headers.get("Content-Type",
                                             "application/octet-stream")
            except Exception:
                self.send_error(502)
                return
            if data:
                self.mgr.avatar_images.put(url, ctype, data)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_page(self) -> None:
        try:
            with open(self.mgr.html_path, "rb") as f:
                content = f.read()
        except OSError:
            content = "未找到气泡网页：新建 文本文档.html".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _sse_write(self, msg: dict) -> None:
        line = f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _serve_events(self) -> None:
        """SSE：先发历史，再实时推送；15s 心跳保活。

        按 seq 取增量：历史截断只影响"从头补发"，不影响增量判定，
        连接长时间打开也不会漏掉新弹幕。
        多房间：?room_id=X 订阅指定房间，缺省/房间不存在时退回主房间。
        """
        room_id = int((parse_qs(urlparse(self.path).query)
                       .get("room_id") or ["0"])[0] or 0)
        broadcaster = self.mgr.broadcaster_for(room_id) or self.mgr.broadcaster
        history, cond = broadcaster.consume()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        last_seq = 0
        with cond:
            if history:
                for _seq, msg in history:
                    self._sse_write(msg)
                last_seq = history[-1][0]
        try:
            while True:
                with cond:
                    new_msgs = [msg for _seq, msg in history
                                if _seq > last_seq]
                    if not new_msgs:
                        if not cond.wait(timeout=_HEARTBEAT_S):
                            self._sse_write({"type": "heartbeat"})
                            continue
                        new_msgs = [msg for _seq, msg in history
                                    if _seq > last_seq]
                    last_seq = history[-1][0]
                for msg in new_msgs:
                    self._sse_write(msg)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            pass
