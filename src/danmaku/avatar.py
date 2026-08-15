"""B 站头像：URL 白名单 + 图片字节缓存 + uid → face URL 解析。

单职责：管理"头像相关"的两层缓存 + 一次性的远端拉取/补全。
- _AvatarImageCache: URL → (ctype, bytes) 跨线程 LRU
- _AvatarResolver: uid → face URL（按需调 card API 异步补全）
- _is_allowed_avatar_url: 严格 host 校验，防 SSRF

依赖：外部传入 aiohttp.ClientSession、_AvatarImageCache。
不关心弹幕来源、不关心广播到哪儿。
"""

from __future__ import annotations

import asyncio
import http.cookies
import threading
from collections import OrderedDict
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from src.utils import config
from src.utils import console


# 通用浏览器 UA：B 站服务端 UA 校验场景下避免 412
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 头像代理白名单：仅允许 B 站图床（hdslb.com）。
# 严格 host 校验避免 "https://evil.com/?ref=hdslb.com" 之类的 URL 绕过 → SSRF
_ALLOWED_AVATAR_HOSTS = {"hdslb.com", "www.hdslb.com", "i0.hdslb.com",
                         "i1.hdslb.com", "i2.hdslb.com"}

_AVATAR_CACHE_MAX = 1000   # 头像缓存条数上限（默认；构造可调）


def make_session() -> aiohttp.ClientSession:
    """创建带 SESSDATA Cookie 的 aiohttp 会话（不填也可连接，用户名会打码）。

    抽离为模块级函数：blivedm 连接用、头像补全用，两个 session 都要带
    SESSDATA 但相互独立（一个挂了不影响另一个）。
    """
    session = aiohttp.ClientSession()
    sessdata = config.cfg.BILI_SESSDATA
    if sessdata:
        cookie = http.cookies.SimpleCookie()
        cookie["SESSDATA"] = sessdata
        cookie["SESSDATA"]["domain"] = "bilibili.com"
        session.cookie_jar.update_cookies(cookie)
    return session


def is_allowed_avatar_url(url: str) -> bool:
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


class AvatarImageCache:
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


class AvatarResolver:
    """uid → 头像 URL：card API 异步补全（LRU 缓存 + 进行中去重）。

    web 端弹幕包通常不含头像字段，需按 uid 调
    https://api.bilibili.com/x/web-interface/card 补全。
    只缓存成功的非空结果，失败下次再试；同一 uid 并发只发一次请求。
    """

    def __init__(self, session: aiohttp.ClientSession,
                 image_cache: AvatarImageCache,
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
