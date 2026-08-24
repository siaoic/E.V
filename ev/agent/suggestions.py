"""自动化建议机制与蓝图（consent-first，5.14）。

对标 hermes cron/suggestions.py + cron/blueprint_catalog.py 精简落地：

- consent-first：建议绝不自动建 job，必须主播 `!suggestions accept` 批准；
- dedup 永久锁死：同一 dedup_key 的建议 dismiss 后不再出现；
- MAX_PENDING=5：超出拒绝新增，防止建议堆积；
- accept 透传 AgentScheduler.add（无第二套引擎），只落 when/task；
- 蓝图：预置 E.V 场景的周期任务模板，`!blueprint <key> [when]` 填槽后
  转成建议挂起，仍需主播 accept（consent-first 一致）。

存储：
- DATA_ROOT/agent_suggestions.json：挂起中的建议列表
- DATA_ROOT/agent_suggestions_dismissed.json：被锁死的 dedup_key 列表
"""

from __future__ import annotations

import json
import os
import time
import uuid

from ev.utils import config, console

# 挂起建议上限：超出拒绝新增（对标 hermes MAX_PENDING=5）
MAX_PENDING = 5

# 自动化蓝图目录（5.14）：key → {title, description, when, task}
# when 为默认调度表达式，!blueprint 可传入自定义 when 覆盖
BLUEPRINTS: dict[str, dict] = {
    "weekly-evolution-review": {
        "title": "每周进化回顾",
        "description": "定期把自我进化沉淀（画像/经验/话术/技能变化）汇总审阅，持续校准",
        "when": "daily 09:30",
        "task": "回顾最近一段时间的自我进化沉淀（画像/经验/话术/技能变化），"
                "总结得失并输出改进建议",
    },
    "daily-brief": {
        "title": "每日状态简报",
        "description": "开播前自动汇总技能清单、挂起建议与近期观众负反馈",
        "when": "daily 08:00",
        "task": "生成今日直播准备简报：检查技能清单、挂起建议与近期观众负反馈",
    },
    "vts-emo-check": {
        "title": "VTS 表情健康检查",
        "description": "周期性检查 VTS 表情模型与热点映射可用性，异常时报告",
        "when": "daily 18:00",
        "task": "检查 VTS 表情模型文件与热点映射是否正常，异常时报告",
    },
    "profile-review": {
        "title": "观众画像回顾",
        "description": "画像随观众变化滚动更新，定期找出过时/矛盾条目",
        "when": "every_24h",
        "task": "回顾观众画像清单，找出过时或矛盾条目并提出修正建议",
    },
}


def _pending_path() -> str:
    """挂起建议存储路径（可写数据根）。"""
    return os.path.join(config.cfg.DATA_ROOT, "agent_suggestions.json")


def _dismissed_path() -> str:
    """被锁死 dedup_key 存储路径（可写数据根）。"""
    return os.path.join(config.cfg.DATA_ROOT, "agent_suggestions_dismissed.json")


