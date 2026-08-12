"""B 站直播弹幕模块（blivedm → SSE 弹幕气泡网页）。

弹幕来源：blivedm（https://github.com/Ikaros-521/blivedm）web 端
WebSocket 协议，仅需 SESSDATA Cookie（不填也可连接，但用户名会打码）：

  - BLiveClient 内部自动重连（默认 1s 间隔）
  - 头像 face 随消息直接下发，无需额外请求 card API
  - 事件 → SSE → 前端「新建 文本文档.html」展示弹幕气泡
"""

import asyncio
import heapq
import http.cookies
import json
import logging
import os
import random
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import aiohttp
import blivedm
import blivedm.models.web as web_models

from src.utils import config
from src.utils import console
from src.utils.profanity_filter import ProfanityFilter

# blivedm 库内部用标准 logging 输出 WARNING/INFO（如 unknown cmd 会打一条超长
# protobuf 日志）。控制台不显示弹幕信息：只保留 ERROR 级以上诊断。
logging.getLogger("blivedm").setLevel(logging.ERROR)

# ===== 运行参数 =====
_HEARTBEAT_S = 15      # SSE 心跳（秒）
_MAX_HISTORY = 20      # SSE 历史缓存条数
_RECONNECT_S = 5       # 客户端异常退出后的重建间隔（秒）
_AVATAR_CACHE_MAX = 1000   # 头像缓存条数上限
_DEDUP_WINDOW_S = 300      # 相同弹幕去重窗口（秒）：窗口内同内容只展示/回复一次
_RECENT_DANMAKU_MAX = 500  # 相同弹幕去重缓存条数上限（防无限膨胀）

# 头像代理白名单：仅允许 B 站图床（hdslb.com）。
# 严格 host 校验避免 "https://evil.com/?ref=hdslb.com" 之类的 URL 绕过 → SSRF
_ALLOWED_AVATAR_HOSTS = {"hdslb.com", "www.hdslb.com", "i0.hdslb.com",
                         "i1.hdslb.com", "i2.hdslb.com"}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 脏话过滤器（data/profanity.txt，7 万词，惰性加载一次）
_PROFANITY_FILTER: "ProfanityFilter | None" = None

# 相同弹幕去重缓存：text → 最近一次展示时间戳。
# 仅弹幕事件循环线程读写（无并发）；重连后 handler 重建但缓存保留。
_RECENT_DANMAKU: "OrderedDict[str, float]" = OrderedDict()


def _get_profanity_filter() -> ProfanityFilter:
    global _PROFANITY_FILTER
    if _PROFANITY_FILTER is None:
        _PROFANITY_FILTER = ProfanityFilter()
        if _PROFANITY_FILTER.count:
            console.info(f"[弹幕] 脏话词库已加载：{_PROFANITY_FILTER.count} 条，命中即过滤")
    return _PROFANITY_FILTER


def _is_allowed_avatar_url(url: str) -> bool:
    """严格 host 校验：仅允许 B 站图床域。

    原实现 `"hdslb.com" in url` 会被 "https://evil.com/?ref=hdslb.com" 绕过，
    导致本地 SSE 服务器被当成开放代理（SSRF）。
    """
    if not url or not url.startswith("http"):
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _ALLOWED_AVATAR_HOSTS


