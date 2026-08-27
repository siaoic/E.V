"""B 站直播弹幕模块（薄 shim，向后兼容层）。

实现已拆分为单职责子模块：

  - src.danmaku.avatar       头像解析 + URL 白名单 + 字节缓存
  - src.danmaku.broadcaster   线程安全 SSE 消息总线
  - src.danmaku.picker       评分 / 候选池 / 窗口 / 批量回复
  - src.danmaku.client        blivedm 客户端 + handler + 线程循环
  - src.danmaku.service       BiliService + Manager + HTTP server

本文件仅做 re-export，保证历史 import 路径
（`from src.danmaku.bili_danmaku import ...`）继续可用。
"""

from __future__ import annotations

import os
import time

from ev.danmaku.avatar import (
    AvatarImageCache as _AvatarImageCache,
    AvatarResolver as _AvatarResolver,
    is_allowed_avatar_url as _is_allowed_avatar_url,
    make_session as _make_session,
)
from ev.danmaku.broadcaster import Broadcaster as _Broadcaster
from ev.danmaku.client import bili_loop as _bili_loop
from ev.danmaku.picker import (
    DanmakuPicker,
    get_danmaku_picker as _get_danmaku_picker,
    set_danmaku_picker,
)
from ev.danmaku.service import BiliService, BiliServiceManager
from ev.utils import config
from ev.utils import console


# 公共符号（兼容层：原模块以这些名字 import 内部使用）
__all__ = [
    "BiliService",
    "BiliServiceManager",
    "DanmakuPicker",
    "set_danmaku_picker",
]


# 兼容原 _xxx 私有名（曾被 application.py 或 tests 引用）
_Broadcaster = _Broadcaster
_make_session = _make_session
_AvatarImageCache = _AvatarImageCache
_AvatarResolver = _AvatarResolver
_is_allowed_avatar_url = _is_allowed_avatar_url
_bili_loop = _bili_loop
_get_danmaku_picker = _get_danmaku_picker


if __name__ == "__main__":
    cfg = config.cfg
    room_ids = cfg.BILI_ROOM_IDS or ([cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
    if not room_ids:
        console.warn("[弹幕] 未配置房间号（BILI_ROOM_ID/BILI_ROOM_IDS），服务未启动")
    else:
        manager = BiliServiceManager(
            room_ids, cfg.BILI_SERVER_PORT,
            os.path.join(cfg.PROJECT_ROOT, "ui", "弹幕卡片v2.html"))
        # 把 client.py 的线程启动函数注入到每个 BiliService，
        # 避免 service.py ⇄ client.py 循环引用
        manager.attach_client_starter(_bili_loop)
        manager.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            manager.stop()
