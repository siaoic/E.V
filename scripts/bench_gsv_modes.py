"""GSV-TTS-Lite 各推理方式 vs engine.py(HTTP) 速度对比脚本（临时测量用，不入测试集）。

统一参数（对齐 src/tts/engine.py 的 _SYNTH_PARAMS + server 流式默认）：
    top_k=5, top_p=0.9, temperature=1.0, repetition_penalty=1.35,
    noise_scale=0.5, speed=1.0, stream_chunk=25, overlap_len=5, boost_first_chunk=True

对比路径：
  进程内（GSV-TTS-Lite-main 官方包，models_dir=tools/gsv_tts/API/models）：
    infer                 单句整句合成            TTFA = 完成
    infer_stream token    流式（25 token/块）     TTFA = 首块到达
    infer_stream sentence 流式（按句出块）        TTFA = 首块到达
    infer_batched         批量合成（1 条）        TTFA = 完成
  HTTP 8000（= src/tts/engine.py 网络路径，绕过磁盘缓存）：
    /tts/stream           流式端点               TTFA = 首块到达
    /tts/batch + /audio   整句端点 + 下载        TTFA = 完成

指标：TTFA（首音频到达 ms）、完成耗时（ms）、音频时长（s）、实时率 = 完成耗时/音频时长。
"""
import asyncio
import base64
import json
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

TEXTS = [
    ("短", "晚上好呀。"),
    ("中", "今天天气真好，适合出去散步。"),
    ("长", "人工智能正在改变我们的生活，语音合成技术也越来越成熟了，这个项目真的很有趣。"),
]

STREAM_KW = {"stream_chunk": _STREAM_CHUNK, "overlap_len": _STREAM_OVERLAP}


def _ref() -> tuple:
    """与 engine.py 相同的参考参数（主参考音频 + 提示文本）。"""
    main = str(config.cfg.GPTSOVITS_REF_AUDIO or "").strip()
    prompt = str(config.cfg.GPTSOVITS_PROMPT_TEXT or "").strip()
    return main, main, prompt


def _dur(clip) -> float:
    try:
        return float(clip.audio_len_s)
    except Exception:
        try:
            return len(clip.audio_data) / clip.samplerate
        except Exception:
            return 0.0


def bench_infer(tts, spk, prompt_audio, prompt_text, text):
    t0 = time.perf_counter()
    clip = tts.infer(
        spk_audio_path=spk, prompt_audio_path=prompt_audio,
        prompt_audio_text=prompt_text, text=text, **_SYNTH_PARAMS,
    )
    done = time.perf_counter()
    return (done - t0) * 1000, (done - t0) * 1000, _dur(clip)


def bench_stream(tts, spk, prompt_audio, prompt_text, text, mode):
    t0 = time.perf_counter()
    first = None
    total_dur = 0.0
    for chunk in tts.infer_stream(
        spk_audio_path=spk, prompt_audio_path=prompt_audio,
        prompt_audio_text=prompt_text, text=text,
        stream_mode=mode, **_SYNTH_PARAMS, **STREAM_KW,
    ):
        if first is None:
            first = time.perf_counter()
        # 按实际采样数累计（token 流式块间 overlap 会使总量略大于播放时长）
        total_dur += len(chunk.audio_data) / chunk.samplerate
    done = time.perf_counter()
    return (first - t0) * 1000, (done - t0) * 1000, total_dur


def bench_batched(tts, spk, prompt_audio, prompt_text, text):
    t0 = time.perf_counter()
    clips = tts.infer_batched(
        spk_audio_paths=spk, prompt_audio_paths=prompt_audio,
        prompt_audio_texts=prompt_text, texts=[text], **_SYNTH_PARAMS,
    )
    done = time.perf_counter()
    dur = sum(_dur(c) for c in clips)
    return (done - t0) * 1000, (done - t0) * 1000, dur


