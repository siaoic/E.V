"""World 框架状态只读 API（WebUI「世界状态」页数据源）。

全部为只读端点，数据直接取自世界基座（``src/worlds/``）、
VTuber 口型桥（``src/maisaka/vtuber_bridge/``）与工具调用落库；
基座还没绑会话 / 没有世界 / VTS 未连接时如实返回，不伪造在线。
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from src.common.database.database import get_db_session
from src.common.database.database_model import ToolRecord
from src.common.logger import get_logger
from src.config.config import global_config
from src.webui.dependencies import require_auth
from src.worlds.manager import get_world_manager

logger = get_logger("webui")

router = APIRouter(prefix="/worlds", tags=["worlds"], dependencies=[Depends(require_auth)])

# 世界相关的工具名前缀（世界插件原生工具 + 基座通用工具 + 表情工具）
WORLD_TOOL_PREFIXES = ("world_", "minecraft_", "pvz_", "vtuber_")


@router.get("/overview")
async def get_overview() -> Dict[str, Any]:
    """世界框架总览：总开关、配置与各世界清单及在线状态。"""

    manager = get_world_manager()
    settings = global_config.worlds
    descriptors = manager.registry.descriptors()
    return {
        "enabled": bool(settings.enabled),
        "live_platform": settings.live_platform,
        "stream_id": settings.stream_id,
        "inject_state": bool(settings.inject_state),
        "stale_after_seconds": settings.stale_after_seconds,
        "event_throttle_seconds": settings.event_throttle_seconds,
        "change_poll_seconds": settings.change_poll_seconds,
        "delayed_sources": list(settings.delayed_sources or []),
        "delayed_seconds": settings.delayed_seconds,
        "bound_session_id": manager.bound_session_id,
        "pending_event_count": manager.pending_event_count,
        "worlds": [
            {
                "name": descriptor.name,
                "display_name": descriptor.display_name,
                "plugin_id": descriptor.plugin_id,
                "polls_changes": descriptor.polls_changes,
                "supports_delayed": descriptor.supports_delayed,
                "delayed_active": manager.delayed_seconds_for(descriptor.name) is not None,
            }
            for descriptor in descriptors
        ],
    }


@router.get("/state")
async def get_state() -> Dict[str, Any]:
    """各世界当前缓存的中文状态文本（实时 + 延迟两个视图）。"""

    manager = get_world_manager()
    worlds: List[Dict[str, Any]] = []
    for name in manager.registry.names():
        views = manager.state_views(name)
        descriptor = manager.registry.get(name)
        worlds.append(
            {
                "name": name,
                "display_name": descriptor.display_name if descriptor else name,
                "realtime": views["realtime"],
                "delayed": views["delayed"],
            }
        )
    return {"worlds": worlds}


@router.get("/events")
async def get_events() -> Dict[str, Any]:
    """最近世界事件流（环形缓冲，含被节流丢弃的）。"""

    manager = get_world_manager()
    return {"events": manager.event_history, "pending_count": manager.pending_event_count}


@router.get("/tools")
async def get_tools() -> Dict[str, Any]:
    """近期世界相关工具调用（复用工具调用落库，取最近 50 条）。"""

    from sqlalchemy import or_

    records: List[Dict[str, Any]] = []
    try:
        prefix_filters = [ToolRecord.tool_name.like(f"{prefix}%") for prefix in WORLD_TOOL_PREFIXES]
        with get_db_session() as session:
            rows = (
                session.query(ToolRecord)
                .filter(or_(*prefix_filters))
                .order_by(ToolRecord.timestamp.desc())
                .limit(50)
                .all()
            )
            for row in rows:
                records.append(
                    {
                        "tool_name": row.tool_name,
                        "session_id": row.session_id,
                        "timestamp": row.timestamp.strftime("%m-%d %H:%M:%S") if row.timestamp else "",
                    }
                )
    except Exception as exc:  # noqa: BLE001 - 落库不可用时如实返回空列表与错误
        logger.error(f"查询世界工具调用记录失败: {exc!r}")
        return {"tools": [], "error": str(exc)}
    return {"tools": records}


@router.get("/vtuber")
async def get_vtuber() -> Dict[str, Any]:
    """VTuber 口型桥连接状态（W4 未启用/未连接时如实呈现）。"""

    from src.maisaka.vtuber_bridge import state as vtuber_state

    return vtuber_state.snapshot()
