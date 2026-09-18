"""直播间消息的规范化类型与中文渲染。

这一层只做「blivedm 结构化消息 → 规范化的中文文本」的纯转换，不持有连接、不依赖插件上下文，
因此可以脱离宿主单独测试。blivedm 的原始消息对象按鸭子类型使用，类型注解只用于说明来源。

产出分两类：

- :class:`LiveChatMessage`：弹幕与醒目留言，正文由插件注入成普通聊天消息，走完整聊天链路；
- :class:`LiveEvent`：礼物 / 上舰 / 进场关注，作为世界事件与世界状态，供 AI 感知直播间氛围。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class LiveEventKind(str, Enum):
    """直播间世界事件种类。"""

    GIFT = "gift"
    """礼物"""

    GUARD = "guard"
    """上舰（开通 / 续费舰队）"""

    INTERACT = "interact"
    """进入直播间、关注主播等互动"""


# 舰队等级 → 中文名（blivedm 的取值：0 非舰队，1 总督，2 提督，3 舰长）
GUARD_NAMES: Dict[int, str] = {1: "总督", 2: "提督", 3: "舰长"}

# INTERACT_WORD_V2 的 msg_type → 中文描述
INTERACT_TEXTS: Dict[int, str] = {
    1: "进入直播间",
    2: "关注了主播",
    3: "分享了直播间",
    4: "特别关注了主播",
    5: "与主播互粉",
    6: "为主播点赞",
}

# 金瓜子与人民币的换算：blivedm 注释明确「1000 金瓜子 = 1 元」。
COIN_PER_YUAN = 1000


def guard_name(level: int) -> str:
    """把舰队等级转成中文名；未知等级原样暴露，不静默当成非舰队。"""

    return GUARD_NAMES.get(level, f"未知舰队等级{level}")


def coin_to_yuan(coin: int) -> str:
    """把金瓜子数换算成人民币展示串。"""

    return f"{coin / COIN_PER_YUAN:.2f} 元"


@dataclass(frozen=True)
class LiveChatMessage:
    """一条要注入成聊天消息的直播间消息。

    Attributes:
        text: 送进聊天流的正文，已包含必要的类型标记（例如醒目留言的金额）。
        uid: 发送者 uid；未登录连接时 blivedm 会给出 ``0``。
        uname: 发送者昵称；未登录连接时可能是打码昵称。
    """

    text: str
    uid: int = 0
    uname: str = ""
    superchat_yuan: float = 0.0
    """醒目留言的人民币金额；普通弹幕为 0（供准入预算判定重要度）。"""


@dataclass(frozen=True)
class LiveEvent:
    """一条规范化后的直播间事件。

    Attributes:
        kind: 事件种类。
        text: 已渲染好的中文描述（不含世界前缀）。
        uid: 触发用户的 uid，未登录或未知时为 ``0``。
        uname: 触发用户昵称。
        coin: 事件涉及的金瓜子价值，``0`` 表示不适用（例如弹幕、进场）。
        payload: 关键原始字段摘要，供插件侧做策略判断，不进入模型上下文。
    """

    kind: LiveEventKind
    text: str
    uid: int = 0
    uname: str = ""
    superchat_yuan: float = 0.0
    """醒目留言的人民币金额；普通弹幕为 0（供准入预算判定重要度）。"""
    coin: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)


def render_gift(message: Any) -> LiveEvent:
    """渲染礼物（``blivedm.models.web.GiftMessage``）。

    ``coin_type`` 为 ``gold`` 时 ``total_coin`` 是金瓜子（可换算人民币）；
    为 ``silver`` 时是免费银瓜子，只报数量不做价值渲染。
    """

    coin_type = str(message.coin_type or "")
    coin = int(message.total_coin or 0)
    if coin_type == "gold":
        value_text = f"，{coin} 金瓜子 ≈ {coin_to_yuan(coin)}"
    elif coin_type == "silver":
        value_text = f"，{coin} 银瓜子"
    else:
        value_text = ""

    return LiveEvent(
        kind=LiveEventKind.GIFT,
        text=(
            f"观众【{message.uname}】送出「{message.gift_name}」×{int(message.num)}"
            f"（{message.action or '赠送'}{value_text}）"
        ),
        uid=int(message.uid),
        uname=str(message.uname),
        coin=coin if coin_type == "gold" else 0,
        payload={
            "gift_name": str(message.gift_name),
            "num": int(message.num),
            "coin_type": coin_type,
            "guard_level": int(message.guard_level or 0),
        },
    )


def render_guard(message: Any) -> LiveEvent:
    """渲染上舰（``blivedm.models.web.GuardBuyMessage``）。"""

    level = int(message.guard_level or 0)
    price = int(message.price or 0)
    return LiveEvent(
        kind=LiveEventKind.GUARD,
        text=(
            f"观众【{message.username}】开通了「{guard_name(level)}」×{int(message.num)}"
            f"（单价 {price} 金瓜子 ≈ {coin_to_yuan(price)}）"
        ),
        uid=int(message.uid),
        uname=str(message.username),
        coin=price,
        payload={"guard_level": level, "num": int(message.num), "source": 0},
    )


def render_user_toast(message: Any) -> LiveEvent:
    """渲染上舰的另一种消息（``blivedm.models.web.UserToastV2Message``）。"""

    level = int(message.guard_level or 0)
    price = int(message.price or 0)
    unit = str(message.unit or "")
    unit_text = f" · {unit}" if unit else ""
    return LiveEvent(
        kind=LiveEventKind.GUARD,
        text=(
            f"观众【{message.username}】开通了「{guard_name(level)}」×{int(message.num)}{unit_text}"
            f"（单价 {price} 金瓜子 ≈ {coin_to_yuan(price)}）"
        ),
        uid=int(message.uid),
        uname=str(message.username),
        coin=price,
        payload={"guard_level": level, "num": int(message.num), "source": int(message.source or 0)},
    )


def render_danmaku(message: Any) -> LiveChatMessage:
    """渲染弹幕（``blivedm.models.web.DanmakuMessage``）为聊天消息正文。"""

    return LiveChatMessage(
        text=str(message.msg),
        uid=int(message.uid),
        uname=str(message.uname),
    )


def render_super_chat(message: Any) -> LiveChatMessage:
    """渲染醒目留言（``blivedm.models.web.SuperChatMessage``）为聊天消息正文。

    醒目留言既要进聊天流（观众说的话），也要让 AI 知道金额，因此把金额写进正文标记：
    ``[SC ¥30] 内容``。醒目留言不再单独作为世界事件上报，避免同一句话被 AI 看到两遍。
    """

    return LiveChatMessage(
        text=f"[SC ¥{int(message.price or 0)}] {message.message}",
        uid=int(message.uid),
        uname=str(message.uname),
        superchat_yuan=float(message.price or 0),
    )


def render_interact(message: Any) -> LiveEvent:
    """渲染进场 / 关注等互动（``blivedm.models.web.InteractWordV2Message``）。"""

    msg_type = int(message.msg_type or 0)
    action_text = INTERACT_TEXTS.get(msg_type, f"触发了未知互动(msg_type={msg_type})")
    return LiveEvent(
        kind=LiveEventKind.INTERACT,
        text=f"观众【{message.username}】{action_text}",
        uid=int(message.uid),
        uname=str(message.username),
        payload={"msg_type": msg_type},
    )


def describe_connection(*, connected: bool, logged_in_uid: Optional[int], last_error: str) -> str:
    """把连接状态渲染成一句中文。"""

    if connected:
        if logged_in_uid:
            return f"已连接（已登录 uid={logged_in_uid}）"
        return "已连接（未登录，事件的用户身份可能被 B 站打码）"
    if last_error:
        return f"未连接（{last_error}）"
    return "未连接"
