"""弹幕挑选器：评分 + 候选池 + 窗口 + 批量回复。

单职责：决定「哪些弹幕值得回」「什么时候回」「几条一起回」。
不关心弹幕来源（blivedm）、不关心广播（SSE）、不关心回调方是谁，
所有外部依赖通过构造函数注入（on_reply_callback / on_batch_reply_callback）。

工作原理（高分弹幕优先 + 高密度合并）：
  1. submit()：对每条弹幕做兴趣度评分（0~100），低于 MIN_SCORE 直接丢；
  2. 高于门槛的进候选池：双优先级堆 + dict 索引，支持同 key 顶替、惰性删除；
  3. 8s 窗口让一波弹幕到齐再比，最长 max_gap_s 保底硬挑一次防冷场；
  4. 挑分最高的回复；高密度时把分数相近（≤_BATCH_SCORE_DELTA）且同窗口内
     的候选一并取出，聚合一次批量回复（最多 _BATCH_MAX_ITEMS 条）。
  5. 回复后冷却（_min_gap_s），冷却内新弹幕继续评分入池但不触发回复。
"""

from __future__ import annotations

import heapq
import random
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

from src.utils import config
from src.utils import console


# ===== 评分常量 =====

# 高兴趣关键词：直接拉高分（点名 / 提问 / 感叹 / 情绪浓）
_HOT_TOKENS = [
    # 点名主播："主播"、"未可飞"、"肥牛"、"E.V" 之类
    "主播", "up", "UP", "喂", "在吗", "在不在", "听得到", "看得见",
    # 疑问词：大概率需要回应
    "吗", "呢", "？", "?", "怎么", "为什么", "什么", "哪", "谁", "几",
    # 感叹与情绪浓
    "！", "!", "哇", "草", "绝", "神", "笑死", "好可爱", "好帅",
    "喜欢", "爱", "想你", "亲",
]

# 低兴趣特征：直接丢分或跳过
_SKIP_PREFIXES = ("[表情]",)
_BORING_TOKENS = ("666", "233", "hhh", "哈哈哈", "哈哈", "来了", "打卡",
                  "签到", "1", "2", "3", "a", "s", "d", "f", "q", "w",
                  "e", "r", "t", "y", "。。", "..")
