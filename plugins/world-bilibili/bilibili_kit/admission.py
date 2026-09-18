# -*- coding: utf-8 -*-
"""观众事件准入预算（移植自 Cortico ``src/worlds/bilibili/audience-admission.ts``）。

解决「大直播间弹幕洪水」：拥挤时同批弹幕若超过预算（行数 / 估算 token），
按车道优先级准入——

- ``critical``：醒目留言（SC，观众花了钱的发言）始终放行，但计入总预算；
- ``important``：重要观众（舰队 35 天内 / SC 达额 30 天内 / 互动达量 30 天内）
  优先，且占总预算的比例不超过 ``important_budget_share``；
- ``ordinary``：普通弹幕，在剩余预算内放行，超出即丢弃。

公平性沿用 Cortico 的 ``fair_debt``：重要观众的消息因预算被丢弃时记 +1 债，
被准入时 −1，债务高的观众后续优先。

与 Cortico 原实现的**刻意差异**（本插件逐条消费消息，不做批投影）：

1. 准入按「时间窗口预算」执行：每个 ``window_seconds`` 窗口共享
   行 / token 预算计数，消息到达即判「装得下就放行」——Chat 注入不能为凑批加延迟；
2. 拥挤信号来自 blivedm 心跳的 ``popularity``（人气值，B 站已弃用但仍在推送），
   阈值语义与原实现的「高能榜人数」不同，需按直播间实际人气档位调参；
   **没有新鲜拥挤信号时限流完全不生效**（与原实现一致：默认零行为变化）；
3. 原实现的盐化哈希随机排序用于批内公平抽样，单条投递下没有可重排序的批，
   故省略；ordinary 的公平性由「先到先得 + 窗口轮转」承担；
4. 原实现只在「整批超预算」时启用车道分配；这里拥挤期间车道限额即生效
   （important 受车道限额约束，ordinary 仅受总预算约束），否则单条投递下
   车道限额永远轮不到生效；
5. 原实现用 fair_debt 做批内选择排序；这里改为「债务 ≥3 的重要观众越过车道
   限额回补一条（仍受总预算约束）」，让债务在单条投递下有真实效果。

台账（重要观众资格）持久化到 JSON（原子替换写），跨重启保留舰队 / SC / 互动资格。
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DAY_MS = 24 * 60 * 60 * 1000
MINUTE_MS = 60 * 1000
LEDGER_VERSION = 1
FAIR_DEBT_LIMIT = 1000


def estimate_tokens(text: str) -> int:
    """估算文本 token 占用（与 Cortico 同款：CJK×0.6 + 其他×0.3，向上取整）。"""
    cjk = 0
    other = 0
    for ch in text:
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF
            or 0x3000 <= code <= 0x30FF
            or 0xFF00 <= code <= 0xFFEF
        ):
            cjk += 1
        else:
            other += 1
    return math.ceil(cjk * 0.6 + other * 0.3)


def line_count(text: str) -> int:
    return len(text.splitlines()) or 1


def uid_of(value: str) -> Optional[str]:
    normalized = str(value).strip()
    return normalized if normalized.isdigit() and normalized != "0" else None


@dataclass
class AudienceTuning:
    """准入调参（默认值与 Cortico 对齐；人气值阈值需按直播间调参）。"""

    superchat_single_yuan: float = 30.0
    superchat_rolling_yuan: float = 50.0
    superchat_window_ms: float = 30 * DAY_MS
    superchat_lease_ms: float = 30 * DAY_MS
    guard_retention_ms: float = 35 * DAY_MS
    interaction_window_ms: float = 30 * DAY_MS
    interaction_lease_ms: float = 30 * DAY_MS
    interaction_minutes_per_stream: int = 25
    interaction_stream_count: int = 1
    crowded_on: float = 200.0
    crowded_off: float = 170.0
    crowd_release_ms: float = 120_000.0
    crowd_fresh_ms: float = 90_000.0
    crowd_stale_hold_ms: float = 300_000.0
    line_budget: int = 114
    token_budget: int = 1061
    important_budget_share: float = 0.5
    window_seconds: float = 10.0
    persist_seconds: float = 30.0


@dataclass
class _SuperchatEntry:
    at: int
    cents: int


@dataclass
class _InteractionStream:
    last_at: int
    minutes: List[int] = field(default_factory=list)
    qualified_at: Optional[int] = None


@dataclass
class _ViewerLedger:
    last_seen_at: int
    guard_expires_at: int = 0
    superchat_expires_at: int = 0
    superchats: List[_SuperchatEntry] = field(default_factory=list)
    interaction_expires_at: int = 0
    interaction_streams: Dict[str, _InteractionStream] = field(default_factory=dict)
    fair_debt: int = 0


class AudienceAdmission:
    """观众事件准入控制器：台账内存更新同步，持久化由调用方定期触发。"""

    def __init__(
        self,
        *,
        room_id: int,
        file: Optional[Path] = None,
        tuning: Optional[AudienceTuning] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.room_id = int(room_id)
        self.file = Path(file) if file is not None else None
        self.tuning = tuning or AudienceTuning()
        self._now = now or (lambda: int(time.time() * 1000))

        self._salt = uuid.uuid4().hex
        self._open_stream_id: Optional[str] = None
        self._viewers: Dict[str, _ViewerLedger] = {}
        self._dirty = False
        self._last_persist_at = 0
        self._last_maintenance_at = 0

        self._crowd_count: Optional[float] = None
        self._crowd_observed_at: Optional[int] = None
        self._crowded = False
        self._below_release_since: Optional[int] = None

        # 当前窗口的预算用量
        self._window_started_at = self._now()
        self._used_lines = 0
        self._used_tokens = 0
        self._important_lines = 0
        self._important_tokens = 0

        self.totals = {
            "admitted": 0,
            "dropped": 0,
            "limited_windows": 0,
            "windows": 0,
            "dropped_tokens": 0,
        }
        self._last_drop_reason = ""
        self._persistence_error = ""
        self._load()

    # ------------------------------------------------------------------ 台账与观察

    def start_stream(self, live_started_at: Any = None) -> str:
        started = str(live_started_at).strip() if live_started_at is not None else ""
        stream_id = f"{self.room_id}:{started}" if started else f"{self.room_id}:local:{uuid.uuid4().hex[:8]}"
        if self._open_stream_id != stream_id:
            self._open_stream_id = stream_id
            self._reset_crowd_signal()
            self._dirty = True
        return stream_id

    def end_stream(self) -> None:
        self._reset_crowd_signal()
        if self._open_stream_id is not None:
            self._open_stream_id = None
            self._dirty = True

    def observe(
        self,
        sender_key: str,
        *,
        at: Optional[int] = None,
        interaction: bool = False,
        superchat_yuan: Optional[float] = None,
        guard: bool = False,
        guard_level: int = 0,
    ) -> None:
        uid = uid_of(sender_key)
        if uid is None:
            return
        at = int(at if at is not None else self._now())
        self._maintain(at)
        viewer = self._viewer(uid, at)
        viewer.last_seen_at = max(viewer.last_seen_at, at)

        if interaction:
            self._note_interaction(viewer, at)
        if superchat_yuan is not None:
            self._note_superchat(viewer, float(superchat_yuan), at)
        if guard or guard_level > 0:
            viewer.guard_expires_at = max(viewer.guard_expires_at, at + int(self.tuning.guard_retention_ms))
        self._dirty = True

    def observe_online_rank(self, count: float, at: Optional[int] = None) -> None:
        at = int(at if at is not None else self._now())
        if self._crowd_observed_at is not None and at - self._crowd_observed_at > self.tuning.crowd_stale_hold_ms:
            self._crowded = False
            self._below_release_since = None
        self._crowd_count = float(count)
        self._crowd_observed_at = at
        if count >= self.tuning.crowded_on:
            self._crowded = True
            self._below_release_since = None
            return
        if not self._crowded:
            return
        if count > self.tuning.crowded_off:
            self._below_release_since = None
            return
        if self._below_release_since is None:
            self._below_release_since = at
            return
        if at - self._below_release_since >= self.tuning.crowd_release_ms:
            self._crowded = False
            self._below_release_since = None

    def importance_of(self, sender_key: str, at: Optional[int] = None) -> List[str]:
        uid = uid_of(sender_key)
        if uid is None:
            return []
        viewer = self._viewers.get(uid)
        if viewer is None:
            return []
        at = int(at if at is not None else self._now())
        reasons: List[str] = []
        if viewer.guard_expires_at > at:
            reasons.append("guard")
        if viewer.superchat_expires_at > at:
            reasons.append("superchat")
        if viewer.interaction_expires_at > at:
            reasons.append("interaction")
        return reasons

    # ------------------------------------------------------------------ 准入

    def admit(self, text: str, sender_key: str, *, critical: bool = False, at: Optional[int] = None) -> Dict[str, Any]:
        """单条投递准入；返回 ``{"accepted", "lane", "dropped_reason", "limiting_active", "lines", "tokens"}``。"""

        at = int(at if at is not None else self._now())
        crowd_active = self._crowd_active(at)
        reasons = self.importance_of(sender_key, at)
        lane = "critical" if critical else ("important" if reasons else "ordinary")
        lines = line_count(text)
        tokens = estimate_tokens(text)

        self._roll_window(at)
        # 拥挤即限流：车道限额在拥挤期间直接生效（普通弹幕仅受总预算约束）。
        # 与原实现的差异见模块 docstring 差异 #4/#5。
        limiting_active = crowd_active

        if not limiting_active:
            self._take(lines, tokens)
            self.totals["admitted"] += 1
            return {"accepted": True, "lane": lane, "dropped_reason": "", "limiting_active": False, "lines": lines, "tokens": tokens}

        if self._window_just_rolled:
            self.totals["limited_windows"] = int(self.totals.get("limited_windows", 0)) + 1

        total_fits = (
            self._used_lines + lines <= self.tuning.line_budget
            and self._used_tokens + tokens <= self.tuning.token_budget
        )
        accepted = False
        dropped_reason = ""
        if lane == "critical":
            # SC 观众花了钱：始终放行（即使越过总预算，metrics 会标 critical_overflow）
            accepted = True
        elif lane == "important":
            important_line_limit = int(self.tuning.line_budget * self.tuning.important_budget_share)
            important_token_limit = int(self.tuning.token_budget * self.tuning.important_budget_share)
            lane_fits = (
                self._important_lines + lines <= important_line_limit
                and self._important_tokens + tokens <= important_token_limit
            )
            uid = uid_of(sender_key)
            viewer = self._viewers.get(uid) if uid is not None else None
            # 债务 ≥3 的重要观众允许越过车道限额回补一条（仍受总预算约束）
            bypass = (not lane_fits) and viewer is not None and viewer.fair_debt >= 3
            if total_fits and (lane_fits or bypass):
                accepted = True
                self._important_lines += lines
                self._important_tokens += tokens
                if viewer is not None:
                    relief = 3 if bypass else 1
                    viewer.fair_debt = max(-FAIR_DEBT_LIMIT, viewer.fair_debt - relief)
            else:
                dropped_reason = "important_lane_full" if not lane_fits else "budget_full"
        else:
            if total_fits:
                accepted = True
            else:
                dropped_reason = "budget_full"

        if accepted:
            self._take(lines, tokens)
            self.totals["admitted"] += 1
        else:
            self.totals["dropped"] += 1
            self.totals["dropped_tokens"] = int(self.totals["dropped_tokens"]) + tokens
            self._last_drop_reason = dropped_reason
            uid = uid_of(sender_key)
            if lane == "important":
                uid = uid_of(sender_key)
                if uid is not None and (viewer := self._viewers.get(uid)) is not None:
                    # 被预算挤掉的重要观众记一笔债；债务 ≥3 时可越过车道限额回补
                    viewer.fair_debt = min(FAIR_DEBT_LIMIT, viewer.fair_debt + 1)
                    self._dirty = True

        return {"accepted": accepted, "lane": lane, "dropped_reason": dropped_reason, "limiting_active": True, "lines": lines, "tokens": tokens}

    def _take(self, lines: int, tokens: int) -> None:
        self._used_lines += lines
        self._used_tokens += tokens

    def _roll_window(self, at: int) -> None:
        window_ms = max(self.tuning.window_seconds, 1.0) * 1000
        self._window_just_rolled = False
        if at - self._window_started_at >= window_ms:
            self._window_started_at = at
            self._used_lines = 0
            self._used_tokens = 0
            self._important_lines = 0
            self._important_tokens = 0
            self._window_just_rolled = True
            self.totals["windows"] = int(self.totals.get("windows", 0)) + 1

    # ------------------------------------------------------------------ 拥挤信号

    def _crowd_active(self, at: int) -> bool:
        if self._crowd_observed_at is None:
            return False
        age = max(0, at - self._crowd_observed_at)
        return self._crowded and age <= self.tuning.crowd_stale_hold_ms

    def _reset_crowd_signal(self) -> None:
        self._crowd_count = None
        self._crowd_observed_at = None
        self._crowded = False
        self._below_release_since = None

    # ------------------------------------------------------------------ 台账内部

    def _viewer(self, uid: str, at: int) -> _ViewerLedger:
        viewer = self._viewers.get(uid)
        if viewer is None:
            viewer = _ViewerLedger(last_seen_at=at)
            self._viewers[uid] = viewer
        return viewer

    def _note_superchat(self, viewer: _ViewerLedger, yuan: float, at: int) -> None:
        cents = int(round(yuan * 100))
        cutoff = at - int(self.tuning.superchat_window_ms)
        viewer.superchats = [entry for entry in viewer.superchats if entry.at >= cutoff]
        if cents > 0:
            viewer.superchats.append(_SuperchatEntry(at=at, cents=cents))
        rolling_cents = sum(entry.cents for entry in viewer.superchats)
        if cents >= int(round(self.tuning.superchat_single_yuan * 100)) or rolling_cents >= int(
            round(self.tuning.superchat_rolling_yuan * 100)
        ):
            viewer.superchat_expires_at = max(viewer.superchat_expires_at, at + int(self.tuning.superchat_lease_ms))

    def _note_interaction(self, viewer: _ViewerLedger, at: int) -> None:
        stream_id = self._open_stream_id or self.start_stream()
        stream = viewer.interaction_streams.get(stream_id)
        if stream is None:
            stream = _InteractionStream(last_at=at)
            viewer.interaction_streams[stream_id] = stream
        stream.last_at = max(stream.last_at, at)
        if stream.qualified_at is not None:
            return
        minute = at // MINUTE_MS
        if minute not in stream.minutes:
            stream.minutes.append(minute)
        if len(stream.minutes) < self.tuning.interaction_minutes_per_stream:
            return
        stream.qualified_at = at
        stream.minutes = []
        cutoff = at - int(self.tuning.interaction_window_ms)
        qualified = sum(
            1
            for entry in viewer.interaction_streams.values()
            if entry.qualified_at is not None and entry.qualified_at >= cutoff
        )
        if qualified >= self.tuning.interaction_stream_count:
            viewer.interaction_expires_at = max(viewer.interaction_expires_at, at + int(self.tuning.interaction_lease_ms))

    def _maintain(self, at: int) -> None:
        if at - self._last_maintenance_at < 60 * MINUTE_MS:
            return
        self._last_maintenance_at = at
        interaction_cutoff = at - int(self.tuning.interaction_window_ms)
        sc_cutoff = at - int(self.tuning.superchat_window_ms)
        inactive_cutoff = at - max(
            int(self.tuning.guard_retention_ms),
            int(self.tuning.interaction_window_ms),
            int(self.tuning.superchat_window_ms),
        )
        stale_uids: List[str] = []
        for uid, viewer in self._viewers.items():
            viewer.superchats = [entry for entry in viewer.superchats if entry.at >= sc_cutoff]
            for stream_id in list(viewer.interaction_streams.keys()):
                stream = viewer.interaction_streams[stream_id]
                relevant = (stream.qualified_at if stream.qualified_at is not None else stream.last_at) >= interaction_cutoff
                if not relevant:
                    del viewer.interaction_streams[stream_id]
            if (
                viewer.guard_expires_at < at
                and viewer.superchat_expires_at < at
                and viewer.interaction_expires_at < at
                and viewer.last_seen_at < inactive_cutoff
                and not viewer.superchats
                and not viewer.interaction_streams
            ):
                stale_uids.append(uid)
        for uid in stale_uids:
            del self._viewers[uid]
        if stale_uids:
            self._dirty = True

    # ------------------------------------------------------------------ 持久化与快照

    def maybe_persist(self) -> bool:
        """到持久化间隔且确有变更时写盘；返回是否真正写入。"""
        at = self._now()
        if not self._dirty or at - self._last_persist_at < self.tuning.persist_seconds * 1000:
            return False
        self._last_persist_at = at
        return self.flush()

    def flush(self) -> bool:
        if self.file is None or not self._dirty:
            return False
        payload = {
            "schemaVersion": LEDGER_VERSION,
            "salt": self._salt,
            "openStreamId": self._open_stream_id,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "viewers": {
                uid: {
                    "lastSeenAt": viewer.last_seen_at,
                    "guardExpiresAt": viewer.guard_expires_at,
                    "superchatExpiresAt": viewer.superchat_expires_at,
                    "superchats": [{"at": entry.at, "cents": entry.cents} for entry in viewer.superchats],
                    "interactionExpiresAt": viewer.interaction_expires_at,
                    "interactionStreams": {
                        stream_id: {
                            "lastAt": stream.last_at,
                            "minutes": stream.minutes,
                            "qualifiedAt": stream.qualified_at,
                        }
                        for stream_id, stream in viewer.interaction_streams.items()
                    },
                    "fairDebt": viewer.fair_debt,
                }
                for uid, viewer in self._viewers.items()
            },
        }
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.file.with_suffix(self.file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp_path, self.file)
            self._dirty = False
            self._persistence_error = ""
            return True
        except OSError as exc:
            self._persistence_error = str(exc)
            return False

    def _load(self) -> None:
        if self.file is None or not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._persistence_error = "台账文件损坏，已按全新台账启动"
            return
        if raw.get("schemaVersion") != LEDGER_VERSION or not raw.get("salt"):
            self._persistence_error = "台账版本不识别，已按全新台账启动"
            return
        self._salt = str(raw["salt"])
        self._open_stream_id = raw.get("openStreamId")
        for uid, value in (raw.get("viewers") or {}).items():
            if uid_of(uid) is None or not isinstance(value, dict):
                continue
            viewer = _ViewerLedger(last_seen_at=int(value.get("lastSeenAt") or 0))
            viewer.guard_expires_at = int(value.get("guardExpiresAt") or 0)
            viewer.superchat_expires_at = int(value.get("superchatExpiresAt") or 0)
            viewer.superchats = [
                _SuperchatEntry(at=int(entry["at"]), cents=int(entry["cents"]))
                for entry in value.get("superchats") or []
                if isinstance(entry, dict)
            ]
            viewer.interaction_expires_at = int(value.get("interactionExpiresAt") or 0)
            for stream_id, stream in (value.get("interactionStreams") or {}).items():
                if not isinstance(stream, dict):
                    continue
                viewer.interaction_streams[stream_id] = _InteractionStream(
                    last_at=int(stream.get("lastAt") or 0),
                    minutes=[int(minute) for minute in stream.get("minutes") or []],
                    qualified_at=int(stream["qualifiedAt"]) if stream.get("qualifiedAt") is not None else None,
                )
            viewer.fair_debt = int(value.get("fairDebt") or 0)
            self._viewers[uid] = viewer

    def summary_text(self, at: Optional[int] = None) -> str:
        """一行中文摘要（供世界状态快照展示限流情况）。"""
        at = int(at if at is not None else self._now())
        crowd = "未知" if self._crowd_observed_at is None else str(int(self._crowd_count or 0))
        if not self._crowd_active(at):
            return f"观众准入：限流未生效（人气 {crowd}）；本进程累计准入 {self.totals['admitted']} 条、丢弃 {self.totals['dropped']} 条。"
        return (
            f"观众准入：限流生效中（人气 {crowd}，窗口预算 {self.tuning.line_budget} 行 / "
            f"{self.tuning.token_budget} token，已用 {self._used_lines}/{self._used_tokens}）；"
            f"累计准入 {self.totals['admitted']} 条、丢弃 {self.totals['dropped']} 条。"
        )
