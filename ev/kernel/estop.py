"""全局急停（3.13，对标 Hermes agent/estop.py）：哨兵文件 DATA_ROOT/ESTOP。

存在哨兵文件即视为急停（fail-safe：哨兵文件不可读/内容损坏仍按急停处理，
急停必须无条件保持）。急停生效时，Agent 对高危工具（shell / 文件写删 /
记忆写删 / 外发消息）拒绝执行——防止危险操作在失控状态下继续发生。

- engage(reason)：写哨兵文件（幂等，重写保留最新 reason）
- disengage()：删除哨兵文件
- is_engaged()：一次 stat 判定；stat 异常按急停处理（fail-closed）
- is_blocked(tool_name)：急停开关开启 且 急停生效 且 工具在拦截集内

运行时可写路径统一走 cfg.DATA_ROOT（项目红线），哨兵文件也落在 DATA_ROOT。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ev.utils import config, console

SENTINEL_NAME = "ESTOP"

# 急停拦截的工具集（Agent 工具名）：shell 执行 / 文件写删 / 记忆写删 /
# 外发网络。新增高危工具时在此追加（工具注册表与 executor 共用此集合）。
ESTOP_BLOCKED_TOOLS = frozenset({
    "run_shell", "delete_file", "delete_directory",
    "write_file", "append_file",
    "remember_fact", "forget_memory", "memory",
    "write_diary", "get_weather",
})

# 组件级"本次急停已告警"标记：每个组件在急停期间只告警一次，不刷屏
_log_lock = threading.Lock()
_logged_components: set[str] = set()


def estop_enabled() -> bool:
    """3.13 急停总开关：关闭时 is_blocked 恒 False（行为与现状一致）。"""
    try:
        return bool(config.cfg.AGENT_ESTOP_ENABLED)
    except Exception:
        return False


def sentinel_path() -> Path:
    """哨兵文件路径（可写数据根，E_V_DATA_DIR 重定向后随之迁移）。"""
    return Path(config.cfg.DATA_ROOT) / SENTINEL_NAME


def is_engaged() -> bool:
    """是否处于急停状态（一次 stat）。

    Fail SAFE：stat 异常（权限/IO 故障）时按急停处理——急停绝不能因为
    文件系统异常被悄悄解除。
    """
    try:
        return sentinel_path().exists()
    except OSError:
        return True


def engage(reason: Optional[str] = None) -> Path:
    """创建急停哨兵文件（幂等；重复 engage 更新 reason）。"""
    path = sentinel_path()
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        # 尽力而为：空/半截哨兵文件同样算急停（fail-safe）
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
    return path


def disengage() -> bool:
    """解除急停（删除哨兵文件）；未处于急停返回 False。"""
    try:
        sentinel_path().unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def get_state() -> Optional[dict]:
    """返回 {"reason", "engaged_at"}；未急停返回 None。

    哨兵文件不可读/内容损坏时仍按急停处理（字段为 None），急停本身权威。
    """
    path = sentinel_path()
    if not path.exists():
        return None
    reason = None
    engaged_at = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            reason = raw.get("reason") or None
            engaged_at = raw.get("engaged_at") or None
    except (OSError, ValueError):
        pass
    return {"reason": reason, "engaged_at": engaged_at}


def is_blocked(tool_name: str) -> bool:
    """急停是否拦截该工具：开关开启 + 急停生效 + 工具在拦截集内。

    拦截集外的只读工具（查时间/读技能/检索会话等）不受急停影响。
    """
    if not estop_enabled():
        return False
    if tool_name not in ESTOP_BLOCKED_TOOLS:
        return False
    if not is_engaged():
        return False
    warn_once(tool_name)
    return True


def warn_once(component: str) -> None:
    """对每个组件在本次急停期间只告警一次（解除后重新布防）。"""
    with _log_lock:
        if component in _logged_components:
            return
        _logged_components.add(component)
    console.warn(
        f"[急停] 全局急停生效（{sentinel_path()}），高危工具「{component}」已拒绝执行；"
        f"删除哨兵文件可解除")
