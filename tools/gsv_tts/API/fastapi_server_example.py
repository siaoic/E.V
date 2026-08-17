"""
FastAPI 服务端使用示例
展示如何使用异步 TTS 接口处理并发请求
支持外链音频URL
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from gsv_tts import TTS
from starlette.background import BackgroundTask
import uuid
import os
import tempfile
import logging
import threading
import time
import numpy as np

app = FastAPI(title="GSV-TTS 异步 API", version="1.1")

models_dir = project_root / "API" / "models"
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

tts: Optional[TTS] = None

temp_dir = tempfile.mkdtemp(prefix="gsv_tts_")

# 未被下载的 wav 超时删除时限与清理扫描间隔（秒）：
# 正常路径下 wav 在 /audio 响应发送完成后即被删除（BackgroundTask），
# 客户端中断/下载失败等未下载文件由本守护线程兜底清理，防目录膨胀。
_OUTPUT_TTL_SECONDS = 600
_OUTPUT_SWEEP_INTERVAL = 60


def _sweep_stale_outputs() -> None:
    """后台守护：周期性删除 output 目录中超时未被下载的 wav。"""
    while True:
        time.sleep(_OUTPUT_SWEEP_INTERVAL)
        try:
            now = time.time()
            for f in os.listdir(output_dir):
                if not f.endswith(".wav"):
                    continue
                p = output_dir / f
                try:
                    if now - p.stat().st_mtime > _OUTPUT_TTL_SECONDS:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass


threading.Thread(target=_sweep_stale_outputs, daemon=True).start()


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


class TTSSingleRequest(BaseModel):
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
    # 文本切段与段间静音调参（客户端 _SYNTH_PARAMS 显式传入，缺省用服务端默认）
    cut_minlen: int = 10
    cut_mute: float = 0.3
    cut_mute_scale_map: dict = {}
    # 流式 token 块大小：首块产出速度与 SoVITS 解码稳定性的权衡点
    # （默认 12 与旧硬编码一致；调大如 25 首块更慢但短序列更稳）
    stream_chunk: int = 12


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
        sovits_cache=[50],
    )
    print("✅ TTS 模型加载完成！")


@app.get("/")
async def root():
    return {
        "message": "GSV-TTS 异步 API 服务已启动",
        "docs": "/docs",
        "features": {
            "url_support": True,
        }
    }


@app.post("/tts/single")
async def tts_single(request: TTSSingleRequest):
    """单个 TTS 请求的异步接口，支持外链音频"""
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
                detail="未提供 prompt_text（服务端已不加载 ASR 模型）"
            )
        
        audio_clip = await tts.infer_async(
            spk_audio_path=speaker_audio,
            prompt_audio_path=prompt_audio,
            prompt_audio_text=prompt_text,
            text=request.text,
            top_k=request.top_k,
            top_p=request.top_p,
            temperature=request.temperature,
            repetition_penalty=request.repetition_penalty,
            noise_scale=request.noise_scale,
            speed=request.speed,
        )
        
        output_filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
        output_path = output_dir / output_filename
        audio_clip.save(str(output_path))
        
        return {
            "success": True,
            "audio_len": audio_clip.audio_len_s,
            "filename": output_filename,
            "prompt_text_used": prompt_text,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/batch")
async def tts_batch(request: TTSBatchRequest):
    """批量 TTS 请求的异步接口，支持外链音频"""
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
                detail="未提供 prompt_text（服务端已不加载 ASR 模型）"
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
            cut_minlen=request.cut_minlen,
            cut_mute=request.cut_mute,
            cut_mute_scale_map=request.cut_mute_scale_map or {},
            return_subtitles=True,
        )

        filenames = []
        subtitles_list = []
        for clip in audio_clips:
            filename = f"tts_{uuid.uuid4().hex[:8]}.wav"
            output_path = output_dir / filename
            clip.save(str(output_path))
            filenames.append(filename)
            # 词级时间戳（orig_idx 相对本句文本），供客户端逐字显示字幕
            subtitles_list.append(clip.subtitles)

        return {
            "success": True,
            "count": len(audio_clips),
            "filenames": filenames,
            "subtitles": subtitles_list,
            "prompt_text_used": prompt_text,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/stream")
async def tts_stream(request: TTSBatchRequest):
    """流式 TTS：token 级流式合成，响应体为 int16 PCM 块（单声道 32k）。

    首块在 GPT 解码约 12 token 后即产出（boost_first_chunk，约 0.5s 音频），
    远早于整句合成完成，客户端可边收边播压低首字延迟——stream_chunk 越小
    首块解码越快，但块过小（<8 token）SoVITS 解码短序列易退化，12 为平衡值。
    流式合成逐块有重叠混音（SOLA），不返回词级字幕，字幕走整句显示。
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
                detail="未提供 prompt_text（服务端已不加载 ASR 模型）"
            )

        async def stream():
            async for clip in tts.infer_stream_async(
                spk_audio_path=speaker_audio,
                prompt_audio_path=prompt_audio,
                prompt_audio_text=prompt_text,
                text=request.texts[0],
                return_subtitles=False,
                is_cut_text=True,
                cut_minlen=request.cut_minlen,
                cut_mute=request.cut_mute,
                cut_mute_scale_map=request.cut_mute_scale_map or {},
                stream_mode="token",
                stream_chunk=request.stream_chunk,
                overlap_len=5,
                boost_first_chunk=True,
                top_k=request.top_k,
                top_p=request.top_p,
                temperature=request.temperature,
                repetition_penalty=request.repetition_penalty,
                noise_scale=request.noise_scale,
                speed=request.speed,
                debug=False,
            ):
                # float32[-1,1] → int16 需缩放，直接 astype 会全变 0；
                # 末尾奇数样本补 1 字节 0，保证块长偶数（客户端按 int16 解析）
                audio = np.clip(clip.audio_data * 32767.0, -32768, 32767)
                chunk = audio.astype("<i2").tobytes()
                if len(chunk) % 2:
                    chunk += b"\x00"
                yield chunk

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(tts.samplerate)},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _remove_audio_file(file_path: Path) -> None:
    """后台任务：音频响应发送完成后删除 wav 文件（重复拉取时幂等）。"""
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass


@app.get("/audio/{filename}")
async def get_audio(filename: str):
    """获取生成的音频文件（响应发送完成后即删除，防 output 目录膨胀）"""
    file_path = output_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件未找到")
    return FileResponse(
        file_path,
        media_type="audio/wav",
        background=BackgroundTask(_remove_audio_file, file_path),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8167)
