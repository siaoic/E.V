"""LLM→TTS 端到端耗时测量脚本（临时测量用，不入测试集）。

走与 src/llm/stream.py::converse 一致的核心链路：
    brain.chat_stream(逐句 yield) → tts.speak(入队) → 后台 _pump 流式合成
    → player.begin_stream/emit（首块到达即播放）

测量点（time.perf_counter）：
    t0            对话开始（调用 chat_stream 前）
    T_llm_first   LLM yield 出第一句可播文本
    T_speak       tts.speak 把第一句送入合成队列
    T_audio_first player 收到第一块合成音频（合成播放开始）
    T_audio_play  首块真正写入声卡（出声）

不修改任何业务代码：仅用运行时包装/公开回调记录时间戳。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.llm_brain import LLMBrain
from src.tts.engine import TTSEngine
from src.utils import config


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "介绍一下你自己，用两三句话回答"
    markers: dict = {}

    brain = LLMBrain(mcp=None)
    tts = TTSEngine()
    if not await tts.start():
        print("[测量] TTS 服务不可用，无法完成 LLM→TTS 链路测量")
        return

    # 对齐生产启动语义：warmup 一次性预热流式合成链路（不计入测量）
    await tts.warmup()

    # 包装播放入口：首块合成音频到达即视为「合成播放开始」
    orig_begin = tts._player.begin_stream
    orig_emit = tts._player.emit

    def wrap_begin(sr, text, gen):
        markers.setdefault("audio_first", time.perf_counter())
        return orig_begin(sr, text, gen)

    def wrap_emit(audio, sr, text, subtitles, gen):
        markers.setdefault("audio_first", time.perf_counter())
        return orig_emit(audio, sr, text, subtitles, gen)

    tts._player.begin_stream = wrap_begin
    tts._player.emit = wrap_emit

    # 公开回调：首块真正写入声卡前触发（出声时刻）
    def on_play(wav, text, dur_s):
        markers.setdefault("audio_play", time.perf_counter())

    tts.set_on_play_callback(on_play)

    t0 = time.perf_counter()
    tts.preheat()
    sentences = 0
    first_text = ""
    async for sentence in brain.chat_stream(prompt):
        text = (sentence or "").strip()
        if not text:
            continue
        markers.setdefault("llm_first", time.perf_counter())
        markers.setdefault("speak", time.perf_counter())
        if not first_text:
            first_text = text
        await tts.speak(text)
        sentences += 1
        await asyncio.sleep(0)

    # 等待合成播放启动（首块到达），最多 60s
    for _ in range(600):
        if "audio_first" in markers:
            break
        await asyncio.sleep(0.1)

    await tts.drain()
    tts.set_on_play_callback(None)
    await tts.stop()

    print("=" * 56)
    print(f"提示词: {prompt}")
    print(f"LLM 模型: {config.cfg.LLM_MODEL} | TTS: {tts._server_url}")
    print(f"产出句子数: {sentences} | 首句: {first_text[:30]}")
    print("-" * 56)
    t_llm = markers.get("llm_first")
    t_speak = markers.get("speak")
    t_first = markers.get("audio_first")
    t_play = markers.get("audio_play")
    if t_llm is None:
        print("LLM 未产出任何句子")
        return
    print(f"首句 yield（相对调用）             : {(t_llm - t0) * 1000:8.0f} ms")
    if t_first is not None:
        print(f"LLM 首句 → TTS 合成播放首块        : {(t_first - t_llm) * 1000:8.0f} ms  ★核心指标")
        print(f"  其中 入队→首块（合成+传输）      : {(t_first - t_speak) * 1000:8.0f} ms")
        print(f"  首块→真正出声（声卡缓冲）       : {(t_play - t_first) * 1000:8.0f} ms")
    else:
        print("TTS 未收到音频（可能合成失败/退化丢弃）")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
