"""SSE 弹幕广播：跨线程线程安全的消息总线。

单职责：弹幕事件循环线程（生产者）→ SSE HTTP 线程（消费者）之间的
单房间消息总线。不关心消息格式、不关心消费方是谁。

并发模型：
- push()：生产者（弹幕线程）调用，加锁后追加到 history 并 notify_all
- consume()：消费者（SSE 线程）调用，拿到 (history, cond) 后自己循环 wait/取
- history 截断：超过 _MAX_HISTORY 删最旧，按 (seq, msg) 存储；
  SSE 线程按 seq 取增量，避免 history 截断导致"从头补发"时新弹幕收不到
"""

from __future__ import annotations

import threading
from typing import List, Tuple


_MAX_HISTORY = 200  # SSE 历史缓存条数（原 20 太小，繁忙直播间几秒就把初始 status 挤掉，新订阅者拿不到连接状态）


class Broadcaster:
    """线程安全弹幕广播（SSE 服务器线程 ⇄ 弹幕事件循环线程）。

    history 保存最近 _MAX_HISTORY 条；每条带单调递增 seq，
    SSE 连接靠 seq 判断增量（不能按列表下标，否则 history
    截断后 `history[index:]` 永远为空，新弹幕就再也收不到了）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._history: List[Tuple[int, dict]] = []  # (seq, msg)
        self._seq = 0

    def push(self, msg: dict) -> None:
        with self._cond:
            self._seq += 1
            self._history.append((self._seq, msg))
            if len(self._history) > _MAX_HISTORY:
                del self._history[:-_MAX_HISTORY]
            self._cond.notify_all()

    def consume(self) -> Tuple[List[Tuple[int, dict]], threading.Condition]:
        """返回 (历史列表, Condition)，供 SSE 线程循环读取。"""
        return self._history, self._cond
