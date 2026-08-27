"""独立 TTS 流式服务端（Token 级流式，走 HTTP 供主程序调用）。

与官方 fastapi_server_example.py 的差异：官方原版只有 /tts/single、/tts/batch、
/audio，没有流式端点；本服务在 tools/gsv_tts **之外**新建，import 官方
gsv_tts.TTS 类（不改其任何代码），额外提供 /tts/stream Token 级流式端点
（复用官方 infer_stream_async），并保留 /tts/batch + /audio 作为批量兜底。

由 启动tts.bat 启动（端口 8000，与 .env 的 TTS_SERVER_URL 一致）。
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 项目根 = src/tts/server.py 的上三层；tools/gsv_tts 含 gsv_tts 包（官方原版）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_TOOLS_GSV = _PROJECT_ROOT / "tools" / "gsv_tts"
sys.path.insert(0, str(_TOOLS_GSV))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import List, Optional  # noqa: E402
from gsv_tts import TTS  # noqa: E402
import asyncio  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402

import numpy as np  # noqa: E402

app = FastAPI(title="GSV-TTS 流式 API", version="1.2")

models_dir = _TOOLS_GSV / "API" / "models"
output_dir = _TOOLS_GSV / "output"
output_dir.mkdir(exist_ok=True)

tts: Optional[TTS] = None

# 专用推理线程池：对齐 bench_gsv_modes.py 的进程内直连路径。单线程保证
# 串行推理（与 tts._infer_lock 双层保障），避免默认线程池被多请求/中断残留
# 任务占用，逐块编码也在工作线程内完成，事件循环不参与 CPU 加工。
_infer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gsv-infer")

temp_dir = tempfile.mkdtemp(prefix="gsv_tts_")


def is_url(path: str) -> bool:
    """检查是否为URL"""
    return path.startswith("http://") or path.startswith("https://")


async def download_audio(url: str) -> str:
    """下载音频URL到临时文件"""
    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()

    ext = ".wav"
    content_type = response.headers.get("content-type", "")
    if "mp3" in content_type or url.lower().endswith(".mp3"):
        ext = ".mp3"
    elif "ogg" in content_type or url.lower().endswith(".ogg"):
        ext = ".ogg"
    elif "flac" in content_type or url.lower().endswith(".flac"):
        ext = ".flac"

    temp_path = os.path.join(temp_dir, f"download_{uuid.uuid4().hex}{ext}")
    with open(temp_path, "wb") as f:
        f.write(response.content)

    logging.info(f"下载音频到: {temp_path}")
    return temp_path


class TTSStreamRequest(BaseModel):
    text: str
    speaker_audio: str
    prompt_audio: str
    prompt_text: Optional[str] = None
    top_k: int = 5
    top_p: float = 0.9
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    noise_scale: float = 0.5
    speed: float = 1.0
    stream_chunk: int = 25
    overlap_len: int = 5


class TTSBatchRequest(BaseModel):
    texts: List[str]
    speaker_audio: str
    prompt_audio: str
    prompt_text: Optional[str] = None
    top_k: int = 5
    top_p: float = 0.9
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    noise_scale: float = 0.5
    speed: float = 1.0


@app.on_event("startup")
async def startup_event():
    global tts
    print("🚀 正在加载 TTS 模型...")

    max_cache_len = 1024
    batch_sizes = [1, 4, 8]
    cache_lens = []
    length = 512
    while length <= max_cache_len:
        cache_lens.append(length)
        length *= 2
    gpt_cache = [(b, c) for b in batch_sizes for c in cache_lens]

    tts = TTS(
        models_dir=str(models_dir),
        gpt_cache=gpt_cache,
        # 50 = stream_chunk * 2 = 25 * 2, 55 = stream_chunk * 2 + overlap_len
        sovits_cache=[50, 55],
    )

    # GPT 的 SDPA 后端从 CUDNN_ATTENTION 换成 EFFICIENT_ATTENTION：
    # cuDNN attention 对每个新序列形状会做一次 kernel 编译（约 400ms，导致新文本
    # 首字延迟偏高），EFFICIENT 无编译开销。仅运行时替换 t2s_model 模块级变量，
    # 不改任何库源码。
    try:
        import gsv_tts.GPT_SoVITS.GPT.t2s_model as _tm
        from torch.nn.attention import SDPBackend as _SDPBackend

        _tm.SDPBACKEND = _SDPBackend.EFFICIENT_ATTENTION
    except Exception:
        pass

    print("✅ TTS 模型加载完成！")


@app.get("/")
async def root():
    return {
        "message": "GSV-TTS 流式 API 服务已启动",
        "docs": "/docs",
        "features": {
            "url_support": True,
            "auto_asr": False,
        },
    }


@app.post("/tts/stream")
async def tts_stream(request: TTSStreamRequest):
    """Token 级流式 TTS 接口：SSE 边合成边返回音频块 + 词级时间戳。

    基于官方 infer_stream（stream_mode="token"）实现：GPT 按 stream_chunk
    个 token 累积后解码出音频块即返回，客户端首块到达即可播放，首字延迟
    只受「首块合成」限制。每个事件为 JSON：audio（int16 PCM 的 base64）、
    subtitles（词级时间戳，相对整句起点）、orig_text、audio_len_s；流结束
    发送 {"done": true}。
    """
    try:
        speaker_audio = request.speaker_audio
        prompt_audio = request.prompt_audio
        prompt_text = request.prompt_text

        if is_url(speaker_audio):
            speaker_audio = await download_audio(speaker_audio)

        if is_url(prompt_audio):
            prompt_audio = await download_audio(prompt_audio)

        if prompt_text is None or prompt_text == "":
            raise HTTPException(
                status_code=400,
                detail="无法自动识别prompt_audio文本，请手动提供prompt_text"
            )

        async def gen():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def _worker():
                """专用线程内直跑官方 infer_stream（与 bench_gsv_modes.py 进程内
                路径一致），逐块 int16/base64/SSE 编码全部在工作线程完成，事件
                循环只负责转发，避免与 GPT 解码争抢 CPU。
                """
                try:
                    with tts._infer_lock:
                        for chunk in tts.infer_stream(
                            spk_audio_path=speaker_audio,
                            prompt_audio_path=prompt_audio,
                            prompt_audio_text=prompt_text,
                            text=request.text,
                            return_subtitles=True,
                            stream_mode="token",
                            stream_chunk=request.stream_chunk,
                            overlap_len=request.overlap_len,
                            boost_first_chunk=True,
                            top_k=request.top_k,
                            top_p=request.top_p,
                            temperature=request.temperature,
                            repetition_penalty=request.repetition_penalty,
                            noise_scale=request.noise_scale,
                            speed=request.speed,
                        ):
                            audio = (chunk.audio_data * 32767).astype(np.int16).tobytes()
                            event = {
                                "audio": base64.b64encode(audio).decode("ascii"),
                                "subtitles": chunk.subtitles,
                                "orig_text": chunk.orig_text,
                                "audio_len_s": chunk.audio_len_s,
                            }
                            loop.call_soon_threadsafe(
                                queue.put_nowait, f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                            )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, 'data: {"done": true}\n\n')

            loop.run_in_executor(_infer_executor, _worker)
            while True:
                line = await queue.get()
                yield line
                if line == 'data: {"done": true}\n\n':
                    break

        return StreamingResponse(gen(), media_type="text/event-stream")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/batch")
async def tts_batch(request: TTSBatchRequest):
    """批量 TTS 请求的异步接口（engine.py 流式失败/退化时的整句兜底）"""
    try:
        speaker_audio = request.speaker_audio
        prompt_audio = request.prompt_audio
        prompt_text = request.prompt_text

        if is_url(speaker_audio):
            speaker_audio = await download_audio(speaker_audio)

        if is_url(prompt_audio):
            prompt_audio = await download_audio(prompt_audio)

        if prompt_text is None or prompt_text == "":
            raise HTTPException(
                status_code=400,
                detail="无法自动识别prompt_audio文本，请手动提供prompt_text"
            )

        audio_clips = await tts.infer_batched_async(
            spk_audio_paths=speaker_audio,
            prompt_audio_paths=prompt_audio,
            prompt_audio_texts=prompt_text,
            texts=request.texts,
            top_k=request.top_k,
            top_p=request.top_p,
            temperature=request.temperature,
            repetition_penalty=request.repetition_penalty,
            noise_scale=request.noise_scale,
            speed=request.speed,
        )

        filenames = []
        for clip in audio_clips:
            filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
            output_path = output_dir / filename
            clip.save(str(output_path))
            filenames.append(filename)

        return {
            "success": True,
            "count": len(audio_clips),
            "filenames": filenames,
            "prompt_text_used": prompt_text,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    """获取生成的音频文件"""
    file_path = output_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件未找到")
    return FileResponse(file_path, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
