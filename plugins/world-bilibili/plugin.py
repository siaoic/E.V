"""B 站直播间世界插件（``maibot-live.world-bilibili``）。

本插件是 B 站直播的**唯一入口**：自己连 blivedm，把直播间消息分成两路处理——

- 弹幕与醒目留言：通过 ``message.inject_inbound`` 能力注入主程序的入站链路，
  与普通聊天消息完全同路（Hook、违禁词过滤、命令分发、落库、回复决策），
  插件不自己实现任何消息处理；
- 礼物 / 上舰 / 进场关注：作为世界事件与状态上报给世界框架基座，供 AI 感知直播间氛围；
- 麦麦的回复（出站）：由本插件注册的消息网关承接。回复的文字不回发直播间，
  观众的耳朵听到的是主程序播到本机扬声器的直播语音，因此这里只把回复记进日志。

醒目留言只走聊天消息这一路，金额写在正文标记里（``[SC ¥30] 内容``），不再额外上报
世界事件，避免同一句话被 AI 看到两遍。

上报策略：礼物 / 上舰是高价值互动，按 ``flush`` 立即投递并唤醒一轮；
进场关注在大房间会刷屏，默认 ``debounce``（只落上下文），可用配置改成 ``flush``。

状态快照走 ``world_poll``，由框架的变更轮询按 ``changed`` 取走，插件不主动推状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Dict, Literal, Optional, Union

from maibot_sdk import API, Field as PluginField, MaiBotPlugin, MessageGateway, PluginConfigBase
from pydantic import Field

from .bilibili_kit import BiliDanmakuClient, LiveChatMessage, LiveEvent, LiveEventKind, LiveRoomStats

# 主程序世界框架能力名（基座实现在 src/worlds/，插件通过能力通道调用）。
WORLD_REGISTER_CAPABILITY = "world.register"
WORLD_UNREGISTER_CAPABILITY = "world.unregister"
WORLD_EVENT_CAPABILITY = "world.event"

# 主程序入站消息注入能力（实现在 src/plugin_runtime/capabilities/data.py）。
CHAT_INJECT_CAPABILITY = "message.inject_inbound"

# 直播间聊天流的平台名与群标识：必须与主程序直播平台配置（config/bot_config.toml
# 的 worlds.live_platform）以及直播语音的 LIVE_PLATFORM 保持一致，
# 否则弹幕会落进另一个聊天流，AI 也就认不出这是直播。
LIVE_PLATFORM = "bilibili_live"
ROOM_GROUP_NAME_TEMPLATE = "B站直播间{room_id}"

# 承接 bilibili_live 出站的消息网关组件名（主程序按平台把回复投递到这里）。
SEND_GATEWAY_NAME = "bilibili_live_send"

# 世界插件按约定需要实现的公开 API 名（由基座通过插件运行时回呼）。
WORLD_POLL_API = "world_poll"
WORLD_OBSERVE_API = "world_observe"

# 每一类世界事件的交付语义。进场/关注由配置覆盖，见 BilibiliConfig.interact_trigger。
TRIGGER_BY_KIND: Dict[LiveEventKind, str] = {
    LiveEventKind.GIFT: "flush",
    LiveEventKind.GUARD: "flush",
}

# 消费队列里混放两种产出：世界事件（礼物 / 上舰 / 进场）与聊天消息（弹幕 / 醒目留言）。
# 共用一条队列是为了保住 blivedm 的到达顺序，避免两类消息在上下文里前后颠倒。
LiveItem = Union[LiveEvent, LiveChatMessage]

# 世界事件的交付语义取值（与框架基座的 WorldEventTrigger 对齐）
TriggerName = Literal["preempt", "flush", "debounce", "piggyback"]

DEFAULT_ENV_PROMPT = (
    "你正在 B 站直播，直播间里有观众在送礼、上舰和进出直播间；"
    "这些互动会作为世界事件与状态出现，弹幕与醒目留言则作为普通聊天消息出现。"
)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件通用配置。"""

    enabled: bool = PluginField(default=True, description="是否启用插件")
    config_version: str = PluginField(default="0.1.0", description="配置版本，由宿主用于配置迁移")