_BORING_RE = re.compile(r"^(哈|啊|哦|嗯|6|2|h|w)+$")
# 实质内容检测：含中英文/数字/假名才算有内容；纯标点符号（"？？？"、
# "！！！"、"。。。"）无内容，不值得回
_HAS_CONTENT_RE = re.compile(
    r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 回复间隔（与 ProactiveEngine 解耦，独立控制弹幕节奏）
# 冷却初始值（秒）；每次回复后从共用随机间隔范围
# （config.RESPONSE_INTERVAL_MIN~MAX，与主动对话同一范围）滚动取值，
# 避免固定 20s 节奏显得机械。
_DEFAULT_MIN_GAP_S = 20      # 两次弹幕回复最少间隔（初始/兜底）
_DEFAULT_MAX_GAP_S = 30      # 最长 30 秒不回就挑当前最好的（保底防冷场）
_DEFAULT_WINDOW_S = 8        # 窗口 8 秒：弹幕到齐后比较，挑分最高的回
_DEFAULT_MIN_SCORE = 20      # 低于此分的弹幕直接丢弃（太水不值得回）
_POOL_CAP = 50               # 候选池上限：溢出时淘汰分数最低的候选
_BATCH_SCORE_DELTA = 15      # 批量回复：与最高分差 ≤ 此值的候选可合并一次回复
_BATCH_MAX_ITEMS = 3         # 批量回复最多聚合条数（含最高分那条）


class DanmakuPicker:
    """弹幕挑选器：不是每条都回、也不是固定时间回，选"当前最有趣的"。

    依赖注入：
      - on_reply_callback(uid, username, text)：单条回复的回调
      - on_batch_reply_callback([(uid, username, text), ...])：批量回复的回调
        （None 表示不支持批量，挑选器只走单条路径）
    """

    def __init__(
        self,
        on_reply_callback: Callable[[int, str, str], None],
        on_batch_reply_callback: Optional[
            Callable[[List[Tuple[int, str, str]]], None]] = None,
        min_gap_s: int = _DEFAULT_MIN_GAP_S,
        max_gap_s: int = _DEFAULT_MAX_GAP_S,
        window_s: int = _DEFAULT_WINDOW_S,
        min_score: int = _DEFAULT_MIN_SCORE,
    ) -> None:
        if on_reply_callback is None:
            raise ValueError("on_reply_callback 不能为 None")
        self._cb: Callable[[int, str, str], None] = on_reply_callback
        self._batch_cb: Optional[
            Callable[[List[Tuple[int, str, str]]], None]] = on_batch_reply_callback
        self._min_gap_s = min_gap_s
        self._max_gap_s = max_gap_s
        self._window_s = window_s
        self._min_score = min_score

        # 候选池：双优先级堆 + dict 索引（支持顶替与惰性删除）
        # _pool: key=(uid, text) → 当前条目 (score, uid, username, text, received_at, seq)
        # _max_heap: [(-score, -seq, key)]  取回复时 heappop 拿最高分（同分新的优先）
        # _min_heap: [(score, seq, key)]    池满时 heappop 踢最低分（同分旧的优先）
        # 顶替语义：同 key 更高分提交 → 新条目入双堆，旧堆元素作废（弹出时按 seq 校验跳过）
        self._pool: dict = {}
        self._max_heap: list = []
        self._min_heap: list = []
        self._seq = 0
        self._lock = threading.Lock()

        self._last_reply_at = 0.0            # 上次真正回复的时间戳
        self._window_started_at: Optional[float] = None  # 当前窗口开始时间
        self._window_timer: Optional[threading.Timer] = None
        self._max_gap_timer: Optional[threading.Timer] = None

    # ---- 外部接口 ----

    def submit(self, uid: int, username: str, text: str) -> None:
        """从弹幕处理器提交一条候选。内部会评分、入池、启窗口。"""
        if text is None:
            return
        # 表情包/极短水弹幕：直接跳过
        stripped = text.strip()
        if not stripped or stripped.startswith(_SKIP_PREFIXES):
            return
        score = self._score(stripped)
        if score < self._min_score:
            return
        received_at = time.time()
        key = (uid, stripped)
        with self._lock:
            # 同用户同内容不重复加；同用户不同内容只保留分数更高的那条
            old = self._pool.get(key)
            if old is not None and old[0] >= score:
                return
            self._seq += 1
            entry = (score, uid, username, stripped, received_at, self._seq)
            self._pool[key] = entry
            heapq.heappush(self._max_heap, (-score, -self._seq, key))
            heapq.heappush(self._min_heap, (score, self._seq, key))
            # 被顶替的作废堆元素不主动删（惰性）；作废元素过多时按有效候选重建
            if len(self._max_heap) > len(self._pool) + _POOL_CAP:
                self._rebuild_heaps_locked()
            # 溢出（超过上限）：淘汰分数最低的候选，避免洪峰把高分弹幕挤出
            self._evict_overflow_locked()
            # 启 8s 窗口：等一等后面有没有更有趣的
            if self._window_started_at is None:
                self._window_started_at = received_at
                self._schedule_window_end(received_at + self._window_s)
            # 启保底定时器（最长 max_gap 秒不回就硬挑一次）；只在没有挂起时才启
            if self._max_gap_timer is None:
                self._schedule_max_gap_end(received_at + self._max_gap_s)

    def stop(self) -> None:
        with self._lock:
            for t in (self._window_timer, self._max_gap_timer):
                if t is not None:
                    try:
                        t.cancel()
                    except Exception:
                        pass
            self._window_timer = None
            self._max_gap_timer = None

    # ---- 评分：兴趣度打分 ----

    @staticmethod
    def _score(text: str) -> int:
        """弹幕兴趣度评分（0~100）。分低不值得回。"""
        # 单字符 / 纯重复灌水 → 0 分
        if len(text) <= 1 or _BORING_RE.match(text):
            return 0
        if any(tok == text for tok in _BORING_TOKENS):
            return 0
        # 纯标点/符号（"？？？"、"！！！"）：无实质内容，直接 0 分。
        # 否则 "？" 在 _HOT_TOKENS 里会按"提问"狂加分（基础 10 + 12 + 3），
        # 一条空标点反而越过门槛被精选。
        if not _HAS_CONTENT_RE.search(text):
            return 0

        score = 10  # 基础分：过了上面两道门就算 10 分起

        # 长度加分：有内容（>8 字）的弹幕通常是真的在说话
        if len(text) >= 15:
            score += 15
        elif len(text) >= 8:
            score += 8

        # 高兴趣关键词命中（每条 +12，最高 +36）
        hot_hits = 0
        for tok in _HOT_TOKENS:
            if tok in text:
                hot_hits += 1
                if hot_hits >= 3:
                    break
        score += hot_hits * 12

        # 结尾标点暗示
        if text.endswith(("？", "?")):
            score += 3  # 提问强烈加分
        elif text.endswith(("！", "!")):
            score += 2
        elif text.endswith(("~", "～")):
            score += 1

        # 纯灌水词命中倒扣分
        boring_hits = sum(1 for tok in _BORING_TOKENS if tok in text)
        score -= boring_hits * 8

        return max(0, min(100, score))

    # ---- 优先级堆内部操作 ----

    def _rebuild_heaps_locked(self) -> None:
        """作废堆元素过多时按当前有效候选重建双堆（防惰性删除堆积膨胀）。"""
        self._max_heap = [(-e[0], -e[5], key) for key, e in self._pool.items()]
        self._min_heap = [(e[0], e[5], key) for key, e in self._pool.items()]
        heapq.heapify(self._max_heap)
        heapq.heapify(self._min_heap)

    def _evict_overflow_locked(self) -> None:
        """候选池超上限：从最小堆踢掉分数最低的候选（跳过被顶替的作废堆元素）。"""
        while len(self._pool) > _POOL_CAP and self._min_heap:
            score, seq, key = heapq.heappop(self._min_heap)
            entry = self._pool.get(key)
            # 堆顶可能是被更高分顶替的作废元素（分数/序号对不上），跳过
            if entry is None or entry[0] != score or entry[5] != seq:
                continue
            del self._pool[key]

    def _pop_best_locked(self) -> Optional[tuple]:
        """从候选池按分数降序取最高分 1 条，取完移走；顺手清理过期候选。"""
        while self._max_heap:
            neg_score, neg_seq, key = heapq.heappop(self._max_heap)
            entry = self._pool.get(key)
            if entry is None or entry[0] != -neg_score or entry[5] != -neg_seq:
                continue  # 已被顶替的作废堆元素
            del self._pool[key]
            self._cleanup_stale_locked()
            return entry
        self._pool.clear()  # 堆空仍有残留（理论上不会）：清空兜底
        return None

    def _cleanup_stale_locked(self) -> None:
        """取走后清理超过 max_gap*2 还没被看上的过期候选（堆里对应元素弹出时作废）。"""
        now = time.time()
        for key in list(self._pool):
            if now - self._pool[key][4] > self._max_gap_s * 2:
                del self._pool[key]

    def _pop_reply_batch_locked(self) -> Optional[List[tuple]]:
        """取最高分弹幕；高密度时把分数相近（≤ _BATCH_SCORE_DELTA）且
        同窗口内的候选一并取出，组成一次批量回复（最多 _BATCH_MAX_ITEMS 条）。
        """
        best = self._pop_best_locked()
        if best is None:
            return None
        if self._batch_cb is None or not self._pool:
            return [best]
        batch = [best]
        best_score = best[0]
        now = time.time()
        # 补充候选：分数与最高分相近、且仍在当前窗口内（同一波弹幕）
        peers = [
            v for v in self._pool.values()
            if v[0] >= best_score - _BATCH_SCORE_DELTA
            and now - v[4] <= self._window_s
        ]
        peers.sort(key=lambda v: v[0], reverse=True)
        for peer in peers[:_BATCH_MAX_ITEMS - 1]:
            self._pool.pop((peer[1], peer[3]))
            batch.append(peer)
        return batch

    # ---- 调度：窗口结束 / 保底超时触发选优回复 ----

    def _schedule_window_end(self, fire_at: float) -> None:
        delay = max(0.0, fire_at - time.time())
        t = threading.Timer(delay, self._on_window_end)
        t.daemon = True
        t.start()
        self._window_timer = t

    def _schedule_max_gap_end(self, fire_at: float) -> None:
        delay = max(0.0, fire_at - time.time())
        t = threading.Timer(delay, self._on_max_gap_end)
        t.daemon = True
        t.start()
        self._max_gap_timer = t

    def _fire_reply(self, batch: List[tuple], label: str) -> None:
        """把选中的弹幕批次交给回调：单条走 on_reply_callback，多条走 on_batch_reply_callback。

        label：触发来源（窗口结束「精选回复」/ 最长间隔「保底回复」）。
        """
        try:
            if len(batch) == 1:
                _, uid, username, text, _, _ = batch[0]
                console.dim(f"[弹幕] {label} {username}")
                self._cb(uid, username, text)
            else:
                names = "、".join(item[2] for item in batch)
                console.dim(f"[弹幕] 批量回复 {names}")
                self._batch_cb(
                    [(item[1], item[2], item[3]) for item in batch])
        except Exception as e:
            console.error(f"[弹幕] 回调异常：{type(e).__name__}: {e}")

    def _on_window_end(self) -> None:
        """窗口结束：如果冷却已过，就从候选池挑最好的回复。"""
        with self._lock:
            self._window_timer = None
            self._window_started_at = None
            if self._in_cooldown_locked():
                return
            batch = self._pop_reply_batch_locked()
            if batch is None:
                return
            # 立刻把两个定时器都关掉（这次要回复了）
            if self._max_gap_timer is not None:
                try:
                    self._max_gap_timer.cancel()
                except Exception:
                    pass
                self._max_gap_timer = None
            self._last_reply_at = time.time()
            self._roll_cooldown()
        self._fire_reply(batch, "精选回复")

    def _on_max_gap_end(self) -> None:
        """保底：到了最长间隔还没窗口触发，硬挑当前最好的。"""
        with self._lock:
            self._max_gap_timer = None
            if self._in_cooldown_locked():
                # 冷却中：下次再等一个 max_gap
                self._schedule_max_gap_end(time.time() + self._max_gap_s)
                return
            batch = self._pop_reply_batch_locked()
            if batch is None:
                return
            if self._window_timer is not None:
                try:
                    self._window_timer.cancel()
                except Exception:
                    pass
                self._window_timer = None
                self._window_started_at = None
            self._last_reply_at = time.time()
            self._roll_cooldown()
        self._fire_reply(batch, "保底回复")

    def _roll_cooldown(self) -> None:
        """回复后滚动冷却时长：与主动对话共用随机间隔范围，避免固定节奏。"""
        self._min_gap_s = random.uniform(
            config.cfg.RESPONSE_INTERVAL_MIN, config.cfg.RESPONSE_INTERVAL_MAX)

    def _in_cooldown_locked(self) -> bool:
        return (time.time() - self._last_reply_at) < self._min_gap_s


# ===== 进程级注册（与主程序解耦） =====

_DANMAKU_PICKER: "Optional[DanmakuPicker]" = None


def set_danmaku_picker(picker: "Optional[DanmakuPicker]") -> None:
    """主程序调用：把挑选器注入进来；传 None 则停用弹幕自动回复。"""
    global _DANMAKU_PICKER
    _DANMAKU_PICKER = picker


def get_danmaku_picker() -> "Optional[DanmakuPicker]":
    return _DANMAKU_PICKER
