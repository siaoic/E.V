"""进程内直接调用 vs HTTP API 差距对照实验（临时测量用）。

消除混杂因素：
- 进程内实例先充分预热（图缓存完整）再测，对齐 HTTP 服务端常驻预热状态；
- 进程内测完再测 HTTP（各自独占 GPU 推理，互不竞争）；
- 各测多次取中位数，过滤单次波动。

对比对象：infer_stream(token)（进程内）vs /tts/stream（HTTP，engine.py 路径）。
"""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_GSV_LITE = _PROJECT_ROOT / "GSV-TTS-Lite-main"
sys.path.insert(0, str(_GSV_LITE))
sys.path.insert(0, str(_PROJECT_ROOT))

import httpx
from ev.utils import config
from ev.tts.engine import _STREAM_CHUNK, _STREAM_OVERLAP, _SYNTH_PARAMS

MODELS_DIR = _PROJECT_ROOT / "tools" / "gsv_tts" / "API" / "models"
SERVER_URL = str(config.cfg.TTS_SERVER_URL or "http://127.0.0.1:8000").strip()
TEXT = "今天天气真好，适合出去散步，我们一起去公园吧。"
N = 5  # 每侧测试次数


def main():
    spk = str(config.cfg.GPTSOVITS_REF_AUDIO or "").strip()
    prompt = str(config.cfg.GPTSOVITS_PROMPT_TEXT or "").strip()
    if not spk or not prompt:
        print("缺少参考音频配置"); return

    from gsv_tts import TTS
    tts = TTS(models_dir=str(MODELS_DIR), sovits_cache=[50, 55])
    # 充分预热：跑 3 次让 GPT/SoVITS 图缓存覆盖常用长度
    print("进程内实例预热 ×3…")
    for i in range(3):
        for _ in tts.infer_stream(
            spk_audio_path=spk, prompt_audio_path=spk, prompt_audio_text=prompt,
            text=TEXT, stream_mode="token",
            stream_chunk=_STREAM_CHUNK, overlap_len=_STREAM_OVERLAP, **_SYNTH_PARAMS,
        ):
            pass
    print("预热完成\n")

    # 进程内：infer_stream(token)
    local_ttfa, local_done = [], []
    for i in range(N):
        t0 = time.perf_counter(); first = None
        for _ in tts.infer_stream(
            spk_audio_path=spk, prompt_audio_path=spk, prompt_audio_text=prompt,
            text=TEXT, stream_mode="token",
            stream_chunk=_STREAM_CHUNK, overlap_len=_STREAM_OVERLAP, **_SYNTH_PARAMS,
        ):
            if first is None:
                first = time.perf_counter()
        done = time.perf_counter()
        local_ttfa.append((first - t0) * 1000)
        local_done.append((done - t0) * 1000)
        print(f"  进程内 #{i+1}: 首块 {local_ttfa[-1]:.0f}ms | 完成 {local_done[-1]:.0f}ms")

    # HTTP：/tts/stream（engine.py 路径）
    async def http_pass():
        http_ttfa, http_done = [], []
        async with httpx.AsyncClient(timeout=120.0) as client:
            for i in range(N):
                payload = {
                    "text": TEXT, "speaker_audio": spk, "prompt_audio": spk,
                    "prompt_text": prompt, "stream_chunk": _STREAM_CHUNK,
                    "overlap_len": _STREAM_OVERLAP, **_SYNTH_PARAMS,
                }
                t0 = time.perf_counter(); first = None
                async with client.stream("POST", f"{SERVER_URL}/tts/stream", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        if json.loads(line[6:]).get("done"):
                            break
                        if first is None:
                            first = time.perf_counter()
                done = time.perf_counter()
                http_ttfa.append((first - t0) * 1000)
                http_done.append((done - t0) * 1000)
                print(f"  HTTP    #{i+1}: 首块 {http_ttfa[-1]:.0f}ms | 完成 {http_done[-1]:.0f}ms")
        return http_ttfa, http_done

    http_ttfa, http_done = asyncio.run(http_pass())

    print("\n" + "=" * 64)
    print(f"文本：{TEXT}（各测 {N} 次，进程内实例已充分预热）")
    for name, a, b in [
        ("首块(TTFA)", local_ttfa, http_ttfa),
        ("完成耗时    ", local_done, http_done),
    ]:
        ma, mb = statistics.median(a), statistics.median(b)
        print(f"{name}: 进程内 {ma:6.0f}ms | HTTP {mb:6.0f}ms | 差 {mb - ma:+.0f}ms ({(mb - ma) / ma * 100:+.1f}%)")
    print("=" * 64)


if __name__ == "__main__":
    main()
