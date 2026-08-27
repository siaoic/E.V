"""blivedm 客户端 + 消息处理 + 线程事件循环。

单职责：把 B 站 WebSocket 弹幕（blivedm 库）解出来，
经过脏话/方括号/同内容去重过滤后推到 SSE Broadcaster + 投给 DanmakuPicker。

依赖注入：
- 构造时接收 broadcaster（Broadcaster）、room_id、avatar_images（共享缓存）
- 全局 set_danmaku_picker() 注入 picker（避免 client ⇄ picker 循环引用）
- 弹幕去重缓存为进程级单例（同内容窗口内只展示一次）

注意：blivedm 的 handle() 是同步分发（不 await 协程），
因此 _BiliDanmakuHandler 的回调必须用同步 def，不能写 async def
（否则弹幕会被丢弃）。异步的部分（头像补全）通过 create_task 后台跑。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import quote

import blivedm
import blivedm.models.web as web_models

from ev.danmaku.avatar import (
    AvatarImageCache, AvatarResolver, make_session,
)
from ev.danmaku.broadcaster import Broadcaster
from ev.danmaku.picker import get_danmaku_picker
from ev.utils import config
from ev.utils import console
from ev.utils.profanity_filter import ProfanityFilter


# blivedm 库内部用标准 logging 输出 WARNING/INFO（如 unknown cmd 会打一条超长
# protobuf 日志）。控制台不显示弹幕信息：只保留 ERROR 级以上诊断。
logging.getLogger("blivedm").setLevel(logging.ERROR)


# ===== 弹幕去重（与挑选器无关，单条弹幕是否值得展示） =====

_DEDUP_WINDOW_S = 300      # 相同弹幕去重窗口（秒）：窗口内同内容只展示/回复一次
_RECENT_DANMAKU_MAX = 500  # 相同弹幕去重缓存条数上限（防无限膨胀）

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


# ===== blivedm 客户端 =====

class _BiliDanmakuClient(blivedm.BLiveClient):
    """blivedm 客户端：连接/断开状态 → SSE。"""

    def __init__(self, room_id: int, broadcaster: Broadcaster, **kwargs):
        super().__init__(room_id, **kwargs)
        self._broadcaster = broadcaster

    async def _on_ws_connect(self):
        """每次成功建立 WebSocket 连接时调用（鉴权前）。"""
        await super()._on_ws_connect()
        self._broadcaster.push({"type": "status", "connected": True})
        # 每 30 秒重推一次 status，保证新 SSE 订阅者/断线重连者能尽快拿到连接状态
        def _repush(loop):
            import asyncio
            try:
                loop.create_task(self.__repush_status_loop())
            except Exception:
                pass
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            loop = None
        if loop and loop.is_running() and not getattr(self, '_repush_status_scheduled', False):
            self._repush_status_scheduled = True
            # 不阻塞 _on_ws_connect：用 call_soon 排一个常驻协程
            loop.call_soon(lambda: _repush(loop))

    async def __repush_status_loop(self):
        import asyncio
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    self._broadcaster.push({"type": "status", "connected": True})
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise

    async def _on_ws_close(self):
        """连接断开时调用。"""
        await super()._on_ws_close()
        self._broadcaster.push({"type": "status", "connected": False})


# ===== 消息处理器 =====

class _BiliDanmakuHandler(blivedm.BaseHandler):
    """blivedm 消息处理器：事件 → SSE 广播 + 投给 picker。

    依赖注入（构造时）：
    - broadcaster：SSE 消息总线
    - avatars：uid → face URL + 字节缓存解析器
    """

    def __init__(self, broadcaster: Broadcaster, avatars: AvatarResolver):
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
              uid: int = 0, privilege_type: int = 0) -> None:
        """推送到 SSE（字段与气泡网页约定一致：用户名/弹幕/头像）。

        头像为空且 uid 有效时，后台调 card API 补全，完成后推
        avatar 事件由网页就地更新气泡头像。
        privilege_type：0 普通观众，1 总督，2 提督，3 舰长；用于前端渲染会员边框。
        """
        # 弹幕内容仅推 SSE 展示（网页左侧实时流 + 精选回复卡片），不在控制台
        # 显示——被选中回复的弹幕由主程序 _chat_danmaku 在左栏「对话」显示
        self._b.push({
            "type": "danmaku",
            "username": username,
            "text": text,
            "avatar": self._proxy_avatar(avatar),
            "uid": uid,
            "privilege_type": privilege_type,
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
        priv = message.privilege_type or 0
        self._push(uname, text, avatar, uid, priv)
        # 交给弹幕挑选器：兴趣度评分 → 排队 → 挑"有趣"的让 AI 回复
        picker = get_danmaku_picker()
        if picker is not None:
            picker.submit(uid=uid, username=uname, text=text)

    # ===== SC 醒目留言（paid 类型） =====
    def _on_super_chat(self, client, message: web_models.SuperChatMessage):
        uid = message.uid or 0
        uname = message.uname or "匿名"
        avatar = message.face or self._avatars.get(uid)
        text = message.message or ""
        price = message.price or 0
        bg_color = message.background_bottom_color or ""

        if avatar:
            self._schedule_prefetch(avatar)
            self._b.push({
                "type": "avatar",
                "uid": uid,
                "avatar": self._proxy_avatar(avatar),
            })
        self._b.push({
            "type": "superchat",
            "uid": uid,
            "username": uname,
            "text": text,
            "avatar": self._proxy_avatar(avatar) if avatar else "",
            "price": price,
            "amount": f"￥{price}",
            "bg_color": bg_color,
        })
        if not avatar and uid:
            self._schedule_avatar(uid)

    # ===== 大航海入列（crew 类型，用 UserToastV2 带附言） =====
    def _on_user_toast_v2(self, client, message: web_models.UserToastV2Message):
        # source=2 是系统补发的"赠送"副本，官方评论栏也不显示，直接跳过去重
        if message.source == 2:
            return
        uid = message.uid or 0
        uname = message.username or "匿名"
        level = message.guard_level or 0
        toast = message.toast_msg or ""
        avatar = self._avatars.get(uid) or ""

        self._b.push({
            "type": "guard",
            "uid": uid,
            "username": uname,
            "guard_level": level,
            "text": toast,
            "avatar": self._proxy_avatar(avatar) if avatar else "",
        })
        if not avatar and uid:
            self._schedule_avatar(uid)

    def _schedule_prefetch(self, url: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._avatars.prefetch(url))


# ===== 线程事件循环 =====

_RECONNECT_S = 5  # 客户端异常退出后的重建间隔（秒）


async def _run_bili(broadcaster: Broadcaster, room_id: int,
                    avatar_images: AvatarImageCache) -> None:
    """blivedm 客户端：内部自动重连；异常退出后重建客户端。"""
    while True:
        session = make_session()          # blivedm 连接用
        avatar_session = make_session()   # 头像补全用（同带 SESSDATA）
        client: Optional[_BiliDanmakuClient] = None
        try:
            client = _BiliDanmakuClient(
                room_id, broadcaster=broadcaster, session=session)
            client.set_handler(_BiliDanmakuHandler(
                broadcaster, AvatarResolver(avatar_session, avatar_images)))
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


def bili_loop(broadcaster: Broadcaster, room_id: int,
              avatar_images: AvatarImageCache) -> None:
    """独立线程：跑 blivedm 客户端的 asyncio 事件循环。

    线程入口函数，供 BiliService.set_client_starter() 注入后调用。
    """
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
