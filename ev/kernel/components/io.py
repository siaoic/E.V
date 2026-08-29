"""STT 语音识别 + 弹幕 start_bili（L444-457 + L459-460）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ev.kernel.runtime import RuntimeContext


async def setup(runtime: "RuntimeContext") -> None:
    """STT 初始化 + 弹幕 start_bili。"""
    from ev.utils import console

    cfg = runtime.cfg

    # 语音识别
    if cfg.STT_ENABLED:
        try:
            from ev.asr.stt import STTEngine
            runtime.stt_engine = STTEngine(cfg)
            runtime.stt_engine.start()
            if runtime.stt_engine.check_health():
                console.dim(
                    f"语音识别已启用：对着麦克风说话即可输入"
                    f"（{cfg.STT_MODEL}，静音 {cfg.STT_SILENCE_SECONDS:.0f}s 自动切段）")
            else:
                # P2-8 修复：本地 ASR 服务未启动时不装没听见——醒目告警，
                # 避免说话没反应还每句白等转写超时
                console.error(
                    "⚠️ 本地 ASR 服务未响应，语音识别不会工作！"
                    "请先双击 启动asr.bat（或在 .env 配置 STT_BASE_URL 走云端转写）")
        except Exception as e:
            runtime.stt_engine = None
            console.warn(f"语音识别启动失败（可忽略）：{e}")
    else:
        console.dim("语音识别未启用（.env 设置 STT_ENABLED=true 开启）")

    # B 站弹幕服务启动
    runtime.danmaku_picker, runtime.bili_svc, runtime.danmaku_reply_task = await runtime.start_bili()


async def teardown(runtime: "RuntimeContext") -> None:
    """原 teardown 中 bili + stt 段（在 plugin 后、sub 前执行）。"""
    import asyncio

    cancel = runtime.stop_bili()
    runtime.danmaku_picker, runtime.bili_svc, runtime.danmaku_reply_task = None, None, None
    if cancel is not None:
        try:
            await cancel
        except (asyncio.CancelledError, Exception):
            pass
    if runtime.stt_engine is not None:
        runtime.stt_engine.stop()