class BilibiliConfig(PluginConfigBase):
    """直播间世界的连接与世界参数。"""

    room_id: int = PluginField(
        default=0, ge=0, description="B 站直播间房间号（支持短号）；填 0 表示未配置，插件会拒绝加载"
    )
    sessdata: str = PluginField(
        default="",
        description="B 站账号 SESSDATA，用于获取真实用户身份；留空则匿名连接（部分事件的昵称会被打码）",
    )
    name: str = PluginField(default="bilibili", description="世界机器名，全局唯一，用于世界事件与状态归属")
    display_name: str = PluginField(default="B站直播间", description="世界显示名，出现在注入文本与事件前缀中")
    env_prompt: str = PluginField(
        default=DEFAULT_ENV_PROMPT, description="注入给模型的静态世界说明，只在世界集合变化时注入一次"
    )
    interact_trigger: TriggerName = PluginField(
        default="debounce",
        description=(
            "进场 / 关注的投递语义：flush 会立即唤醒一轮（适合小房间打招呼），"
            "debounce 只落上下文不唤醒（默认，避免大房间进场刷屏）"
        ),
    )
    recent_window_seconds: float = PluginField(
        default=180.0, ge=5.0, le=3600.0, description="状态快照里保留互动事件的时长（秒）"
    )


class WorldBilibiliConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


