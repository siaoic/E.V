"""直播间连接：blivedm 客户端封装。

只负责「连上去、把原始消息规范化后塞进队列、暴露连接状态」，
上报策略（哪些事件要报、用什么优先级）全部由插件决定，因此这一层不读插件配置。

产出两种东西：弹幕 / 醒目留言是 :class:`LiveChatMessage`（交给插件注入成聊天消息），
礼物 / 上舰 / 进场关注是 :class:`LiveEvent`（交给插件上报成世界事件）。

blivedm 的接收回调是同步分发的（在它自己的网络协程里），回调里不能做任何 await 工作，
所以这里只做纯计算 + ``put_nowait``。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

import blivedm
import blivedm.models.web as web_models

from .render import (
    LiveChatMessage,
    LiveEvent,
    describe_connection,
    render_danmaku,
    render_gift,
    render_guard,
    render_interact,
    render_super_chat,
    render_user_toast,
)

# GUARD_BUY 与 USER_TOAST_MSG_V2 会为同一次上舰各发一条消息，按 (uid, 舰队等级) 去重。
GUARD_DEDUPE_SECONDS = 10.0


class _LiveHandler(blivedm.BaseHandler):
    """把 blivedm 的各类消息转成 :class:`LiveEvent` / :class:`LiveChatMessage` 并交给客户端。

    注意：``_get_valid_method`` 会跳过「未被子类重写」的基类空实现，
    因此这里每重写一个 ``_on_xxx`` 就多接收一类事件，未重写的类型完全不解析、零开销。
    弹幕与醒目留言走聊天消息通道（本插件是 B 站直播的唯一入口），
    礼物 / 上舰 / 进场关注走世界事件通道。
    """

    def __init__(self, owner: "BiliDanmakuClient") -> None:
        self._owner = owner

    def _on_heartbeat(self, client: blivedm.BLiveClient, message: web_models.HeartbeatMessage) -> None:
        """心跳到达意味着连接确实建立了；人气值作为拥挤信号交给准入预算。"""

        self._owner.mark_connected()
        self._owner.observe_crowd(int(message.popularity or 0))

    def _on_danmaku(self, client: blivedm.BLiveClient, message: web_models.DanmakuMessage) -> None:
        """弹幕。``dm_type`` 为 0 是文本弹幕，其余是表情 / 语音，没有可读正文。"""

        if int(message.dm_type or 0) != 0:
            return
        self._owner.push_chat(render_danmaku(message))

    def _on_gift(self, client: blivedm.BLiveClient, message: web_models.GiftMessage) -> None:
        """礼物。"""

        self._owner.push(render_gift(message))

    def _on_buy_guard(self, client: blivedm.BLiveClient, message: web_models.GuardBuyMessage) -> None:
        """上舰（经典消息）。"""

        self._owner.push_guard(render_guard(message))

    def _on_user_toast_v2(self, client: blivedm.BLiveClient, message: web_models.UserToastV2Message) -> None:
        """上舰（新消息）。

        ``source == 2`` 是上舰时紧接着发的那条「赠送」重复通知，B 站官方评论栏也不显示，
        这里同样跳过；``source == 0`` 与 GUARD_BUY 的重复由 :meth:`BiliDanmakuClient.push_guard` 去重。
        """

        if int(message.source or 0) == 2:
            return
        self._owner.push_guard(render_user_toast(message))

    def _on_super_chat(self, client: blivedm.BLiveClient, message: web_models.SuperChatMessage) -> None:
        """醒目留言。"""

        self._owner.push_chat(render_super_chat(message))

    def _on_interact_word_v2(self, client: blivedm.BLiveClient, message: web_models.InteractWordV2Message) -> None:
        """进入直播间、关注主播等互动。"""

        self._owner.push(render_interact(message))

    def on_client_stopped(self, client: blivedm.BLiveClient, exception: Optional[Exception]) -> None:
        """连接停止（正常停止或异常断开）时更新状态。"""

        self._owner.mark_disconnected(exception)


class BiliDanmakuClient:
    """B 站直播间弹幕客户端。"""

    def __init__(
        self,
        *,
        room_id: int,
        sessdata: str,
        push_event: Any,
        logger: Any,
        on_crowd: Any = None,
    ) -> None:
        """初始化客户端。

        Args:
            room_id: B 站直播间房间号，可以是短号。
            sessdata: B 站账号 SESSDATA，留空则匿名连接。
            push_event: 接收 :class:`LiveEvent` / :class:`LiveChatMessage` 的可调用对象
                （同步、不得阻塞），通常是 ``Queue.put_nowait``。
            logger: 插件的 ``ctx.logger``。
            on_crowd: 心跳人气值回调 ``on_crowd(popularity: int)``（拥挤信号，可空）。
        """

        self._room_id = int(room_id)
        self._sessdata = str(sessdata or "")
        self._push_event = push_event
        self._logger = logger
        self._on_crowd = on_crowd

        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[blivedm.BLiveClient] = None

        self._connected = False
        self._logged_in_uid: Optional[int] = None
        self._last_error = ""
        self._guard_seen: Dict[Tuple[int, int], float] = {}

    @property
    def room_id(self) -> int:
        """目标直播间房间号。"""

        return self._room_id

    @property
    def status_text(self) -> str:
        """当前连接状态的中文描述。"""

        return describe_connection(
            connected=self._connected,
            logged_in_uid=self._logged_in_uid,
            last_error=self._last_error,
        )

    # ------------------------------------------------------------------ 状态（由接收回调驱动）

    def mark_connected(self) -> None:
        """标记连接已建立。"""

        self._connected = True
        self._last_error = ""

    def observe_crowd(self, popularity: int) -> None:
        """把心跳人气值交给拥挤信号回调（同步、不得阻塞）。"""

        if self._on_crowd is not None:
            try:
                self._on_crowd(int(popularity))
            except Exception as exc:  # noqa: BLE001 - 信号失败不影响连接
                self._logger.debug(f"拥挤信号回调失败: {exc!r}")

    def mark_disconnected(self, exception: Optional[Exception]) -> None:
        """标记连接已断开；``exception`` 为 ``None`` 表示是按请求正常停止。"""

        self._connected = False
        if exception is not None:
            self._last_error = f"{type(exception).__name__}: {exception}"
            self._logger.error(f"直播间连接断开：room={self._room_id} {self._last_error}")

    # ------------------------------------------------------------------ 事件投递（由接收回调驱动）

    def push(self, event: LiveEvent) -> None:
        """把一条规范化事件交给插件。"""

        self._push_event(event)

    def push_chat(self, message: LiveChatMessage) -> None:
        """把一条待注入的聊天消息交给插件；空正文直接丢弃。"""

        if not message.text.strip():
            return
        self._push_event(message)

    def push_guard(self, event: LiveEvent) -> None:
        """投递上舰事件，并对同一用户同一等级的重复消息去重。

        blivedm 会为一次上舰同时派发 GUARD_BUY 与 USER_TOAST_MSG_V2，
        两条消息内容重复，因此这里按 ``(uid, 舰队等级)`` 在短时间内只放行第一条。
        """

        key = (event.uid, int(event.payload.get("guard_level", 0)))
        now = time.monotonic()
        last_seen = self._guard_seen.get(key)
        if last_seen is not None and now - last_seen < GUARD_DEDUPE_SECONDS:
            return
        self._guard_seen[key] = now
        self._push_event(event)

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """建立连接并开始接收事件。

        Raises:
            RuntimeError: blivedm 初始化房间信息时抛出意外异常——这属于不可恢复的硬错误，
                必须让插件加载失败并把原因暴露出来，而不是假装连上了。
        """

        if self._sessdata:
            # session 级 cookie 不限定 domain，对 api.bilibili.com（登录校验）与弹幕服务器均生效
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                cookies={"SESSDATA": self._sessdata},
            )
        else:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        client = blivedm.BLiveClient(self._room_id, session=self._session)
        client.set_handler(_LiveHandler(self))

        # init_room 只会因网络抖动降级（返回 False），这里把降级结果喊出来；
        # 真正抛出的异常是硬错误，不吞。
        degraded = not await client.init_room()
        self._client = client
        self._logged_in_uid = client.uid or None

        if degraded:
            self._logger.error(
                f"直播间房间信息初始化降级，将使用默认弹幕服务器：room={self._room_id}"
                "（房间号可能无效，请核对 bilibili.room_id）"
            )
        if not self._logged_in_uid:
            self._logger.error(
                "弹幕连接未登录：弹幕用户 uid 将为 0 且昵称打码，"
                "请在插件配置 bilibili.sessdata 中填写有效的 SESSDATA"
            )

        client.start()
        self._logger.info(f"开始监听 B 站直播间：room={self._room_id} uid={self._logged_in_uid or 0}")

    async def stop(self) -> None:
        """停止接收并释放连接资源。"""

        client = self._client
        self._client = None
        if client is not None:
            await client.stop_and_close()

        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

        self._connected = False
        self._logger.info(f"已停止监听 B 站直播间：room={self._room_id}")