class _Broadcaster:
    """线程安全弹幕广播（SSE 服务器线程 ⇄ 弹幕事件循环线程）。

    history 保存最近 _MAX_HISTORY 条；每条带单调递增 seq，
    SSE 连接靠 seq 判断增量（不能按列表下标，否则 history
    截断后 `history[index:]` 永远为空，新弹幕就再也收不到了）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._history: List[Tuple[int, dict]] = []  # (seq, msg)
        self._seq = 0

    def push(self, msg: dict) -> None:
        with self._cond:
            self._seq += 1
            self._history.append((self._seq, msg))
            if len(self._history) > _MAX_HISTORY:
                del self._history[:-_MAX_HISTORY]
            self._cond.notify_all()

    def consume(self) -> Tuple[List[Tuple[int, dict]], threading.Condition]:
        """返回 (历史列表, Condition)，供 SSE 线程循环读取。"""
        return self._history, self._cond


def _make_session() -> aiohttp.ClientSession:
    """创建带 SESSDATA Cookie 的 aiohttp 会话（不填也可连接，用户名会打码）。"""
    session = aiohttp.ClientSession()
    sessdata = config.cfg.BILI_SESSDATA
    if sessdata:
        cookie = http.cookies.SimpleCookie()
        cookie["SESSDATA"] = sessdata
        cookie["SESSDATA"]["domain"] = "bilibili.com"
        session.cookie_jar.update_cookies(cookie)
    return session


class _AvatarImageCache:
    """头像图片字节缓存（URL → (content_type, bytes)），跨线程 LRU。

    HTTP 线程与弹幕事件循环线程共享；代理命中时直接回字节，
    不再每次请求都去 B 站拉图（头像加载慢的根因）。
    """

    def __init__(self, maxsize: int = _AVATAR_CACHE_MAX):
        self._lock = threading.Lock()
        self._cache: "OrderedDict[str, Tuple[str, bytes]]" = OrderedDict()
        self._maxsize = maxsize

    def get(self, url: str) -> Optional[Tuple[str, bytes]]:
        with self._lock:
            item = self._cache.get(url)
            if item is not None:
                self._cache.move_to_end(url)
            return item

    def put(self, url: str, ctype: str, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._cache[url] = (ctype, data)
            self._cache.move_to_end(url)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)


class _AvatarResolver:
    """uid → 头像 URL：card API 异步补全（LRU 缓存 + 进行中去重）。

    web 端弹幕包通常不含头像字段，需按 uid 调
    https://api.bilibili.com/x/web-interface/card 补全。
    只缓存成功的非空结果，失败下次再试；同一 uid 并发只发一次请求。
    """

    def __init__(self, session: aiohttp.ClientSession,
                 image_cache: _AvatarImageCache,
                 maxsize: int = _AVATAR_CACHE_MAX):
        self._session = session
        self._images = image_cache
        self._cache: "OrderedDict[int, str]" = OrderedDict()
        self._maxsize = maxsize
        self._inflight: dict = {}
        self._inflight_img: dict = {}

    def get(self, uid: int) -> str:
        return self._cache.get(uid, "")

    async def prefetch(self, url: str) -> None:
        """把头像图片字节拉进缓存（幂等：已有/进行中跳过）。

        弹幕带 face 或 card API 补全拿到 URL 后调用，
        让浏览器请求代理时命中缓存、秒开头像。
        """
        if not url or self._images.get(url) is not None:
            return
        if self._inflight_img.get(url):
            return
        future = asyncio.ensure_future(self._do_prefetch(url))
        self._inflight_img[url] = future
        try:
            await future
        finally:
            self._inflight_img.pop(url, None)

    async def _do_prefetch(self, url: str) -> None:
        try:
            async with self._session.get(
                    url,
                    headers={"User-Agent": _UA,
                             "Referer": "https://live.bilibili.com/"}) as resp:
                data = await resp.read()
                ctype = resp.headers.get("Content-Type",
                                         "application/octet-stream")
            if data:
                self._images.put(url, ctype, data)
        except Exception:
            pass  # 预取失败不阻断主流程，代理兜底再拉

    async def fetch(self, uid: int) -> str:
        """取头像（带缓存）。"""
        if uid in self._cache:
            return self._cache[uid]
        inflight = self._inflight.get(uid)
        if inflight is not None:
            try:
                return await asyncio.shield(inflight)
            except Exception:
                return ""
        future = asyncio.ensure_future(self._do_fetch(uid))
        self._inflight[uid] = future
        try:
            return await future
        finally:
            self._inflight.pop(uid, None)

    async def _do_fetch(self, uid: int) -> str:
        face = ""
        try:
            async with self._session.get(
                    "https://api.bilibili.com/x/web-interface/card",
                    params={"mid": uid},
                    headers={"User-Agent": _UA,
                             "Referer": "https://live.bilibili.com/"}) as resp:
                payload = await resp.json(content_type=None)
            data = payload.get("data") or {}
            face = (data.get("card") or {}).get("face") or ""
        except Exception:
            face = ""
        if face:
            self._cache[uid] = face
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)
        return face


class _BiliDanmakuClient(blivedm.BLiveClient):
    """blivedm 客户端：连接/断开状态 → SSE。"""

    def __init__(self, room_id: int, broadcaster: _Broadcaster, **kwargs):
        super().__init__(room_id, **kwargs)
        self._broadcaster = broadcaster

    async def _on_ws_connect(self):
        """每次成功建立 WebSocket 连接时调用（鉴权前）。"""
        await super()._on_ws_connect()
        self._broadcaster.push({"type": "status", "connected": True})

    async def _on_ws_close(self):
        """连接断开时调用。"""
        await super()._on_ws_close()
        self._broadcaster.push({"type": "status", "connected": False})


class _BiliDanmakuHandler(blivedm.BaseHandler):
    """blivedm 消息处理器：事件 → SSE 广播。

    注意：blivedm 的 handle() 是同步分发（不 await 协程），
    因此这里必须用同步 def，不能写 async def（否则弹幕会被丢弃）。
    头像补全则通过后台任务（create_task）异步完成。
    """

    def __init__(self, broadcaster: _Broadcaster,
                 avatars: _AvatarResolver):
        super().__init__()
        self._b = broadcaster
        self._avatars = avatars

    @staticmethod
    def _proxy_avatar(url: str) -> str:
        """头像 URL 走本地代理：B 站图床防盗链，浏览器直连会被 403。

        本地服务器用 B 站 Referer 拉图后转发给浏览器，同源加载无防盗链问题。
        """
        if not url or url.startswith("/avatar?"):
            return url
        return (f"http://127.0.0.1:{config.cfg.BILI_SERVER_PORT}"
                f"/avatar?u={quote(url, safe='')}")

    def _push(self, username: str, text: str, avatar: str = "",
              uid: int = 0) -> None:
        """推送到 SSE（字段与气泡网页约定一致：用户名/弹幕/头像）。

        头像为空且 uid 有效时，后台调 card API 补全，完成后推
        avatar 事件由网页就地更新气泡头像。
        """
        # 弹幕内容仅推 SSE 展示（网页左侧实时流 + 精选回复卡片），不在控制台
        # 显示——被选中回复的弹幕由主程序 _chat_danmaku 在左栏「对话」显示
        self._b.push({
            "type": "danmaku",
            "username": username,
            "text": text,
            "avatar": self._proxy_avatar(avatar),
            "uid": uid,
        })
        if not avatar and uid:
            self._schedule_avatar(uid)

    def _schedule_avatar(self, uid: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._fetch_and_push_avatar(uid))

    async def _fetch_and_push_avatar(self, uid: int) -> None:
        url = await self._avatars.fetch(uid)
        if url:
            # 预取图片字节，浏览器请求代理时命中缓存秒回
            await self._avatars.prefetch(url)
            self._b.push({"type": "avatar", "uid": uid,
                          "avatar": self._proxy_avatar(url)})

    def _on_danmaku(self, client, message: web_models.DanmakuMessage):
        text = message.msg
        if not text:
            return
        # 过滤带方括号的弹幕：B 站指令/红包/投票等特殊弹幕正文多为
        # [xxx] 占位或装饰格式，无实质内容，直接丢弃（不推送也不显示）
        if "[" in text or "]" in text:
            return
        # 脏话过滤（data/profanity.txt，参考 AI-Vtuber badwords）：命中即丢弃
        if _get_profanity_filter().has_hit(text):
            return
        # 表情包弹幕：内容为占位文本
        if message.dm_type == 1 and message.emoticon_options_dict.get("url"):
            text = f"[表情] {text}"
        # 相同弹幕去重：去重窗口内（_DEDUP_WINDOW_S 秒）同内容只展示/回复一次，
        # 防刷屏（"666"、"主播唱首歌"连刷）导致弹幕墙刷屏、AI 重复回复
        now = time.time()
        last = _RECENT_DANMAKU.get(text)
        if last is not None and now - last < _DEDUP_WINDOW_S:
            return
        _RECENT_DANMAKU[text] = now
        _RECENT_DANMAKU.move_to_end(text)
        if len(_RECENT_DANMAKU) > _RECENT_DANMAKU_MAX:
            _RECENT_DANMAKU.popitem(last=False)
        uid = message.uid or 0
        uname = message.uname or "匿名"
        avatar = message.face or self._avatars.get(uid)
        if avatar:
            # 弹幕自带 face：后台预取图片字节 + 推 avatar 事件，
            # 让精选回复卡片/左侧实时流按 uid 补上头像
            self._schedule_prefetch(avatar)
            self._b.push({"type": "avatar", "uid": uid,
                          "avatar": self._proxy_avatar(avatar)})
        self._push(uname, text, avatar, uid)
        # 交给弹幕挑选器：兴趣度评分 → 排队 → 挑"有趣"的让 AI 回复
        picker = _get_danmaku_picker()
        if picker is not None:
            picker.submit(uid=uid, username=uname, text=text)

    def _schedule_prefetch(self, url: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._avatars.prefetch(url))


async def _run_bili(broadcaster: _Broadcaster, room_id: int,
                    avatar_images: _AvatarImageCache) -> None:
    """blivedm 客户端：内部自动重连；异常退出后重建客户端。"""
    while True:
        session = _make_session()          # blivedm 连接用
        avatar_session = _make_session()   # 头像补全用（同带 SESSDATA）
        client: Optional[_BiliDanmakuClient] = None
        try:
            client = _BiliDanmakuClient(
                room_id, broadcaster=broadcaster, session=session)
            client.set_handler(_BiliDanmakuHandler(
                broadcaster, _AvatarResolver(avatar_session, avatar_images)))
            client.start()
            await client.join()  # 阻塞直到客户端停止（内部断线自动重连）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            console.error(f"[弹幕] 连接异常：{type(e).__name__}: {e}")
            await asyncio.sleep(_RECONNECT_S)
        finally:
            if client is not None:
                try:
                    await client.stop_and_close()
                except Exception:
                    pass
            await session.close()
            await avatar_session.close()


def _bili_loop(broadcaster: _Broadcaster, room_id: int,
               avatar_images: _AvatarImageCache) -> None:
    """独立线程：跑 blivedm 客户端的 asyncio 事件循环。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_bili(broadcaster, room_id, avatar_images))
    except Exception as e:
        console.error(f"[弹幕] 事件循环异常退出：{type(e).__name__}: {e}")
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()


