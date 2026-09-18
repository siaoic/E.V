"""结构化 JSON → 中文文本渲染。

渲染只发生在 Python 侧（Node 侧只报结构化 JSON，不做任何中文拼接），
改文案不需要动 Node。方块与生物名保留 mineflayer 的英文名，避免
翻译表与游戏版本漂移不一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 事件类别中文名（用在任务结果等场景）
_KIND_ZH = {"hostile": "敌对", "passive": "被动", "player": "玩家"}


def _format_position(position: Optional[Dict[str, Any]]) -> str:
    if not position:
        return "未知位置"
    return f"({position.get('x')}, {position.get('y')}, {position.get('z')})"


def _format_entities(entities: List[Dict[str, Any]]) -> str:
    """把实体摘要渲染成「2 只 zombie（最近 4.5 格）」式短句。"""

    if not entities:
        return "周围没有生物"
    parts: List[str] = []
    for entity in entities:
        kind_zh = _KIND_ZH.get(str(entity.get("kind")), "")
        name = entity.get("name", "?")
        nearest = entity.get("nearest")
        distance = f"，最近 {nearest} 格" if isinstance(nearest, (int, float)) else ""
        prefix = f"{kind_zh}生物" if kind_zh and kind_zh != "玩家" else ""
        parts.append(f"{prefix}{name}×{entity.get('count', 0)}{distance}".replace("生物玩家", "玩家"))
    return "；".join(parts)


def _format_inventory(inventory: List[Dict[str, Any]]) -> str:
    if not inventory:
        return "背包是空的"
    return "、".join(f"{item.get('name')}×{item.get('count', 0)}" for item in inventory)


def render_state(state: Dict[str, Any], *, connected: bool = True) -> str:
    """渲染完整状态快照（world.state / world_observe 用）。"""

    if not connected or not state:
        return "未连接到 Minecraft 服务器。"

    health = state.get("health")
    food = state.get("food")
    parts = [f"你在 {_format_position(state.get('position'))}"]
    if health is not None:
        parts.append(f"血量 {health}/{state.get('maxHealth', 20)}")
    if food is not None:
        parts.append(f"饥饿 {food}/20")
    if state.get("blockBelow"):
        parts.append(f"脚下是 {state['blockBelow']}")

    threat = state.get("hostileNearby")
    hostile_count = state.get("hostileCount") or 0
    if threat:
        parts.append(f"⚠ 有敌对生物逼近：{threat.get('name')}（{threat.get('distance')} 格），附近共 {hostile_count} 只敌对生物")

    entities = state.get("entities") or []
    if entities:
        parts.append(f"周围生物：{_format_entities(entities)}")

    players = state.get("playersOnline") or []
    if players:
        parts.append(f"在线玩家：{'、'.join(players)}")

    parts.append(f"背包：{_format_inventory(state.get('inventory') or [])}")
    head = "，".join(parts[:3])
    rest = "；".join(parts[3:])
    return f"{head}。{rest}。" if rest else f"{head}。"


def render_changes(changed: List[str], state: Dict[str, Any]) -> str:
    """把一般变化渲染成一句话（debounce 事件用）。"""

    descriptions: List[str] = []
    for field in changed:
        if field == "position":
            descriptions.append(f"你移动到了 {_format_position(state.get('position'))}")
        elif field == "inventory" or field == "inventoryTotalCount":
            descriptions.append("背包发生了变化")
        elif field == "blockBelow":
            descriptions.append(f"脚下变成了 {state.get('blockBelow')}")
        elif field == "health":
            descriptions.append(f"血量现在是 {state.get('health')}/20")
        elif field == "food":
            descriptions.append(f"饥饿度现在是 {state.get('food')}/20")
        elif field == "entities" or field == "hostileNearby" or field == "hostileCount":
            descriptions.append(f"周围生物有变化：{_format_entities(state.get('entities') or [])}")
    if not descriptions:
        descriptions.append("周围环境有变化")
    return "；".join(descriptions) + "。"


def render_significant(state: Dict[str, Any]) -> str:
    """渲染显著变化（preempt 事件用）：当前只有敌对生物出现/增多一种来源。"""

    threat = state.get("hostileNearby")
    if threat:
        return f"⚠ 有敌对生物逼近：{threat.get('name')}（{threat.get('distance')} 格）！"
    return "⚠ 周围出现危险！"


def render_event(frame: Dict[str, Any]) -> str:
    """渲染离散事件帧（damage / death / chat / player_join / player_leave）。"""

    frame_type = frame.get("type")
    if frame_type == "damage":
        amount = frame.get("amount")
        health = frame.get("health")
        cause = str(frame.get("cause") or "").strip()
        cause_text = f"（{cause}）" if cause else ""
        return f"你受到了伤害{cause_text}，血量降到 {health}/20（本次伤害 {amount} 点）。"
    if frame_type == "death":
        return "你死了，刚刚已经重生。"
    if frame_type == "chat":
        player = frame.get("player", "?")
        return f"{player} 在游戏里对你说：{frame.get('text', '')}"
    if frame_type == "player_join":
        return f"玩家 {frame.get('player', '?')} 进入了服务器。"
    if frame_type == "player_leave":
        return f"玩家 {frame.get('player', '?')} 离开了服务器。"
    return f"发生了事件：{frame_type}"


def render_task_done(frame: Dict[str, Any]) -> str:
    """渲染动作执行结果（task_done 事件文本）。"""

    detail = str(frame.get("detail") or "").strip()
    if frame.get("ok"):
        elapsed = frame.get("elapsedMs")
        suffix = f"（耗时 {elapsed}ms）" if isinstance(elapsed, int) and elapsed > 2000 else ""
        return f"动作执行成功：{detail}{suffix}"
    return f"动作执行失败：{detail or '未知原因'}"


def render_scout(data: Dict[str, Any]) -> str:
    """渲染 scout 观察结果（附加在任务结果事件里）。"""

    scout = (data or {}).get("scout") or {}
    blocks = scout.get("blocksNearby") or []
    if not blocks:
        return "周围 6 格内没有可辨认的方块。"
    summary = "、".join(f"{block.get('name')}×{block.get('count', 0)}" for block in blocks)
    return f"周围 6 格内的方块：{summary}。"
