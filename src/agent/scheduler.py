"""Agent 定时任务调度：注册周期/定点任务，后台到点自动触发 run_task。

- 清单落盘 DATA_ROOT/agent_schedule.json（容错读写，损坏回退空清单）
- when 表达式：
  - every_<N>m：每 N 分钟（如 every_30m）
  - every_<N>h：每 N 小时（如 every_2h）
  - daily <HH:MM>：每天固定时刻（24 小时制，如 daily 20:00）
- 到点由 runtime 后台循环调用 due_items() 取出到期任务并触发执行；
  触发后自动计算下次时间（daily 顺延到明天），不重复触发。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.utils import config, console


def _default_path() -> Path:
    """清单默认路径：可写数据根（DATA_ROOT），禁止写源码目录。"""
    return Path(config.cfg.DATA_ROOT) / "agent_schedule.json"


class AgentScheduler:
    """定时任务清单：注册 / 移除 / 列表 / 到点触发。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(path) if path else _default_path()
        self._items: list[dict] = []  # [{id, when, task, next_run, last_run, enabled}]
        self._seq = 0

    # ---------- 持久化 ----------

    def load(self) -> None:
        """从磁盘加载清单；文件缺失/损坏回退空清单（不影响运行）。"""
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._items = data if isinstance(data, list) else []
        except (OSError, ValueError):
            self._items = []
        # 序列号取已用最大 id，保证新增 id 不重复
        self._seq = max((int(i.get("id") or 0) for i in self._items), default=0)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.warn(f"[Agent调度] 清单写入失败：{e}")

    # ---------- 时间表达式解析 ----------

    @staticmethod
    def parse_when(when: str) -> Optional[tuple[str, str]]:
        """解析时间表达式 → (类型, 参数)；非法返回 None。

        - every_<N>m / every_<N>h：固定间隔（N 为正整数）
        - daily <HH:MM>：每天固定时刻（24 小时制）
        """
        w = (when or "").strip().lower()
        if w.startswith("every_"):
            unit = w[-1]
            if unit not in ("m", "h"):
                return None
            n = w[len("every_"):-1]
            if not n.isdigit() or int(n) < 1:
                return None
            return ("every", f"{int(n)}{unit}")
        if w.startswith("daily "):
            hhmm = w[len("daily "):].strip()
            try:
                datetime.strptime(hhmm, "%H:%M")
            except ValueError:
                return None
            return ("daily", hhmm)
        return None

    # ---------- 调度计算 ----------

    @staticmethod
    def _next_run(kind: str, param: str, after: float) -> float:
        """计算下一次触发时间戳（epoch 秒）。"""
        if kind == "every":
            n, unit = int(param[:-1]), param[-1]
            seconds = n * (60 if unit == "m" else 3600)
            return after + seconds
        # daily HH:MM：下一次该时刻（今天已过则顺延到明天）
        now = datetime.fromtimestamp(after)
        hh, mm = (int(x) for x in param.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target.timestamp() <= after:
            target += timedelta(days=1)
        return target.timestamp()

    # ---------- 对外操作 ----------

    def add(self, when: str, task: str) -> tuple[bool, str]:
        """注册定时任务；返回 (是否成功, 提示文本)。"""
        parsed = self.parse_when(when)
        if parsed is None:
            return False, "时间表达式非法（支持 every_30m / every_2h / daily 20:00）"
        if not task or not task.strip():
            return False, "任务描述为空"
        self._seq += 1
        item = {
            "id": str(self._seq),
            "when": when.strip().lower(),
            "task": task.strip(),
            "next_run": self._next_run(*parsed, time.time()),
            "last_run": 0,
            "enabled": True,
        }
        self._items.append(item)
        self._save()
        return True, f"已注册定时任务 #{item['id']}：{item['when']} → {item['task']}"

    def remove(self, item_id: str) -> bool:
        """按 id 移除定时任务；返回是否真的移除。"""
        for i, item in enumerate(self._items):
            if str(item.get("id")) == str(item_id):
                self._items.pop(i)
                self._save()
                return True
        return False

    def list_text(self) -> str:
        """人类可读的任务清单文本。"""
        if not self._items:
            return "暂无定时任务（!agent_schedule add every_30m \"任务\" 添加）"
        lines = []
        for item in self._items:
            state = "开" if item.get("enabled") else "停"
            nxt = time.strftime("%m-%d %H:%M", time.localtime(item.get("next_run") or 0))
            lines.append(
                f"#{item.get('id')} [{state}] {item.get('when')}（下次 {nxt}）："
                f"{item.get('task')}")
        return "\n".join(lines)

    # ---------- 到点触发 ----------

    def due_items(self) -> list[dict]:
        """取出所有已到触发时刻且启用的任务，并推进各自的 next_run。

        由后台循环周期调用（未启用清单时返回空）。表达式非法时停用该任务
        防止空转；到期任务写入 last_run 后返回供调用方触发执行。
        """
        now = time.time()
        due = []
        for item in self._items:
            if not item.get("enabled"):
                continue
            nxt = item.get("next_run") or 0
            if nxt > now:
                continue
            due.append(item)
            item["last_run"] = now
            parsed = self.parse_when(item.get("when") or "")
            if parsed is None:
                item["enabled"] = False  # 表达式非法则停用，避免空转
            else:
                item["next_run"] = self._next_run(*parsed, now)
        if due:
            self._save()
        return due