class BiliService:
    """单直播间弹幕连接：blivedm 客户端线程 + 弹幕广播。

    只负责本房间的 blivedm 连接与 SSE 广播数据；
    HTTP 服务器与头像缓存由 BiliServiceManager 统一管理。
    """

    def __init__(self, room_id: int, broadcaster: _Broadcaster,
                 avatar_images: _AvatarImageCache) -> None:
        self.room_id = room_id
        self.broadcaster = broadcaster
        self.avatar_images = avatar_images
        self._bili_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._bili_thread is not None and self._bili_thread.is_alive():
            return
        self._bili_thread = threading.Thread(
            target=_bili_loop,
            args=(self.broadcaster, self.room_id, self.avatar_images),
            daemon=True)
        self._bili_thread.start()

    def stop(self) -> None:
        # blivedm 线程为 daemon，随进程退出自动结束（与原实现一致）
        self._bili_thread = None


class BiliServiceManager:
    """多直播间弹幕服务：一个 SSE 服务器 + 每房间一个 blivedm 连接。

    - 每个房间一个 BiliService（独立 broadcaster，弹幕互不串流）；
    - 头像字节缓存全局共享（同一批用户跨房间复用）；
    - SSE 网页 /events?room_id=X 订阅指定房间；不带 room_id 默认主房间（第一个），
      单房间用法与原实现完全一致。
    """

    def __init__(self, room_ids: List[int], port: int, html_path: str) -> None:
        self.html_path = html_path
        self.avatar_images = _AvatarImageCache()  # 全局共享，HTTP 线程与弹幕线程共用
        self._services: List[BiliService] = []
        self._by_room: Dict[int, BiliService] = {}
        for room_id in room_ids:
            svc = BiliService(room_id, _Broadcaster(), self.avatar_images)
            self._services.append(svc)
            self._by_room[room_id] = svc
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def broadcaster(self) -> _Broadcaster:
        """主房间（第一个）广播：单房间调用方无感知。"""
        return self._services[0].broadcaster

    def broadcaster_for(self, room_id: int) -> Optional[_Broadcaster]:
        """指定房间的弹幕广播；房间不存在返回 None（由调用方退回主房间）。"""
        svc = self._by_room.get(room_id)
        return svc.broadcaster if svc is not None else None

    def broadcast(self, msg: dict) -> None:
        """推给所有房间（如精选回复卡片跨房间可见）。"""
        for svc in self._services:
            svc.broadcaster.push(msg)

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
        if not _is_allowed_avatar_url(url):
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


