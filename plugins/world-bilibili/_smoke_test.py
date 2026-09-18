"""B站直播间世界插件冒烟测试。

覆盖：Manifest / 配置一致性 / SDK 组件契约、渲染层（blivedm 消息 → 聊天消息 / 世界事件）、
上舰去重、统计版本号语义、以及「注册 → 事件 → 状态 → 按需拉取 → 弹幕注入」的插件编排链路。

测试把插件的 ``ctx.call_capability`` 接到**真实的主程序能力实现**
（``RuntimeWorldCapabilityMixin``）上，世界事件经由 ``WorldManager`` + ``WorldEventBus``
投递到被绑定的直播会话运行时，基座回呼的 ``world_poll`` / ``world_observe`` 经由
``WorldManager._invoke_world_api`` 打到插件实例上。因此校验的是插件与主程序之间真实的
调用契约（参数名、返回结构、投递语义），而不是打桩后自行假设的结构。

唯一例外是 ``message.inject_inbound``：真跑一次会连带启动整条入站主链（建会话、落库、
唤醒回复），远超冒烟测试的范围，因此这里只把插件**发出去的参数**记下来做断言，
真实实现是否登记在册另由 ``register_capability_impls`` 的注册表校验覆盖。

全部用例跑在同一个事件循环里，因为事件总线会绑定到创建它的循环。
冒烟测试不连真实直播间：只把插件拿到的 ``BiliDanmakuClient`` 换成不留网络的替身，
其余链路（blivedm 回调 → 渲染 → 入队 → 消费 → 分发）都走真实实现。

运行： python _smoke_test.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

PLUGIN_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = PLUGIN_DIR.parents[1]  # 直播目录

# 导入项目 logger 会在 CWD 下创建 logs/，切到项目根避免污染插件目录
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "src"))

from maibot_sdk.context import PluginContext  # noqa: E402

from plugin_runtime.runner.manifest_validator import ManifestValidator  # noqa: E402
from plugin_runtime.runner.plugin_loader import PluginLoader  # noqa: E402
from src.config.config import global_config  # noqa: E402
from src.plugin_runtime import integration as plugin_runtime_integration  # noqa: E402
from src.plugin_runtime.capabilities.registry import register_capability_impls  # noqa: E402
from src.plugin_runtime.capabilities.worlds import RuntimeWorldCapabilityMixin  # noqa: E402
from src.worlds.manager import get_world_manager  # noqa: E402

PASS = 0
FAIL = 0
LOGGER = logging.getLogger("world-bilibili-smoke")

BILIBILI_PLUGIN_ID = "maibot-live.world-bilibili"
LIVE_SESSION_ID = "stream-live"
LIVE_PLATFORM = "bilibili_live"
ROOM_ID = 30655190


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_gift(
    *,
    uname: str = "小明",
    uid: int = 1001,
    coin_type: str = "gold",
    total_coin: int = 1000,
    num: int = 10,
) -> SimpleNamespace:
    """造一个 GiftMessage 形状的消息（渲染层按鸭子类型使用）。"""

    return SimpleNamespace(
        gift_name="辣条",
        num=num,
        uname=uname,
        uid=uid,
        guard_level=0,
        action="赠送",
        price=100,
        coin_type=coin_type,
        total_coin=total_coin,
    )


# --------------------------------------------------------------------------- #
# 1. 加载插件实例与静态契约
# --------------------------------------------------------------------------- #

print("== 1. 加载与配置 ==")
validator = ManifestValidator(log_errors=False, log_compat_warnings=False)
manifest = validator.load_from_plugin_path(PLUGIN_DIR)
check("manifest 校验通过", manifest is not None, "; ".join(validator.errors))

plugin: Any = None
if manifest is not None:
    loader = PluginLoader(host_version="1.1.0")
    meta = loader._load_single_plugin(manifest.id, PLUGIN_DIR, manifest, PLUGIN_DIR / "plugin.py")
    check("Runner 加载链路可用", meta is not None)
    if meta is not None:
        plugin = meta.instance

if manifest is None or plugin is None:
    print(f"\n=== 总计: {PASS} pass / {FAIL} fail ===")
    sys.exit(1)

# Runner 用动态模块名加载插件，插件内部的 ``bilibili_kit`` 与测试侧直接 import 的**不是同一个模块对象**
# （枚举成员、类对象都不共享）。为了保证测的就是插件真正跑的那份代码，这里全部从插件模块里取。
PLUGIN_MODULE = sys.modules[type(plugin).__module__]
KIT = PLUGIN_MODULE.bilibili_kit

BiliDanmakuClient = KIT.BiliDanmakuClient
LiveChatMessage = KIT.LiveChatMessage
LiveEvent = KIT.LiveEvent
LiveRoomStats = KIT.LiveRoomStats
render_gift = KIT.render_gift
render_interact = KIT.render_interact
_LiveHandler = KIT.client._LiveHandler  # 私有分发层，冒烟测试需要直接驱动
MAX_RECENT_EVENTS = KIT.stats.MAX_RECENT_EVENTS

# 插件声明使用的能力必须与代码里实际调用的能力名一致
DECLARED_CAPABILITIES = [
    PLUGIN_MODULE.CHAT_INJECT_CAPABILITY,
    PLUGIN_MODULE.WORLD_REGISTER_CAPABILITY,
    PLUGIN_MODULE.WORLD_UNREGISTER_CAPABILITY,
    PLUGIN_MODULE.WORLD_EVENT_CAPABILITY,
]
WORLD_CAPABILITIES = [
    PLUGIN_MODULE.WORLD_REGISTER_CAPABILITY,
    PLUGIN_MODULE.WORLD_UNREGISTER_CAPABILITY,
    PLUGIN_MODULE.WORLD_EVENT_CAPABILITY,
]

base_config = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
plugin.set_plugin_config(base_config)

default_cfg = plugin.get_default_config()
check("config.toml 顶层键齐全", set(base_config) == set(default_cfg), f"{set(base_config)} vs {set(default_cfg)}")
for section in default_cfg:
    check(
        f"[{section}] 键一致",
        set(base_config[section]) == set(default_cfg[section]),
        f"{set(base_config[section])} vs {set(default_cfg[section])}",
    )
check(
    "capabilities 与实际用法匹配",
    sorted(manifest.capabilities) == sorted(DECLARED_CAPABILITIES),
    f"{manifest.capabilities} vs {DECLARED_CAPABILITIES}",
)
check(
    "不再依赖任何插件（框架基座已在主程序内）",
    manifest.plugin_dependency_ids == [],
    str(manifest.plugin_dependency_ids),
)
check(
    "声明了 blivedm 包依赖",
    [dependency.name for dependency in manifest.python_package_dependencies] == ["blivedm"],
    str(manifest.python_package_dependencies),
)

components = plugin.get_components()
by_type: Dict[str, List[str]] = {}
for component in components:
    by_type.setdefault(str(component.get("type")), []).append(str(component.get("name")))
check("注册了 2 个 API", sorted(by_type.get("API", [])) == ["world_observe", "world_poll"], str(by_type))
check(
    "注册了 1 个出站消息网关",
    by_type.get("MESSAGE_GATEWAY") == [PLUGIN_MODULE.SEND_GATEWAY_NAME],
    str(by_type),
)
check("没有多余组件（不重复提供 world_observe 工具）", len(components) == 3, str(by_type))
gateway_metadata = next(
    component["metadata"] for component in components if component.get("type") == "MESSAGE_GATEWAY"
)
check(
    "出站网关只声明发送，且平台就是直播平台",
    gateway_metadata.get("route_type") == "send" and gateway_metadata.get("platform") == LIVE_PLATFORM,
    str(gateway_metadata),
)
api_public = {
    str(component.get("name")): bool(component.get("metadata", {}).get("public")) for component in components
}
check("框架约定 API 均已公开", all(api_public[name] for name in by_type.get("API", [])), str(api_public))

try:
    PluginLoader._validate_sdk_plugin_contract(manifest.id, plugin)
    check("SDK 插件契约通过", True)
except Exception as exc:  # noqa: BLE001 - 冒烟测试统一收集失败原因
    check("SDK 插件契约通过", False, f"{type(exc).__name__}: {exc}")
PluginContext(BILIBILI_PLUGIN_ID)
check("PluginContext 可直接构造", True)


class _RecordingCapabilityService:
    """冒充 Supervisor 的能力服务，只记录登记了哪些能力。"""

    def __init__(self) -> None:
        self.impls: Dict[str, Any] = {}

    def register_capability(self, name: str, impl: Any) -> None:
        self.impls[name] = impl


# __new__ 跳过 __init__：这里只借用能力方法本身，不需要真的把整个运行时跑起来
capability_service = _RecordingCapabilityService()
register_capability_impls(
    plugin_runtime_integration.PluginRuntimeManager.__new__(plugin_runtime_integration.PluginRuntimeManager),
    SimpleNamespace(capability_service=capability_service),
)
check(
    "插件用到的能力都已在主程序登记",
    all(name in capability_service.impls for name in DECLARED_CAPABILITIES),
    f"{DECLARED_CAPABILITIES} vs {sorted(capability_service.impls)}",
)


# --------------------------------------------------------------------------- #
# 2. 渲染层与连接层
# --------------------------------------------------------------------------- #


def test_render_and_client() -> None:
    """验证 blivedm 消息 → 聊天消息 / 世界事件，以及上舰去重与连接状态文案。"""

    print("\n== 2. 渲染与连接层 ==")
    events: List[Any] = []
    client = BiliDanmakuClient(room_id=ROOM_ID, sessdata="", push_event=events.append, logger=LOGGER)
    handler = _LiveHandler(client)

    check("未连接时状态文案为未连接", client.status_text == "未连接", client.status_text)
    client.mark_connected()
    check("匿名连接会提示身份可能打码", "未登录" in client.status_text, client.status_text)

    # 弹幕走聊天消息通道，正文就是弹幕原文
    handler._on_danmaku(None, SimpleNamespace(dm_type=0, msg="晚上好", uname="小明", uid=1001))
    check(
        "弹幕渲染成聊天消息",
        len(events) == 1 and isinstance(events[0], LiveChatMessage) and events[0].text == "晚上好",
        str(events),
    )
    check("弹幕带上发送者身份", events[0].uid == 1001 and events[0].uname == "小明", str(events[0]))

    events.clear()
    handler._on_danmaku(None, SimpleNamespace(dm_type=1, msg="[表情]", uname="小明", uid=1001))
    check("非文本弹幕被丢弃", events == [], str(events))

    events.clear()
    handler._on_danmaku(None, SimpleNamespace(dm_type=0, msg="   ", uname="小明", uid=1001))
    check("空弹幕被丢弃", events == [], str(events))

    events.clear()
    handler._on_gift(None, make_gift())
    check(
        "礼物渲染出用户、数量与人民币价值",
        len(events) == 1 and "小明" in events[0].text and "≈ 1.00 元" in events[0].text,
        str([event.text for event in events]),
    )
    check("礼物事件带上可换算的金瓜子数", events[0].coin == 1000, str(events[0].coin))

    events.clear()
    handler._on_gift(None, make_gift(coin_type="silver", total_coin=100))
    check(
        "银瓜子礼物不折算人民币",
        "银瓜子" in events[0].text and events[0].coin == 0,
        str(events[0].text),
    )

    events.clear()
    guard = SimpleNamespace(guard_level=3, price=138000, num=1, username="小红", uid=1002, gift_name="舰长")
    handler._on_buy_guard(None, guard)
    check(
        "上舰渲染出舰队名与价格",
        len(events) == 1 and "舰长" in events[0].text and "138.00 元" in events[0].text,
        str([event.text for event in events]),
    )
    handler._on_user_toast_v2(
        None,
        SimpleNamespace(guard_level=3, price=138000, num=1, unit="月", username="小红", uid=1002, source=0),
    )
    check("同一次上舰的两条消息只报一条", len(events) == 1, str([event.text for event in events]))

    handler._on_user_toast_v2(
        None,
        SimpleNamespace(guard_level=3, price=138000, num=1, unit="月", username="小刚", uid=1003, source=2),
    )
    check("source=2 的重复赠送通知被丢弃", len(events) == 1, str([event.text for event in events]))

    handler._on_buy_guard(
        None, SimpleNamespace(guard_level=1, price=2000000, num=1, username="小刚", uid=1003, gift_name="总督")
    )
    check(
        "另一用户上舰照常上报",
        len(events) == 2 and "总督" in events[1].text,
        str([event.text for event in events]),
    )

    # 醒目留言只走聊天消息这一路：金额写进正文标记，不再额外产生一条世界事件
    events.clear()
    handler._on_super_chat(None, SimpleNamespace(price=30, message="加油", uname="小美", uid=1004, time=60))
    check(
        "醒目留言渲染成带金额的聊天消息",
        len(events) == 1 and isinstance(events[0], LiveChatMessage) and events[0].text == "[SC ¥30] 加油",
        str(events),
    )

    events.clear()
    handler._on_interact_word_v2(None, SimpleNamespace(msg_type=1, username="小丽", uid=1005))
    handler._on_interact_word_v2(None, SimpleNamespace(msg_type=9, username="小丽", uid=1005))
    check(
        "进场与未知互动都如实渲染",
        len(events) == 2 and "进入直播间" in events[0].text and "未知互动" in events[1].text,
        str([event.text for event in events]),
    )

    client.mark_disconnected(RuntimeError("boom"))
    check("断开后状态文案带出原因", "未连接（RuntimeError: boom）" == client.status_text, client.status_text)


# --------------------------------------------------------------------------- #
# 3. 统计版本号语义
# --------------------------------------------------------------------------- #


def test_stats() -> None:
    """验证「快照内容版本号」只在内容真的会变时自增。"""

    print("\n== 3. 统计语义 ==")
    stats = LiveRoomStats(recent_window_seconds=10.0)
    event = render_interact(SimpleNamespace(msg_type=1, username="小丽", uid=1005))

    check("初始版本号为 0", stats.take_revision(now=100.0) == 0)
    stats.record(event, now=100.0)
    check("记录互动后版本号自增", stats.take_revision(now=100.0) == 1)
    check("无变化时版本号不变", stats.take_revision(now=105.0) == 1)
    check("互动滚出窗口后版本号自增", stats.take_revision(now=111.0) == 2)

    snapshot = stats.render(now=111.0, room_id=ROOM_ID, connection="已连接")
    check("空快照明确说明没有互动", "没有礼物、上舰或进场" in snapshot, snapshot)

    stats.record(render_gift(make_gift()), now=112.0)
    snapshot = stats.render(now=112.0, room_id=ROOM_ID, connection="已连接")
    check("快照列出窗口内的互动", "辣条" in snapshot, snapshot)

    flood = LiveRoomStats(recent_window_seconds=60.0)
    for index in range(MAX_RECENT_EVENTS + 8):
        flood.record(event, now=200.0 + index)
    lines = flood.render(now=230.0, room_id=ROOM_ID, connection="已连接").splitlines()
    check(
        "互动列表有条数上限",
        sum(1 for line in lines if line.startswith("- ")) == MAX_RECENT_EVENTS,
        str(len(lines)),
    )


# --------------------------------------------------------------------------- #
# 4. 组装：真实 WorldManager + 真实能力实现 + 假上下文
# --------------------------------------------------------------------------- #

sent: List[str] = []
sent_args: List[Dict[str, Any]] = []
delivered: List[Dict[str, str]] = []
injected: List[Dict[str, Any]] = []
fail_register = [False]
fail_event = [False]
fail_inject = [False]

# 能力实现混入：与宿主运行时用的是同一份代码，只是省掉了整套 supervisor 启动。
capability_impl = RuntimeWorldCapabilityMixin()


async def dispatch_capability(capability: str, plugin_id: str, args: Dict[str, Any]) -> Any:
    """按能力名分派到真实的主程序能力实现，校验插件与基座之间的参数契约。"""

    if capability == PLUGIN_MODULE.CHAT_INJECT_CAPABILITY:
        # 注入能力真跑一次会连带启动整条入站主链（建会话、落库、唤醒回复），
        # 冒烟测试只校验插件发出去的参数；真实实现的登记由注册表用例覆盖。
        if fail_inject[0]:
            return {"success": False, "error": "模拟注入失败"}
        injected.append(dict(args))
        return {"success": True, "message_id": "stub", "session_id": LIVE_SESSION_ID}
    if capability == WORLD_CAPABILITIES[0]:
        if fail_register[0]:
            return {"success": False, "error": "模拟基座注册失败"}
        return await capability_impl._cap_world_register(plugin_id, capability, args)
    if capability == WORLD_CAPABILITIES[1]:
        return await capability_impl._cap_world_unregister(plugin_id, capability, args)
    if capability == WORLD_CAPABILITIES[2]:
        if fail_event[0]:
            return {"success": False, "error": "模拟基座事件上报失败"}
        return await capability_impl._cap_world_event(plugin_id, capability, args)
    return {"success": False, "error": f"未知能力 {capability}"}


class _FakeGateway:
    """插件上下文里的消息网关代理替身：只记录上报过的就绪状态。"""

    def __init__(self) -> None:
        self.states: List[Dict[str, Any]] = []

    async def update_state(self, gateway_name: str, **kwargs: Any) -> bool:
        self.states.append({"gateway_name": gateway_name, **kwargs})
        return True


class _FakeContext:
    """插件上下文替身：插件只用到 ``plugin_id`` / ``logger`` / ``call_capability`` / ``gateway``。"""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.logger = LOGGER
        self.gateway = _FakeGateway()

    async def call_capability(self, capability: str, timeout_ms: int | None = None, **kwargs: Any) -> Any:
        del timeout_ms
        sent.append(capability)
        sent_args.append(dict(kwargs))
        return await dispatch_capability(capability, self.plugin_id, kwargs)


plugin._set_context(_FakeContext(BILIBILI_PLUGIN_ID))


class _FakeLiveRuntime:
    """冒充被绑定的直播会话运行时：只记录基座投递过来的世界事件。"""

    def __init__(self, platform: str = LIVE_PLATFORM) -> None:
        # 未显式配置 worlds.stream_id 时，基座据此判断该会话是不是直播聊天流
        self.chat_stream = SimpleNamespace(platform=platform)

    async def register_world_event(self, *, source_name: str, text: str, trigger: str) -> None:
        delivered.append({"source_name": source_name, "text": text, "trigger": trigger})


class _FakeAPIEntry:
    """冒充插件运行时的 API 注册表条目。"""

    def __init__(self, name: str, plugin_id: str) -> None:
        self.name = name
        self.full_name = f"{plugin_id}.{name}"
        self.version = "1"
        self.handler_name = name
        self.dynamic = False


class _FakeAPIRegistry:
    """只实现基座回呼世界 API 时用到的那部分查询。"""

    def __init__(self, plugin: Any, plugin_id: str) -> None:
        self._plugin = plugin
        self._plugin_id = plugin_id

    def get_apis(self, *, plugin_id: str = "", name: str = "", enabled_only: bool = False) -> List[_FakeAPIEntry]:
        del enabled_only
        if plugin_id != self._plugin_id or not hasattr(self._plugin, f"handle_{name}"):
            return []
        return [_FakeAPIEntry(name, plugin_id)]


class _FakeResponse:
    """冒充插件运行时的 API 调用返回。"""

    def __init__(self, payload: Any = None, error: Any = None) -> None:
        self.payload = payload
        self.error = error


class _FakeSupervisor:
    """冒充插件运行时 supervisor：把基座回呼直接打到被测插件实例上。"""

    def __init__(self, plugin: Any, plugin_id: str) -> None:
        self._plugin = plugin
        self._plugin_id = plugin_id
        self.api_registry = _FakeAPIRegistry(plugin, plugin_id)

    async def invoke_api(self, *, plugin_id: str, component_name: str, args: Dict[str, Any]) -> _FakeResponse:
        del args
        handler = getattr(self._plugin, f"handle_{component_name}", None)
        if plugin_id != self._plugin_id or handler is None:
            return _FakeResponse(error={"message": f"未知世界 API: {component_name}"})
        return _FakeResponse(payload={"success": True, "result": await handler()})


class _FakeRuntimeManager:
    """冒充插件运行时管理器：只暴露基座回呼用到的 ``supervisors``。"""

    def __init__(self, supervisors: List[Any]) -> None:
        self.supervisors = supervisors


def configure_worlds() -> None:
    """把全局世界配置调成冒烟测试需要的形态。"""

    settings = global_config.worlds
    settings.enabled = True
    # 留空 stream_id，走「按直播平台自动定位聊天流」，这也是默认配置下的行为
    settings.stream_id = ""
    settings.live_platform = LIVE_PLATFORM
    settings.inject_state = True
    settings.stale_after_seconds = 45.0
    settings.event_throttle_seconds = 0.0
    settings.change_poll_seconds = 0.0


manager = get_world_manager()
plugin_runtime_integration.get_plugin_runtime_manager = lambda: _FakeRuntimeManager(
    [_FakeSupervisor(plugin, BILIBILI_PLUGIN_ID)]
)


class _OfflineBiliDanmakuClient(BiliDanmakuClient):  # type: ignore[misc,valid-type]
    """连接层替身：只更新连接状态，不建立任何网络连接。"""

    async def start(self) -> None:
        self.mark_connected()

    async def stop(self) -> None:
        self.mark_disconnected(None)


# --------------------------------------------------------------------------- #
# 5. 用例
# --------------------------------------------------------------------------- #


async def test_plugin_orchestration() -> None:
    """验证注册、事件上报、状态轮询、按需拉取、配置更新与卸载。"""

    print("\n== 4. 插件编排 ==")
    configure_worlds()

    check(
        "非直播聊天流不会被绑定",
        await manager.bind_session("other-session", _FakeLiveRuntime(platform="qq")) is False,
    )
    check("世界框架绑定直播会话", await manager.bind_session(LIVE_SESSION_ID, _FakeLiveRuntime()) is True)

    # 未配置房间号时必须拒绝加载，而不是安静地空转
    plugin.set_plugin_config(
        {
            "plugin": dict(base_config["plugin"]),
            "bilibili": dict(base_config["bilibili"], room_id=0),
        }
    )
    try:
        await plugin.on_load()
        check("未配置房间号时拒绝加载", False, "未抛错")
    except RuntimeError:
        check("未配置房间号时拒绝加载", True)
    check("拒绝加载后不会留下半个世界", manager.registry.names() == [], str(manager.registry.names()))

    run_config = {
        "plugin": dict(base_config["plugin"]),
        "bilibili": dict(base_config["bilibili"], room_id=ROOM_ID, recent_window_seconds=5.0),
    }
    plugin.set_plugin_config(run_config)

    original_client_class = PLUGIN_MODULE.BiliDanmakuClient
    PLUGIN_MODULE.BiliDanmakuClient = _OfflineBiliDanmakuClient
    try:
        await plugin.on_load()

        check("注册后基座能查到世界", manager.registry.names() == ["bilibili"], str(manager.registry.names()))
        descriptor = manager.registry.require("bilibili")
        check(
            "描述符字段与配置一致",
            descriptor.plugin_id == BILIBILI_PLUGIN_ID
            and descriptor.display_name == "B站直播间"
            and descriptor.polls_changes is True,
            f"{descriptor.plugin_id} / {descriptor.display_name} / {descriptor.polls_changes}",
        )
        check("注册走的是 world.register 能力", sent[0] == WORLD_CAPABILITIES[0], str(sent[:1]))
        check("加载后消费任务已启动", plugin._consume_task is not None and not plugin._consume_task.done())
        snapshot = plugin._render_snapshot()
        check("加载后连接状态为已连接", "已连接" in snapshot, snapshot)
        check(
            "加载后上报出站网关就绪（平台级路由，不带账号）",
            plugin.ctx.gateway.states[-1]
            == {"gateway_name": PLUGIN_MODULE.SEND_GATEWAY_NAME, "ready": True, "platform": LIVE_PLATFORM},
            str(plugin.ctx.gateway.states[-1:]),
        )

        # 首次轮询要把初始状态推给框架（change_poll_seconds=0 关掉了自动轮询，这里手动驱动）
        await manager._poll_once()
        first_injection = manager.build_state_injection()
        check("首次轮询写入状态缓存", "没有礼物" in first_injection, first_injection)
        check("同一份快照不会重复注入", manager.build_state_injection() == "")

        # 礼物：flush → 立即投递
        plugin._queue.put_nowait(render_gift(make_gift()))
        await asyncio.sleep(0.1)
        check(
            "礼物事件带世界显示名前缀",
            bool(delivered) and delivered[-1]["source_name"] == "世界·B站直播间",
            str(delivered[-1:]),
        )
        check("礼物事件正文带上送礼人", "小明" in delivered[-1]["text"], str(delivered[-1]))
        check("礼物事件按 flush 投递", delivered[-1]["trigger"] == "flush", str(delivered[-1]))
        check("事件类型只用种类名，不重复世界名", sent_args[-1].get("event_type") == "gift", str(sent_args[-1]))

        await manager._poll_once()
        gift_injection = manager.build_state_injection()
        check("互动变化后状态重新注入", "辣条" in gift_injection, gift_injection)

        # 弹幕：注入成聊天消息，参数必须落在直播间聊天流上
        injected.clear()
        plugin._queue.put_nowait(
            KIT.render_danmaku(SimpleNamespace(dm_type=0, msg="晚上好", uname="小明", uid=1001))
        )
        await asyncio.sleep(0.1)
        check(
            "弹幕走 message.inject_inbound 注入",
            sent[-1] == PLUGIN_MODULE.CHAT_INJECT_CAPABILITY,
            str(sent[-1:]),
        )
        check(
            "注入参数指向直播间聊天流",
            injected
            == [
                {
                    "platform": LIVE_PLATFORM,
                    "text": "晚上好",
                    "user_id": "1001",
                    "user_nickname": "小明",
                    "group_id": str(ROOM_ID),
                    "group_name": f"B站直播间{ROOM_ID}",
                }
            ],
            str(injected),
        )
        await manager._poll_once()
        check("弹幕不进世界状态快照", manager.build_state_injection() == "", "弹幕改变了世界状态快照")

        # 醒目留言同样走聊天消息，不再进世界事件
        injected.clear()
        delivered.clear()
        plugin._queue.put_nowait(
            KIT.render_super_chat(SimpleNamespace(price=30, message="加油", uname="小美", uid=1004, time=60))
        )
        await asyncio.sleep(0.1)
        check(
            "醒目留言以带金额的正文注入聊天流",
            len(injected) == 1 and injected[0]["text"] == "[SC ¥30] 加油",
            str(injected),
        )
        check("醒目留言不再产生世界事件", delivered == [], str(delivered))

        # 出站：主程序把麦麦的回复投递过来（正是旧内置适配器原来的位置）
        check(
            "出站回复只取文本段",
            plugin._extract_reply_text(
                {"raw_message": [{"type": "text", "data": " 晚上好呀 "}, {"type": "image", "data": "表情包"}]}
            )
            == "晚上好呀",
            "非文本段未跳过或文本未裁剪",
        )
        reply_result = await plugin.handle_gateway_send(
            message={
                "message_info": {"user_info": {"user_nickname": "未可飞"}},
                "raw_message": [{"type": "text", "data": "晚上好呀"}],
            },
            route={"platform": LIVE_PLATFORM},
            metadata={},
        )
        check(
            "出站回复回报成功（决定直播语音是否朗读）",
            reply_result == {"success": True},
            str(reply_result),
        )

        # 进场：默认 debounce → 照常投递，但不打断当前回合
        plugin._queue.put_nowait(render_interact(SimpleNamespace(msg_type=1, username="小丽", uid=1005)))
        await asyncio.sleep(0.1)
        check("进场事件仍然投递", "小丽" in delivered[-1]["text"], str(delivered[-1]))
        check("默认按 debounce 投递进场", delivered[-1]["trigger"] == "debounce", str(delivered[-1]))

        # 改成 flush 后，进场也立即投递
        flush_config = {
            "plugin": dict(base_config["plugin"]),
            "bilibili": dict(run_config["bilibili"], interact_trigger="flush"),
        }
        plugin.set_plugin_config(flush_config)
        plugin._queue.put_nowait(render_interact(SimpleNamespace(msg_type=2, username="小刚", uid=1006)))
        await asyncio.sleep(0.1)
        check("配置改为 flush 后进场按 flush 投递", delivered[-1]["trigger"] == "flush", str(delivered[-1]))

        # 按需拉取：即时快照直接进上下文，且不造成下轮重复注入
        observed = await manager.observe("bilibili")
        check("基座可经 world_observe 拉直播间状态", "最近" in observed and "小刚" in observed, observed)
        check("即时拉取不会造成下轮重复注入", manager.build_state_injection() == "")

        # 互动滚出窗口后，轮询仍能发现变化（避免快照一直挂着旧互动）
        await asyncio.sleep(5.2)
        await manager._poll_once()
        expired_injection = manager.build_state_injection()
        check("互动过期后状态会被重新注入", "没有礼物" in expired_injection, expired_injection)

        # 契约违约必须暴露
        fail_register[0] = True
        try:
            await plugin._register_to_core()
            check("基座注册失败会抛出", False, "未抛错")
        except RuntimeError:
            check("基座注册失败会抛出", True)
        fail_register[0] = False

        fail_event[0] = True
        try:
            await plugin._report_event(render_gift(make_gift()))
            check("基座事件上报失败会抛出", False, "未抛错")
        except RuntimeError:
            check("基座事件上报失败会抛出", True)
        fail_event[0] = False

        fail_inject[0] = True
        try:
            await plugin._inject_chat_message(
                KIT.render_danmaku(SimpleNamespace(dm_type=0, msg="晚上好", uname="小明", uid=1001))
            )
            check("弹幕注入失败会抛出", False, "未抛错")
        except RuntimeError:
            check("弹幕注入失败会抛出", True)
        fail_inject[0] = False

        # 配置更新：整体重建连接与统计
        previous_stats = plugin._stats
        previous_task = plugin._consume_task
        plugin.set_plugin_config(run_config)
        await plugin.on_config_update("self", run_config, "0.1.0")

        check("配置更新后世界仍在注册表", manager.registry.names() == ["bilibili"], str(manager.registry.names()))
        check("配置更新重建统计", plugin._stats is not None and plugin._stats is not previous_stats)
        check(
            "配置更新重启消费任务",
            plugin._consume_task is not None and plugin._consume_task is not previous_task,
        )
        check("配置更新重置轮询版本号", plugin._polled_revision == -1, str(plugin._polled_revision))

        plugin._queue.put_nowait(render_interact(SimpleNamespace(msg_type=1, username="小美", uid=1007)))
        await asyncio.sleep(0.1)
        check("配置更新后仍能收到事件", "小美" in delivered[-1]["text"], str(delivered[-1]))

        await plugin.on_unload()
        check("注销走的是 world.unregister 能力", sent[-1] == WORLD_CAPABILITIES[1], str(sent[-1:]))
        check(
            "卸载时交出出站网关",
            plugin.ctx.gateway.states[-1]
            == {"gateway_name": PLUGIN_MODULE.SEND_GATEWAY_NAME, "ready": False, "platform": LIVE_PLATFORM},
            str(plugin.ctx.gateway.states[-1:]),
        )
        check("注销后基座中不再有该世界", manager.registry.names() == [], str(manager.registry.names()))
        check("卸载后消费任务已停止", plugin._consume_task is None)
        check("卸载后连接已释放", plugin._client is None and plugin._queue is None)
        check("卸载后再取统计会报错", _raises_runtime_error(plugin._require_stats))
    finally:
        PLUGIN_MODULE.BiliDanmakuClient = original_client_class
        await manager.stop()


def _raises_runtime_error(callback: Any) -> bool:
    """判断无参回调是否抛出 ``RuntimeError``。"""

    try:
        callback()
    except RuntimeError:
        return True
    return False


async def main() -> None:
    """按顺序跑完所有用例。"""

    test_render_and_client()
    test_stats()
    await test_plugin_orchestration()


asyncio.run(main())

print(f"\n=== 总计: {PASS} pass / {FAIL} fail ===")
sys.exit(0 if FAIL == 0 else 1)