class WorldBilibiliPlugin(MaiBotPlugin):
    """B 站直播间世界插件入口。"""

    config_model = WorldBilibiliConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[BiliDanmakuClient] = None
        self._queue: Optional[asyncio.Queue[LiveItem]] = None
        self._consume_task: Optional[asyncio.Task[None]] = None
        self._stats: Optional[LiveRoomStats] = None
        self._registered_name = ""
        # 上次轮询报告出去的版本号；初值 -1 让首次轮询必定报告变化，把初始状态推给框架。
        self._polled_revision = -1

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """注册世界、连上直播间并开始消费事件。

        Raises:
            RuntimeError: 未配置房间号时抛出——没配房间号的直播间世界没有任何意义，
                必须让加载失败并把原因暴露出来，而不是安静地空转。
        """

        if self.config.bilibili.room_id <= 0:
            raise RuntimeError(
                "B站直播间世界缺少配置 bilibili.room_id（B站直播间房间号），请填写后重新加载插件"
            )

        settings = self.config.bilibili
        self._stats = LiveRoomStats(recent_window_seconds=settings.recent_window_seconds)
        self._polled_revision = -1

        await self._register_to_core()
        try:
            await self._start_client()
        except Exception:
            # 连接失败就让世界注册一并回退，避免留下一个永远没有事件的世界
            await self._unregister_from_core()
            raise

        # 出站同样归本插件：主程序会把直播聊天流里的回复投递到 handle_gateway_send
        await self._publish_gateway_state(ready=True)

        self.ctx.logger.info(
            f"B站直播间世界已接入框架：room={settings.room_id} "
            f"world_name={settings.name} 互动窗口={settings.recent_window_seconds:g} 秒"
        )

    async def on_unload(self) -> None:
        """停止消费与连接，交出出站网关，并从框架基座注销本世界。"""

        await self._stop_consume_task()
        await self._stop_client()
        await self._publish_gateway_state(ready=False)
        await self._unregister_from_core()
        # 统计属于本次连接：清掉它，卸载后再有人来取状态会直接报错而不是拿到旧数据
        self._stats = None
        self.ctx.logger.info("B站直播间世界已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置变更后按新参数整体重建连接与统计。

        房间号、SESSDATA、统计窗口都是构造期参数，无法就地热改，因此整体拆掉再建；
        旧房间的互动列表也随之丢弃，避免新旧房间的数据混在一份快照里。
        """

        del config_data
        await self._stop_consume_task()
        await self._stop_client()
        await self._unregister_from_core()

        self._stats = LiveRoomStats(recent_window_seconds=self.config.bilibili.recent_window_seconds)
        self._polled_revision = -1

        await self._register_to_core()
        try:
            await self._start_client()
        except Exception:
            await self._unregister_from_core()
            raise

        self.ctx.logger.info(f"B站直播间世界配置已更新（scope={scope} version={version}）")

    # ------------------------------------------------------------------ 框架约定 API

    @API(
        WORLD_POLL_API,
        description="框架轮询用：返回互动状态自上次轮询以来是否变化，以及最新状态快照。",
        version="1",
        public=True,
    )
    async def handle_world_poll(self, **_kwargs: Any) -> Dict[str, Any]:
        """供框架做廉价的变更检测。"""

        stats = self._require_stats()
        revision = stats.take_revision(now=time.monotonic())
        changed = revision != self._polled_revision
        self._polled_revision = revision
        return {"changed": changed, "snapshot": self._render_snapshot()}

    @API(
        WORLD_OBSERVE_API,
        description="按需拉取 B 站直播间的即时完整状态（连接状态 + 最近互动）。",
        version="1",
        public=True,
    )
    async def handle_world_observe(self, **_kwargs: Any) -> Dict[str, Any]:
        """供 world_observe 工具按需拉取。"""

        return {"snapshot": self._render_snapshot()}

    # ------------------------------------------------------------------ 出站消息网关

    @MessageGateway(
        "send",
        name=SEND_GATEWAY_NAME,
        description="承接 bilibili_live 的出站：麦麦在直播聊天流里的回复只记入日志，不回发直播间。",
        platform=LIVE_PLATFORM,
        protocol="blivedm",
    )
    async def handle_gateway_send(
        self,
        message: Dict[str, Any],
        route: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """接收主程序要发往直播间的那条回复。

        观众的耳朵听到的是主程序的直播语音（合成后播到本机扬声器），文字本身不回发直播间，
        因此这里只把回复记进日志并回报成功。回报成功与否是有后果的：
        主程序据此判定「已发送」，而发送结果又决定直播语音要不要朗读。
        """

        del route, metadata

        text = self._extract_reply_text(message)
        if not text:
            # 纯图片 / 表情这类回复没有可朗读的文本，但仍算发送成功
            self.ctx.logger.info("[麦麦回复-无文本] 本次回复没有文本段")
            return {"success": True}

        message_info = message.get("message_info")
        user_info = message_info.get("user_info") if isinstance(message_info, dict) else None
        sender = str((user_info or {}).get("user_nickname") or "")
        self.ctx.logger.info(f"[麦麦回复] {sender}: {text}")
        return {"success": True}

    async def _publish_gateway_state(self, *, ready: bool) -> None:
        """向主程序上报出站网关的就绪状态。

        路由键刻意不带账号：出站消息（直播聊天流里的回复）构造出的 ``RouteKey``
        只有平台名，带上账号维度会让绑定匹配不上，出站就会掉回已退休的 legacy 链路。

        Raises:
            RuntimeError: 上报被拒绝时抛出——网关组件已声明，被拒说明契约被破坏。
        """

        accepted = await self.ctx.gateway.update_state(
            SEND_GATEWAY_NAME,
            ready=ready,
            platform=LIVE_PLATFORM,
        )
        if not accepted:
            raise RuntimeError(f"向主程序上报 B站直播出站网关状态失败：ready={ready}")

    @staticmethod
    def _extract_reply_text(message: Dict[str, Any]) -> str:
        """取出出站消息里的文本段并拼成一行；引用、图片等非文本段直接跳过。"""

        components = message.get("raw_message")
        if not isinstance(components, list):
            return ""
        return " ".join(
            item["data"]
            for item in components
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("data"), str)
        ).strip()

    # ------------------------------------------------------------------ 内部实现

    async def _register_to_core(self) -> None:
        """把本世界注册进主程序的世界框架。

        Raises:
            RuntimeError: 能力调用失败时抛出——能力已写在 manifest 中，
                注册失败说明契约被破坏，必须暴露而不是带着半个世界继续跑。
        """

        settings = self.config.bilibili
        name = settings.name.strip()
        if not name:
            raise RuntimeError("B站直播间世界配置缺少 bilibili.name")

        # 能力由宿主按调用方身份归属，插件无需（也不能）自行声明 plugin_id。
        response = await self.ctx.call_capability(
            WORLD_REGISTER_CAPABILITY,
            name=name,
            display_name=settings.display_name,
            env_prompt=settings.env_prompt,
            # 状态由框架按变更轮询取走，本插件从不主动推状态
            polls_changes=True,
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"向世界框架注册 B站直播间失败：{error}")

        self._registered_name = name

    async def _unregister_from_core(self) -> None:
        """把本世界从世界框架注销；未注册时直接返回。"""

        registered = self._registered_name
        if not registered:
            return
        self._registered_name = ""

        response = await self.ctx.call_capability(WORLD_UNREGISTER_CAPABILITY, name=registered)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"从世界框架注销 B站直播间失败：world={registered} error={error}")

    async def _start_client(self) -> None:
        """建立弹幕连接并启动事件消费任务。"""

        settings = self.config.bilibili
        self._queue = asyncio.Queue()
        self._client = BiliDanmakuClient(
            room_id=settings.room_id,
            sessdata=settings.sessdata,
            # blivedm 回调是同步分发的，只能入队，不能在回调里 await
            push_event=self._queue.put_nowait,
            logger=self.ctx.logger,
        )
        await self._client.start()
        self._consume_task = asyncio.create_task(self._consume_loop())

    async def _stop_client(self) -> None:
        """关闭弹幕连接并丢弃队列中未处理的事件。"""

        client = self._client
        self._client = None
        self._queue = None
        if client is not None:
            await client.stop()

    async def _stop_consume_task(self) -> None:
        """取消事件消费任务。"""

        task = self._consume_task
        self._consume_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _consume_loop(self) -> None:
        """把队列里的产出逐条按类型分派出去。

        单条处理失败只记录日志，不终止循环：直播间消息是易失的外部链路，
        一次抖动不应该让整个直播间从此静音。
        """

        queue = self._queue
        if queue is None:
            return

        while True:
            item = await queue.get()
            try:
                await self._dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条消息处理失败不应终止消费链
                self.ctx.logger.exception(f"处理直播间消息失败: {item!r} error={exc!r}")

    async def _dispatch(self, item: LiveItem) -> None:
        """按产出类型分派：聊天消息注入聊天流，世界事件上报框架基座。"""

        if isinstance(item, LiveChatMessage):
            await self._inject_chat_message(item)
        else:
            await self._report_event(item)

    async def _inject_chat_message(self, chat: LiveChatMessage) -> None:
        """把弹幕 / 醒目留言注入主程序，成为直播间聊天流里的一条普通消息。

        Raises:
            RuntimeError: 注入失败时抛出，由消费循环决定是否继续——能力已写在 manifest 中，
                失败说明契约被破坏，必须暴露而不是静默丢掉观众的发言。
        """

        room_id = self.config.bilibili.room_id
        response = await self.ctx.call_capability(
            CHAT_INJECT_CAPABILITY,
            platform=LIVE_PLATFORM,
            text=chat.text,
            user_id=str(chat.uid),
            user_nickname=chat.uname,
            group_id=str(room_id),
            group_name=ROOM_GROUP_NAME_TEMPLATE.format(room_id=room_id),
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"注入直播间聊天消息失败：{error}")

    async def _report_event(self, event: LiveEvent) -> None:
        """记录事件到统计，并作为世界事件交给框架基座。

        Raises:
            RuntimeError: 基座调用失败时抛出，由消费循环决定是否继续。
        """

        stats = self._require_stats()
        stats.record(event, now=time.monotonic())

        response = await self.ctx.call_capability(
            WORLD_EVENT_CAPABILITY,
            world_name=self.config.bilibili.name,
            # 事件类型只用种类名：世界名已经由基座加在文本前缀与唤醒原因里，不必重复
            event_type=event.kind.value,
            text=event.text,
            trigger=self._trigger_for(event.kind),
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"上报世界事件失败：{error}")

    def _trigger_for(self, kind: LiveEventKind) -> str:
        """取该类事件的投递语义。"""

        if kind is LiveEventKind.INTERACT:
            return self.config.bilibili.interact_trigger
        return TRIGGER_BY_KIND[kind]

    def _render_snapshot(self) -> str:
        """渲染一份中文状态快照。"""

        stats = self._require_stats()
        client = self._client
        connection = "未连接（插件刚启动或连接已被关停）" if client is None else client.status_text
        return stats.render(
            now=time.monotonic(),
            room_id=self.config.bilibili.room_id,
            connection=connection,
        )

    def _require_stats(self) -> LiveRoomStats:
        """取直播间统计；插件尚未完成 ``on_load`` 时直接报错。

        Raises:
            RuntimeError: 插件尚未完成 ``on_load`` 时抛出。
        """

        if self._stats is None:
            raise RuntimeError("B站直播间世界插件尚未加载完成")
        return self._stats


def create_plugin() -> WorldBilibiliPlugin:
    """插件工厂函数。"""

    return WorldBilibiliPlugin()