# ===== 弹幕回复挑选器 =====
# 弹幕 AI 回复总开关（不接主程序时为 None，不触发任何挑选逻辑）
_DANMAKU_PICKER: "Optional[DanmakuPicker]" = None


def set_danmaku_picker(picker: "Optional[DanmakuPicker]") -> None:
    """主程序调用：把挑选器注入进来；传 None 则停用弹幕自动回复。"""
    global _DANMAKU_PICKER
    _DANMAKU_PICKER = picker


def _get_danmaku_picker() -> "Optional[DanmakuPicker]":
    return _DANMAKU_PICKER


# 高兴趣关键词：直接拉高分（点名 / 提问 / 感叹 / 情绪浓）
_HOT_TOKENS = [
    # 点名主播："主播"、"未可飞"、"肥牛"、"E.V" 之类
    "主播", "up", "UP", "喂", "在吗", "在不在", "听得到", "看得见",
    # 疑问词：大概率需要回应
    "吗", "呢", "？", "?", "怎么", "为什么", "什么", "哪", "谁", "几",
    # 感叹与情绪浓
    "！", "!", "哇", "草", "绝", "神", "笑死", "好可爱", "好帅",
    "喜欢", "爱", "想你", "亲",
]

# 低兴趣特征：直接丢分或跳过
_SKIP_PREFIXES = ("[表情]",)
_BORING_TOKENS = ("666", "233", "hhh", "哈哈哈", "哈哈", "来了", "打卡",
                  "签到", "1", "2", "3", "a", "s", "d", "f", "q", "w",
                  "e", "r", "t", "y", "。。", "..")