async def bench_http_stream(client, spk, prompt_audio, prompt_text, text):
    payload = {
        "text": text, "speaker_audio": spk, "prompt_audio": prompt_audio,
        "prompt_text": prompt_text, **STREAM_KW, **_SYNTH_PARAMS,
    }
    t0 = time.perf_counter()
    first = None
    dur = 0.0
    async with client.stream("POST", f"{SERVER_URL}/tts/stream", json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            evt = json.loads(line[6:])
            if evt.get("done"):
                break
            if first is None:
                first = time.perf_counter()
            dur = max(dur, float(evt.get("audio_len_s") or 0.0))
    done = time.perf_counter()
    return (first - t0) * 1000, (done - t0) * 1000, dur


async def bench_http_batch(client, spk, prompt_audio, prompt_text, text):
    payload = {
        "texts": [text], "speaker_audio": spk, "prompt_audio": prompt_audio,
        "prompt_text": prompt_text, **_SYNTH_PARAMS,
    }
    t0 = time.perf_counter()
    resp = await client.post(f"{SERVER_URL}/tts/batch", json=payload)
    resp.raise_for_status()
    filenames = resp.json().get("filenames") or []
    dur = 0.0
    for name in filenames:
        r2 = await client.get(f"{SERVER_URL}/audio/{name}")
        r2.raise_for_status()
        content = r2.content
        if content:
            import io
            import soundfile as sf
            data, sr = sf.read(io.BytesIO(content), dtype="float32")
            dur = len(data) / sr
    done = time.perf_counter()
    return (done - t0) * 1000, (done - t0) * 1000, dur


def _fmt(ttfa, done, dur):
    return f"{ttfa:8.0f} | {done:8.0f} | {dur:5.2f}s | {done / max(dur, 1e-6):5.2f}x"


def main():
    spk, prompt_audio, prompt_text = _ref()
    if not spk or not prompt_text:
        print("缺少 GPTSOVITS_REF_AUDIO / GPTSOVITS_PROMPT_TEXT 配置，退出")
        return

    print("加载 GSV-TTS-Lite 官方包（进程内）…")
    from gsv_tts import TTS
    max_cache_len = 1024
    cache_lens = [512]
    while cache_lens[-1] < max_cache_len:
        cache_lens.append(cache_lens[-1] * 2)
    gpt_cache = [(b, c) for b in (1, 4, 8) for c in cache_lens]
    tts = TTS(models_dir=str(MODELS_DIR), gpt_cache=gpt_cache, sovits_cache=[50, 55])
    print("预热（infer）…")
    tts.infer(spk_audio_path=spk, prompt_audio_path=prompt_audio,
              prompt_audio_text=prompt_text, text="预热测试", **_SYNTH_PARAMS)

    rows = []
    for label, text in TEXTS:
        print(f"\n=== 文本[{label}]：{text} ===")
        ttfa, done, dur = bench_infer(tts, spk, prompt_audio, prompt_text, text)
        print(f"  inproc infer          : {_fmt(ttfa, done, dur)}")
        rows.append(("infer(整句)", label, ttfa, done, dur))
        ttfa, done, dur = bench_stream(tts, spk, prompt_audio, prompt_text, text, "token")
        print(f"  inproc stream(token)  : {_fmt(ttfa, done, dur)}")
        rows.append(("infer_stream(token)", label, ttfa, done, dur))
        ttfa, done, dur = bench_stream(tts, spk, prompt_audio, prompt_text, text, "sentence")
        print(f"  inproc stream(sent)   : {_fmt(ttfa, done, dur)}")
        rows.append(("infer_stream(sentence)", label, ttfa, done, dur))
        ttfa, done, dur = bench_batched(tts, spk, prompt_audio, prompt_text, text)
        print(f"  inproc batched        : {_fmt(ttfa, done, dur)}")
        rows.append(("infer_batched(1条)", label, ttfa, done, dur))

    async def http_pass():
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 预热流式链路（服务端图已由启动 warmup 编译，这里兜底）
            try:
                await bench_http_stream(client, spk, prompt_audio, prompt_text, "预热测试")
            except Exception as e:
                print(f"  流式预热失败：{e}")
            for label, text in TEXTS:
                print(f"\n=== HTTP 文本[{label}]：{text} ===")
                ttfa, done, dur = await bench_http_stream(client, spk, prompt_audio, prompt_text, text)
                print(f"  http /tts/stream      : {_fmt(ttfa, done, dur)}")
                rows.append(("engine.py 流式(/tts/stream)", label, ttfa, done, dur))
                ttfa, done, dur = await bench_http_batch(client, spk, prompt_audio, prompt_text, text)
                print(f"  http /tts/batch       : {_fmt(ttfa, done, dur)}")
                rows.append(("engine.py 兜底(/tts/batch)", label, ttfa, done, dur))

    asyncio.run(http_pass())

    print("\n" + "=" * 78)
    print(f"{'路径':<26} | {'文本':<4} | {'首音频ms':>8} | {'完成ms':>8} | {'时长':>6} | {'实时率':>6}")
    print("-" * 78)
    for path, label, ttfa, done, dur in rows:
        print(f"{path:<26} | {label:<4} | {ttfa:>8.0f} | {done:>8.0f} | {dur:>5.2f}s | {done / max(dur, 1e-6):>5.2f}x")
    print("=" * 78)


if __name__ == "__main__":
    main()