def _load_pending() -> list[dict]:
    """读取挂起建议列表（缺失/损坏回退空列表）。"""
    try:
        with open(_pending_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_pending(items: list[dict]) -> None:
    """覆写挂起建议列表（写失败只告警）。"""
    try:
        os.makedirs(os.path.dirname(_pending_path()), exist_ok=True)
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except OSError as e:
        console.warn(f"[建议] 写入挂起建议失败：{e}")


def _load_dismissed() -> list[str]:
    """读取已锁死 dedup_key 列表（缺失/损坏回退空列表）。"""
    try:
        with open(_dismissed_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_dismissed(keys: list[str]) -> None:
    """覆写已锁死 dedup_key 列表（写失败只告警）。"""
    try:
        os.makedirs(os.path.dirname(_dismissed_path()), exist_ok=True)
        with open(_dismissed_path(), "w", encoding="utf-8") as f:
            json.dump(keys, f, ensure_ascii=False, indent=2)
    except OSError as e:
        console.warn(f"[建议] 写入锁死记录失败：{e}")


def add_suggestion(title: str, description: str, source: str,
                   job_spec: dict, dedup_key: str) -> bool:
    """登记一条挂起建议（consent-first，绝不自动建 job）。

    幂等：同 dedup_key 已挂起或已被 dismiss 锁死 → 返回 False 不新增；
    挂起数量达 MAX_PENDING 上限 → 返回 False 拒绝新增。
    """
    title = (title or "").strip()
    dedup_key = (dedup_key or "").strip()
    if not title or not dedup_key:
        return False
    if dedup_key in _load_dismissed():
        console.dim(f"[建议] 已被锁死（dismiss 过）：{title}")
        return False
    items = _load_pending()
    if any(it.get("dedup_key") == dedup_key for it in items):
        return False  # 幂等：已挂起同源建议
    if len(items) >= MAX_PENDING:
        console.dim(f"[建议] 挂起已达上限（{MAX_PENDING}），拒绝新增：{title}")
        return False
    items.append({
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "description": (description or "").strip(),
        "source": (source or "").strip() or "catalog",
        "job_spec": job_spec if isinstance(job_spec, dict) else {},
        "dedup_key": dedup_key,
        "created": time.time(),
    })
    _save_pending(items)
    console.dim(f"[建议] 新增挂起建议：{title}（来源 {source}，"
                f"!suggestions accept 后转定时任务）")
    return True


def _find_pending(key: str) -> dict | None:
    """按 #id 或标题前缀定位一条挂起建议。"""
    key = (key or "").strip()
    if not key:
        return None
    items = _load_pending()
    if key.startswith("#"):
        target = key[1:].strip()
        return next((it for it in items if it.get("id") == target), None)
    return next((it for it in items if it.get("title") == key), None)


def accept_suggestion(key: str) -> tuple[bool, str]:
    """主播批准建议：透传 AgentScheduler.add 建定时任务并移除建议。"""
    item = _find_pending(key)
    if item is None:
        return False, "未找到该建议（!suggestions 查看挂起列表）"
    spec = item.get("job_spec") or {}
    when = (spec.get("when") or "").strip()
    task = (spec.get("task") or "").strip()
    if not (when and task):
        return False, f"建议「{item.get('title')}」缺少调度参数，无法接受"
    # consent-first：仅透传 scheduler 的 add（无第二套引擎），失败回显原因
    from .scheduler import AgentScheduler
    scheduler = AgentScheduler()
    scheduler.load()
    ok, msg = scheduler.add(when, task)
    if not ok:
        return False, msg
    _save_pending([it for it in _load_pending() if it.get("id") != item.get("id")])
    console.ok(f"[建议] 已接受并转定时任务：{item.get('title')}（{when}）")
    return True, f"已接受「{item.get('title')}」→ {msg}"


def dismiss_suggestion(key: str) -> tuple[bool, str]:
    """主播否决建议：移除挂起并锁死 dedup_key（该源建议不再出现）。"""
    item = _find_pending(key)
    if item is None:
        return False, "未找到该建议（!suggestions 查看挂起列表）"
    _save_pending([it for it in _load_pending() if it.get("id") != item.get("id")])
    dedup_key = (item.get("dedup_key") or "").strip()
    if dedup_key:
        keys = _load_dismissed()
        if dedup_key not in keys:
            keys.append(dedup_key)
            _save_dismissed(keys)
    console.dim(f"[建议] 已否决并锁死：{item.get('title')}")
    return True, f"已否决「{item.get('title')}」并锁死该来源建议"


def list_suggestions_text() -> str:
    """挂起建议的人类可读清单（空列表给出用法提示）。"""
    items = _load_pending()
    if not items:
        return "暂无挂起建议（!blueprint weekly-evolution-review 可预置周期任务）"
    lines = []
    for it in items:
        spec = it.get("job_spec") or {}
        lines.append(
            f"#{it.get('id')} [{it.get('source')}] {it.get('title')}"
            f"（{spec.get('when') or '?'}）：{it.get('description') or ''}")
    lines.append("用法：!suggestions accept #<id> 批准转定时任务；"
                 "!suggestions dismiss #<id> 否决并锁死")
    return "\n".join(lines)


def _when_valid(when: str) -> bool:
    """校验时间表达式是否合法（透传 scheduler.parse_when 判定）。"""
    from .scheduler import AgentScheduler
    return AgentScheduler.parse_when(when) is not None


def blueprint_suggestion(key: str, when: str = "") -> tuple[bool, str]:
    """把蓝图模板转成挂起建议（填槽：when 可覆盖默认调度表达式）。"""
    bp = BLUEPRINTS.get((key or "").strip())
    if bp is None:
        return True, ("未找到蓝图（可用：" + "、".join(BLUEPRINTS) + "）")
    spec = {"when": (when or bp["when"]).strip(), "task": bp["task"]}
    if not _when_valid(spec["when"]):
        return True, "时间表达式非法（支持 every_30m / every_2h / daily 20:00）"
    ok = add_suggestion(
        title=bp["title"], description=bp["description"],
        source="blueprint", job_spec=spec, dedup_key=f"blueprint:{key}")
    if not ok:
        return True, f"「{bp['title']}」未新增（已挂起/已锁死/超出上限）"
    return True, (f"「{bp['title']}」已挂起（{spec['when']}），"
                  "确认后 !suggestions accept 转定时任务")


def handle_suggestions_command(raw: str) -> tuple[bool, str]:
    """处理主播命令：!suggestions [accept|dismiss <key>] / !blueprint <key> [when]。

    返回 (是否命令, 回复文本)；非命令返回 (False, "")，交由正常对话。
    """
    raw = (raw or "").strip()
    if raw.startswith("!suggestions"):
        parts = raw.split()
        if len(parts) == 1:
            return True, list_suggestions_text()
        sub = parts[1].lower()
        if sub == "accept" and len(parts) >= 3:
            return accept_suggestion(parts[2])
        if sub == "dismiss" and len(parts) >= 3:
            return dismiss_suggestion(parts[2])
        return True, ("用法：!suggestions 查看 | !suggestions accept #<id> 批准 | "
                      "!suggestions dismiss #<id> 否决")
    if raw.startswith("!blueprint"):
        parts = raw.split()
        if len(parts) >= 2:
            return blueprint_suggestion(parts[1], parts[2] if len(parts) >= 3 else "")
        return True, ("用法：!blueprint <key> [when]，可用蓝图："
                      + "、".join(BLUEPRINTS))
    return False, ""
