"""TTS 合成测试：逐句输入文本 → GPT-SoVITS 官方 api_v2.py 服务端流式合成 → 扬声器播放，
并统计每句「回车发送 → 首块音频开始播放」的端到端延迟（首字延迟）。

用法：
1. 先启动 TTS 服务端：双击 TTS启动.bat（GPT-SoVITS-main 官方 api_v2.py，监听 http://127.0.0.1:9880）
2. 再运行本脚本（需在 runtime 环境）：python tests\test_tts_input.py
3. 输入任意文本回车即合成播放，回车时打印本句延迟；quit / exit / 空行退出
"""

import asyncio
import os
import sys
import time

# 与 main.py 相同的 six 兼容补丁：必须在导入 PySide6 / gsv_tts / transformers
# 链之前执行，否则 shibokensupport 钩子触发 AttributeError 导致启动即崩。
import six
if not hasattr(six._SixMetaPathImporter, "_path"):
    six._SixMetaPathImporter._path = []

# 项目根目录注入 sys.path（本脚本位于 tests/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tts.engine import TTSEngine  # noqa: E402


async def main() -> None:
    # 强制 UTF-8 输出，保证中文日志正常显示
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    engine = TTSEngine()
    if not await engine.start():
        print("TTS 服务端不可用：请先运行 TTS启动.bat（官方 api_v2.py，端口 9880）再重试。")
        return

    # 播放线程回调：记录本句首块音频真正开始播放的墙钟时刻（仅记首次）
    first_play_time = None

    def on_first_play(wav: str, text: str, dur_s: float) -> None:
        nonlocal first_play_time
        if first_play_time is None:
            first_play_time = time.perf_counter()

    engine.set_on_play_callback(on_first_play)

    try:
        print("输入文本开始合成（quit / exit / 空行退出）：")
        print("延迟 = 回车发送 → 首块音频开始播放（毫秒）")
        while True:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text or text.lower() in ("quit", "exit", "q"):
                break
            first_play_time = None
            send_time = time.perf_counter()  # 从发送时刻开始计时
            await engine.speak(text)
            await engine.drain()  # 等待本句合成 + 播放完毕
            if first_play_time is not None:
                latency_ms = (first_play_time - send_time) * 1000
                print(f"[延迟] 首块播放 {latency_ms:7.1f} ms")
            else:
                print("[延迟] 未捕获到播放回调")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
