# -*- coding: utf-8 -*-
"""弹幕观察器（DanmakuObserver）：全量观察 + 刷屏规律检测。

定位（对标真人主播看弹幕的方式）：
  - **观察**：每条弹幕都进滚动缓冲（只看不回）——AI 回复优选弹幕时，
    把最近弹幕流作为背景上下文注入 prompt，让 AI「知道大家在聊什么/
    在刷什么」，但明确要求不逐条回应；
  - **规律触发**：同一句话被刷屏（归一化后相同文本 N 次 / M 人）时，
    即使挑选器没选出优选，也主动触发一次回复（"大家都在刷 XX 啊"），
    经弹幕回复管线播报（忙碌自动避让 + 双重冷却防连发）。

线程模型：observe() 在弹幕线程调用（bili loop），snapshot/detect 在
主循环或任意线程读——内部 threading.Lock 保护，纯内存操作，开销可忽略。
零定时器：检测随 observe 顺手做，触发经调用方注册的 sink 投递。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class PatternHit:
    """一次刷屏规律命中。"""
    pattern: str            # 归一化后的刷屏文本（如 "666" / "哈哈哈"）
    count: int              # 窗口内出现次数
    users: int              # 不重复观众数
    window_sec: float       # 检测窗口
    samples: list = field(default_factory=list)  # 最近几条 (username, 原文)

    def describe(self) -> str:
        """给 LLM 看的一句话描述（合成弹幕文本用）。"""
        return (f"大家正在刷「{self.pattern}」——{self.window_sec:.0f} 秒内 "
                f"{self.count} 条（{self.users} 位观众）")


def _normalize(text: str) -> str:
    """归一化：去标点/空白/大小写；纯重复串折叠（66666→666、哈哈哈哈→哈哈）。

    折叠规则：归一化后所有字符相同（如 "6666" "hhhhh" "哈哈哈"）时截取
    前 3 个字符——让不同长度的刷屏变体落到同一个 pattern 桶。
    """
    s = re.sub(r"[\W_]+", "", (text or "").lower())
    if not s:
        return ""
    if len(set(s)) == 1 and len(s) > 3:
        s = s[:3]
    return s


class DanmakuObserver:
    """滚动观察缓冲 + 刷屏规律检测（线程安全、零定时器）。"""

    def __init__(
        self,
        buffer_window_sec: float = 90.0,
        buffer_max_entries: int = 60,
        flood_window_sec: float = 15.0,
        flood_min_count: int = 5,
        flood_min_users: int = 2,
        flood_cooldown_sec: float = 180.0,
        flood_global_gap_sec: float = 60.0,
    ) -> None:
        self.buffer_window_sec = float(buffer_window_sec)
        self.buffer_max_entries = int(buffer_max_entries)
        self.flood_window_sec = float(flood_window_sec)
        self.flood_min_count = int(flood_min_count)
        self.flood_min_users = int(flood_min_users)
        self.flood_cooldown_sec = float(flood_cooldown_sec)
        self.flood_global_gap_sec = float(flood_global_gap_sec)

        # (ts, username, text, norm) 滚动缓冲
        self._buf: deque = deque(maxlen=self.buffer_max_entries)
        self._fired_at: dict = {}          # pattern -> 上次触发 ts
        self._last_any_fire: float = 0.0   # 任意 pattern 上次触发（全局间隔）
        self._lock = threading.Lock()
        self._pattern_sink: Optional[Callable[[PatternHit], None]] = None

    # ---------- 接线 ----------

    def bind_pattern_sink(self, sink: Callable[[PatternHit], None]) -> None:
        """注册刷屏触发回调（bili 线程调用 sink，由 sink 自己做线程安全投递）。"""
        self._pattern_sink = sink

    def reset(self) -> None:
        """清空缓冲与冷却（会话重启时调用）。"""
        with self._lock:
            self._buf.clear()
            self._fired_at.clear()
            self._last_any_fire = 0.0

    # ---------- 观察（弹幕线程调用） ----------

    def observe(self, text: str, username: str = "", uid: int = 0) -> None:
        """观察一条弹幕：进缓冲 + 顺手做刷屏检测（纯内存，开销可忽略）。

        命中规律且过冷却 → 调 pattern_sink（若有）。任何异常不外泄。
        """
        try:
            text = (text or "").strip()
            if not text:
                return
            now = time.time()
            norm = _normalize(text)
            hit = None
            with self._lock:
                self._buf.append((now, username, text, norm))
                self._evict_locked(now)
                if norm and self._pattern_sink is not None:
                    hit = self._detect_flood_locked(norm, username, now)
            if hit is not None:
                try:
                    self._pattern_sink(hit)
                except Exception:
                    pass
        except Exception:
            pass

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self.buffer_window_sec
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def _detect_flood_locked(self, norm: str, username: str,
                             now: float) -> Optional[PatternHit]:
        """刷屏检测（持锁调用）：同 pattern 计数 + 双重冷却判定。"""
        window = self.flood_window_sec
        cutoff = now - window
        count = 0
        users = set()
        samples = []
        for ts, uname, raw, n in self._buf:
            if ts < cutoff or n != norm:
                continue
            count += 1
            users.add(uname)
            samples.append((uname, raw))
        if count < self.flood_min_count or len(users) < self.flood_min_users:
            return None
        # 冷却：同 pattern 间隔 + 任意 pattern 全局间隔（防连发刷屏回复）
        if now - self._fired_at.get(norm, 0.0) < self.flood_cooldown_sec:
            return None
        if now - self._last_any_fire < self.flood_global_gap_sec:
            return None
        self._fired_at[norm] = now
        self._last_any_fire = now
        if len(self._fired_at) > 64:
            # 冷却表过大时清最旧的一半（保结构不膨胀）
            items = sorted(self._fired_at.items(), key=lambda kv: kv[1])
            self._fired_at = dict(items[len(items) // 2:])
        return PatternHit(
            pattern=norm, count=count, users=len(users),
            window_sec=window, samples=samples[-3:],
        )

    # ---------- 查询（任意线程，读快照） ----------

    def snapshot(self, window_sec: Optional[float] = None,
                 limit: int = 20,
                 exclude_texts: Optional[set] = None) -> list:
        """最近弹幕流快照：[(username, text), ...]（旧→新）。

        exclude_texts：排除文本集合（如本次正要回复的优选弹幕，避免
        上下文与回复目标重复）。
        """
        exclude = exclude_texts or set()
        window = self.buffer_window_sec if window_sec is None else float(window_sec)
        now = time.time()
        cutoff = now - window
        with self._lock:
            rows = [(uname, raw) for ts, uname, raw, _ in self._buf
                    if ts >= cutoff and raw not in exclude]
        return rows[-max(1, int(limit)):]

    def stats(self) -> dict:
        """观察器状态（调试/控制中心用）。"""
        with self._lock:
            now = time.time()
            window = self.flood_window_sec
            cutoff = now - window
            counter: dict = {}
            for ts, uname, raw, norm in self._buf:
                if ts >= cutoff and norm:
                    counter[norm] = counter.get(norm, 0) + 1
            top = sorted(counter.items(), key=lambda kv: -kv[1])[:3]
            return {
                "buffer_size": len(self._buf),
                "window_sec": self.buffer_window_sec,
                "flood_top": top,
                "patterns_fired": len(self._fired_at),
            }


# ===== 全局单例（弹幕 client 与回复链路共享同一缓冲） =====

_observer: Optional[DanmakuObserver] = None


def _build_from_config() -> DanmakuObserver:
    """按 cfg 构建观察器（缺字段走默认值，读配置失败不致命）。"""
    def cfg_val(name: str, default):
        try:
            from ev.utils import config as _cfg
            return getattr(_cfg.cfg, name, default)
        except Exception:
            return default
    return DanmakuObserver(
        buffer_window_sec=float(cfg_val("DANMAKU_OBSERVE_CONTEXT_SEC", 60.0) * 1.5),
        buffer_max_entries=int(cfg_val("DANMAKU_OBSERVE_BUFFER_MAX", 60)),
        flood_window_sec=float(cfg_val("DANMAKU_FLOOD_WINDOW_SEC", 15.0)),
        flood_min_count=int(cfg_val("DANMAKU_FLOOD_MIN_COUNT", 5)),
        flood_min_users=int(cfg_val("DANMAKU_FLOOD_MIN_USERS", 2)),
        flood_cooldown_sec=float(cfg_val("DANMAKU_FLOOD_COOLDOWN_SEC", 180.0)),
    )


def get_observer() -> DanmakuObserver:
    """获取全局观察器（懒创建，弹幕线程与主循环共享）。"""
    global _observer
    if _observer is None:
        _observer = _build_from_config()
    return _observer


def reset_observer() -> None:
    """重置全局实例（供测试/会话重启）。"""
    global _observer
    _observer = None