_BORING_RE = re.compile(r"^(哈|啊|哦|嗯|6|2|h|w)+$")
# 实质内容检测：含中英文/数字/假名才算有内容；纯标点符号（"？？？"、
# "！！！"、"。。。"）无内容，不值得回
_HAS_CONTENT_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 回复间隔（与 ProactiveEngine 解耦，独立控制弹幕节奏）
# 冷却初始值（秒）；每次回复后从共用随机间隔范围
# （config.RESPONSE_INTERVAL_MIN~MAX，与主动对话同一范围）滚动取值，
# 避免固定 20s 节奏显得机械。
_DEFAULT_MIN_GAP_S = 20      # 两次弹幕回复最少间隔（初始/兜底）
_DEFAULT_MAX_GAP_S = 30      # 最长 30 秒不回就挑当前最好的（保底防冷场）
_DEFAULT_WINDOW_S = 8        # 窗口 8 秒：弹幕到齐后比较，挑分最高的回
_DEFAULT_MIN_SCORE = 20      # 低于此分的弹幕直接丢弃（太水不值得回）
_POOL_CAP = 50               # 候选池上限：溢出时淘汰分数最低的候选
_BATCH_SCORE_DELTA = 15      # 批量回复：与最高分差 ≤ 此值的候选可合并一次回复
_BATCH_MAX_ITEMS = 3         # 批量回复最多聚合条数（含最高分那条）


class DanmakuPicker:
    """弹幕挑选器：不是每条都回、也不是固定时间回，选"当前最有趣的"。

    工作原理：
    1. 每条弹幕进来 → 做「兴趣度评分」（0~100 分），低于 MIN_SCORE 直接丢；
    2. 高于门槛的进「候选池」（优先级堆，上限 _POOL_CAP 条）：
       - 取回复时 heappop 直接拿最高分（O(log n)，替代原来 O(n) 全池 max）；
       - 池满时踢「分数最低」的候选而非最早的——开播洪峰下高分弹幕
         不会因为"来得早"被挤出；
       - 同用户同内容不重复加；同用户不同内容只保留分数更高的那条
         （旧条目留在堆里作废，弹出时惰性跳过）。
    3. 等待 WINDOW 窗口时间（WINDOW_S，默认 8s）：让这一波弹幕都到齐再比；
       窗口里新到的高分弹幕能顶替旧的。
    4. 窗口结束 OR 保底到了最长间隔 MAX_GAP_S → 挑分最高的回复；
       高密度时把分数相近（≤ _BATCH_SCORE_DELTA）且同窗口内的候选一并取出，
       走 on_batch_reply_callback 聚合回复（如"观众 A 说 X，观众 B 说 Y"）。
    5. 调 on_reply_callback / on_batch_reply_callback 让主程序 AI 回复；
    6. 回复后进入冷却（MIN_GAP_S），冷却内新弹幕继续评分入池但不触发回复。

    这样效果：
    - 密集弹幕（开播/爆热）：8s 窗口挑最好的 1~3 条一次回复，20~30s 间隔，不刷屏；
    - 稀疏弹幕（冷场）：只要有一条够有趣（≥20 分）就直接等窗口结束回复；
    - 水弹幕（"666"、"哈哈哈哈"）：评分 0~5 分被丢，AI 根本不会理。
    """

    def __init__(
        self,
        on_reply_callback: Callable[[int, str, str], None],
        on_batch_reply_callback: Optional[
            Callable[[List[Tuple[int, str, str]]], None]] = None,
        min_gap_s: int = _DEFAULT_MIN_GAP_S,
        max_gap_s: int = _DEFAULT_MAX_GAP_S,
        window_s: int = _DEFAULT_WINDOW_S,
        min_score: int = _DEFAULT_MIN_SCORE,
    ) -> None:
        if on_reply_callback is None:
            raise ValueError("on_reply_callback 不能为 None")
        self._cb: Callable[[int, str, str], None] = on_reply_callback
        self._batch_cb: Optional[
            Callable[[List[Tuple[int, str, str]]], None]] = on_batch_reply_callback
        self._min_gap_s = min_gap_s
        self._max_gap_s = max_gap_s
        self._window_s = window_s
        self._min_score = min_score

        # 候选池：双优先级堆 + dict 索引（支持顶替与惰性删除）
        # _pool: key=(uid, text) → 当前条目 (score, uid, username, text, received_at, seq)
        # _max_heap: [(-score, -seq, key)]  取回复时 heappop 拿最高分（同分新的优先）
        # _min_heap: [(score, seq, key)]    池满时 heappop 踢最低分（同分旧的优先）
        # 顶替语义：同 key 更高分提交 → 新条目入双堆，旧堆元素作废（弹出时按 seq 校验跳过）
        self._pool: dict = {}
        self._max_heap: list = []
        self._min_heap: list = []
        self._seq = 0
        self._lock = threading.Lock()

        self._last_reply_at = 0.0            # 上次真正回复的时间戳
        self._window_started_at: Optional[float] = None  # 当前窗口开始时间
        self._window_timer: Optional[threading.Timer] = None
        self._max_gap_timer: Optional[threading.Timer] = None

    # ---- 外部接口 ----

    def submit(self, uid: int, username: str, text: str) -> None:
        """从弹幕处理器提交一条候选。内部会评分、入池、启窗口。"""
        if text is None:
            return
        # 表情包/极短水弹幕：直接跳过
        stripped = text.strip()
        if not stripped or stripped.startswith(_SKIP_PREFIXES):
            return
        score = self._score(stripped)
        if score < self._min_score:
            return
        received_at = time.time()
        key = (uid, stripped)
        with self._lock:
            # 同用户同内容不重复加；同用户不同内容只保留分数更高的那条
            old = self._pool.get(key)
            if old is not None and old[0] >= score:
                return
            self._seq += 1
            entry = (score, uid, username, stripped, received_at, self._seq)
            self._pool[key] = entry
            heapq.heappush(self._max_heap, (-score, -self._seq, key))
            heapq.heappush(self._min_heap, (score, self._seq, key))
            # 被顶替的作废堆元素不主动删（惰性）；作废元素过多时按有效候选重建
            if len(self._max_heap) > len(self._pool) + _POOL_CAP:
                self._rebuild_heaps_locked()
            # 溢出（超过上限）：淘汰分数最低的候选，避免洪峰把高分弹幕挤出
            self._evict_overflow_locked()
            # 启 8s 窗口：等一等后面有没有更有趣的
            if self._window_started_at is None:
                self._window_started_at = received_at
                self._schedule_window_end(received_at + self._window_s)
            # 启保底定时器（最长 max_gap 秒不回就硬挑一次）；只在没有挂起时才启
            if self._max_gap_timer is None:
                self._schedule_max_gap_end(received_at + self._max_gap_s)

    def stop(self) -> None:
        with self._lock:
            for t in (self._window_timer, self._max_gap_timer):
                if t is not None:
                    try:
                        t.cancel()
                    except Exception:
                        pass
            self._window_timer = None
            self._max_gap_timer = None

    # ---- 评分：兴趣度打分 ----

    @staticmethod
    def _score(text: str) -> int:
        """弹幕兴趣度评分（0~100）。分低不值得回。"""
        # 单字符 / 纯重复灌水 → 0 分
        if len(text) <= 1 or _BORING_RE.match(text):
            return 0
        if any(tok == text for tok in _BORING_TOKENS):
            return 0
        # 纯标点/符号（"？？？"、"！！！"）：无实质内容，直接 0 分。
        # 否则 "？" 在 _HOT_TOKENS 里会按"提问"狂加分（基础 10 + 12 + 3），
        # 一条空标点反而越过门槛被精选。
        if not _HAS_CONTENT_RE.search(text):
            return 0

        score = 10  # 基础分：过了上面两道门就算 10 分起

        # 长度加分：有内容（>8 字）的弹幕通常是真的在说话
        if len(text) >= 15:
            score += 15
        elif len(text) >= 8:
            score += 8

        # 高兴趣关键词命中（每条 +12，最高 +36）
        hot_hits = 0
        for tok in _HOT_TOKENS:
            if tok in text:
                hot_hits += 1
                if hot_hits >= 3:
                    break
        score += hot_hits * 12

        # 结尾标点暗示
        if text.endswith(("？", "?")):
            score += 3  # 提问强烈加分
        elif text.endswith(("！", "!")):
            score += 2
        elif text.endswith(("~", "～")):
            score += 1

        # 纯灌水词命中倒扣分
        boring_hits = sum(1 for tok in _BORING_TOKENS if tok in text)
        score -= boring_hits * 8

        return max(0, min(100, score))

    # ---- 优先级堆内部操作 ----

    def _rebuild_heaps_locked(self) -> None:
        """作废堆元素过多时按当前有效候选重建双堆（防惰性删除堆积膨胀）。"""
        self._max_heap = [(-e[0], -e[5], key) for key, e in self._pool.items()]
        self._min_heap = [(e[0], e[5], key) for key, e in self._pool.items()]
        heapq.heapify(self._max_heap)
        heapq.heapify(self._min_heap)

    def _evict_overflow_locked(self) -> None:
        """候选池超上限：从最小堆踢掉分数最低的候选（跳过被顶替的作废堆元素）。"""
        while len(self._pool) > _POOL_CAP and self._min_heap:
            score, seq, key = heapq.heappop(self._min_heap)
            entry = self._pool.get(key)
            # 堆顶可能是被更高分顶替的作废元素（分数/序号对不上），跳过
            if entry is None or entry[0] != score or entry[5] != seq:
                continue
            del self._pool[key]

    def _pop_best_locked(self) -> Optional[tuple]:
        """从候选池按分数降序取最高分 1 条，取完移走；顺手清理过期候选。"""
        while self._max_heap:
            neg_score, neg_seq, key = heapq.heappop(self._max_heap)
            entry = self._pool.get(key)
            if entry is None or entry[0] != -neg_score or entry[5] != -neg_seq:
                continue  # 已被顶替的作废堆元素
            del self._pool[key]
            self._cleanup_stale_locked()
            return entry
        self._pool.clear()  # 堆空仍有残留（理论上不会）：清空兜底
        return None

    def _cleanup_stale_locked(self) -> None:
        """取走后清理超过 max_gap*2 还没被看上的过期候选（堆里对应元素弹出时作废）。"""
        now = time.time()
        for key in list(self._pool):
            if now - self._pool[key][4] > self._max_gap_s * 2:
                del self._pool[key]

    def _pop_reply_batch_locked(self) -> Optional[List[tuple]]:
        """取最高分弹幕；高密度时把分数相近（≤ _BATCH_SCORE_DELTA）且
        同窗口内的候选一并取出，组成一次批量回复（最多 _BATCH_MAX_ITEMS 条）。
        """
        best = self._pop_best_locked()
        if best is None:
            return None
        if self._batch_cb is None or not self._pool:
            return [best]
        batch = [best]
        best_score = best[0]
        now = time.time()
        # 补充候选：分数与最高分相近、且仍在当前窗口内（同一波弹幕）
        peers = [
            v for v in self._pool.values()
            if v[0] >= best_score - _BATCH_SCORE_DELTA
            and now - v[4] <= self._window_s
        ]
        peers.sort(key=lambda v: v[0], reverse=True)
        for peer in peers[:_BATCH_MAX_ITEMS - 1]:
            self._pool.pop((peer[1], peer[3]))
            batch.append(peer)
        return batch

    # ---- 调度：窗口结束 / 保底超时触发选优回复 ----

    def _schedule_window_end(self, fire_at: float) -> None:
        delay = max(0.0, fire_at - time.time())
        t = threading.Timer(delay, self._on_window_end)
        t.daemon = True
        t.start()
        self._window_timer = t

    def _schedule_max_gap_end(self, fire_at: float) -> None:
        delay = max(0.0, fire_at - time.time())
        t = threading.Timer(delay, self._on_max_gap_end)
        t.daemon = True
        t.start()
        self._max_gap_timer = t

    def _fire_reply(self, batch: List[tuple], label: str) -> None:
        """把选中的弹幕批次交给回调：单条走 on_reply_callback，多条走 on_batch_reply_callback。

        label：触发来源（窗口结束「精选回复」/ 最长间隔「保底回复」）。
        """
        try:
            if len(batch) == 1:
                _, uid, username, text, _, _ = batch[0]
                console.dim(f"[弹幕] {label} {username}")
                self._cb(uid, username, text)
            else:
                names = "、".join(item[2] for item in batch)
                console.dim(f"[弹幕] 批量回复 {names}")
                self._batch_cb(
                    [(item[1], item[2], item[3]) for item in batch])
        except Exception as e:
            console.error(f"[弹幕] 回调异常：{type(e).__name__}: {e}")

    def _on_window_end(self) -> None:
        """窗口结束：如果冷却已过，就从候选池挑最好的回复。"""
        with self._lock:
            self._window_timer = None
            self._window_started_at = None
            if self._in_cooldown_locked():
                return
            batch = self._pop_reply_batch_locked()
            if batch is None:
                return
            # 立刻把两个定时器都关掉（这次要回复了）
            if self._max_gap_timer is not None:
                try:
                    self._max_gap_timer.cancel()
                except Exception:
                    pass
                self._max_gap_timer = None
            self._last_reply_at = time.time()
            self._roll_cooldown()
        self._fire_reply(batch, "精选回复")

    def _on_max_gap_end(self) -> None:
        """保底：到了最长间隔还没窗口触发，硬挑当前最好的。"""
        with self._lock:
            self._max_gap_timer = None
            if self._in_cooldown_locked():
                # 冷却中：下次再等一个 max_gap
                self._schedule_max_gap_end(time.time() + self._max_gap_s)
                return
            batch = self._pop_reply_batch_locked()
            if batch is None:
                return
            if self._window_timer is not None:
                try:
                    self._window_timer.cancel()
                except Exception:
                    pass
                self._window_timer = None
                self._window_started_at = None
            self._last_reply_at = time.time()
            self._roll_cooldown()
        self._fire_reply(batch, "保底回复")

    def _roll_cooldown(self) -> None:
        """回复后滚动冷却时长：与主动对话共用随机间隔范围，避免固定节奏。"""
        self._min_gap_s = random.uniform(
            config.cfg.RESPONSE_INTERVAL_MIN, config.cfg.RESPONSE_INTERVAL_MAX)

    def _in_cooldown_locked(self) -> bool:
        return (time.time() - self._last_reply_at) < self._min_gap_s


if __name__ == "__main__":
    cfg = config.cfg
    room_ids = cfg.BILI_ROOM_IDS or ([cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
    if not room_ids:
        console.warn("[弹幕] 未配置房间号（BILI_ROOM_ID/BILI_ROOM_IDS），服务未启动")
    else:
        manager = BiliServiceManager(
            room_ids, cfg.BILI_SERVER_PORT,
            os.path.join(cfg.PROJECT_ROOT, "ui", "弹幕卡片.html"))
        manager.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            manager.stop()