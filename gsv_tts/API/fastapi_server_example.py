"""
FastAPI 服务端使用示例
展示如何使用异步 TTS 接口处理并发请求
支持外链音频URL和ASR自动识别
"""

import sys
import base64
import io
import json
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from gsv_tts import TTS
import soundfile as sf
import uuid
import os
import tempfile
import logging

app = FastAPI(title="GSV-TTS 异步 API", version="1.1")

models_dir = project_root / "API" / "models"
output_dir = project_root.parent / "temp"
output_dir.mkdir(exist_ok=True)

tts: Optional[TTS] = None
asr = None

temp_dir = tempfile.mkdtemp(prefix="gsv_tts_")


def is_url(path: str) -> bool:
    """检查是否为URL"""
    return path.startswith("http://") or path.startswith("https://")


async def download_audio(url: str) -> str:
    """下载音频URL到临时文件"""
    import httpx
    import warnings
    warnings.filterwarnings("ignore", message=".*verify.*")
    
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
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


def transcribe_audio(audio_path: str) -> str:
    """使用ASR识别音频文本"""
    global asr
    if asr is None:
        raise HTTPException(status_code=500, detail="ASR模型未启用，请设置 --use_asr 或提供 prompt_text")
    
    results = asr.transcribe(audio_path)
    if results and len(results) > 0:
        result = results[0]
        if hasattr(result, 'text'):
            text = result.text
        elif isinstance(result, dict):
            text = result.get("text", "")
        else:
            text = str(result)
        logging.info(f"ASR识别结果: {text}")
        return text
    return ""


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
    gpt_model: Optional[str] = None
    sovits_model: Optional[str] = None


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
    gpt_model: Optional[str] = None
    sovits_model: Optional[str] = None


class TTSStreamRequest(BaseModel):
    """流式 TTS 请求（token 级流式，边合成边输出，专为低首帧延迟设计）。"""
    text: str
    speaker_audio: str
    prompt_audio: str
    prompt_text: Optional[str] = None
    top_k: int = 15
    top_p: float = 1.0
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    noise_scale: float = 0.5
    speed: float = 1.0
    # —— 流式参数（默认值与 GSV-TTS 的 infer_stream 一致） ——
    stream_chunk: int = 25        # 每块语义 token 数（与服务端 sovits_cache=[50] 严格匹配：50 = chunk*2）
    overlap_len: int = 5          # 块间重叠 token 数（SOLA 交叉淡化平滑衔接）
    boost_first_chunk: bool = True  # 首块低延迟优化（GPT 生成少量 token 即出音频）
    gpt_model: Optional[str] = None
    sovits_model: Optional[str] = None


@app.on_event("startup")
async def startup_event():
    global tts, asr
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
        sovits_cache=[50, 55],
    )
    print("✅ TTS 模型加载完成！")

    # —— 预热：预加载语言模块与模型权重，吃掉首句推理的一次性开销 ——
    # （首次合成请求还会做参考音频编码 / CUDA graph 编译，由客户端预热请求覆盖）
    try:
        tts.init_language_module("zh", "en", "ja")
        # 默认使用 Neuro 模型（与主程序参考音频配套）；如需其他模型改这两行即可
        neuro_dir = Path(__file__).resolve().parents[1] / "API" / "models"
        tts.load_gpt_model(str(neuro_dir / "Neuro-e15.ckpt"))
        tts.load_sovits_model(str(neuro_dir / "Neuro_e8_s1712.pth"))
        print("✅ TTS 模型与语言模块已预加载")
    except Exception as e:
        print(f"⚠️ TTS 预加载失败（不影响使用）：{e}")
    
    use_asr = os.environ.get("USE_ASR", "true").lower() == "true"
    if use_asr:
        try:
            import torch
            from huggingface_hub import snapshot_download
            # 国内直连 huggingface.co 常因 SSL 证书校验失败（TTS 走 ModelScope 正常），
            # 默认切换到 hf-mirror.com 镜像下载 ASR 模型
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            
            local_model_base = models_dir / "qwen3_asr"
            repo_id = "Qwen/Qwen3-ASR-0.6B"

            # 尝试查找本地已存在的模型，支持几种常见目录结构
            local_model_path = None
            candidate1 = local_model_base
            candidate2 = local_model_base / "Qwen" / "Qwen3-ASR-0.6B"
            candidate3 = local_model_base / "Qwen3-ASR-0.6B"
            for cand in (candidate1, candidate2, candidate3):
                if cand.exists() and (cand / "config.json").exists():
                    local_model_path = cand
                    break

            if local_model_path is None:
                print(f"⬇️ 本地未找到ASR模型，正在下载: {repo_id} 到 {local_model_base}")
                try:
                    snapshot_download(
                        repo_id=repo_id,
                        local_dir=str(local_model_base),
                        local_dir_use_symlinks=False,
                    )
                except Exception as ssl_err:
                    if "CERTIFICATE_VERIFY_FAILED" in str(ssl_err):
                        print("⚠️ SSL验证失败，尝试禁用SSL验证后重试...")
                        import ssl as _ssl
                        _ssl._create_default_https_context = _ssl._create_unverified_context
                        snapshot_download(
                            repo_id=repo_id,
                            local_dir=str(local_model_base),
                            local_dir_use_symlinks=False,
                        )
                    else:
                        raise
                print("✅ ASR模型下载完成！")

                # 尝试在下载后定位模型路径
                for cand in (candidate1, candidate2, candidate3):
                    if cand.exists() and (cand / "config.json").exists():
                        local_model_path = cand
                        break

            if local_model_path is None:
                print("⚠️ 未能定位到ASR模型的有效目录，跳过ASR加载。")
                asr = None
            else:
                from qwen_asr import Qwen3ASRModel
                print(f"🚀 正在加载 ASR 模型，路径: {local_model_path}")
                # 如果目标环境不支持 bfloat16，可改为 float16 或 float32
                dtype_choice = torch.bfloat16 if hasattr(torch, 'bfloat16') else torch.float16
                asr = Qwen3ASRModel.from_pretrained(
                    str(local_model_path),
                    dtype=dtype_choice,
                    device_map="cuda:0",
                    trust_remote_code=True
                )
                print("✅ ASR 模型加载完成！")
        except Exception as e:
            print(f"⚠️ ASR 模型加载失败: {e}")
            print("💡 提示：如果没有提供 prompt_text，请求将会失败")
            asr = None
    else:
        print("ℹ️ ASR 模型已禁用")


@app.get("/")
async def root():
    return {
        "message": "GSV-TTS 异步 API 服务已启动",
        "docs": "/docs",
        "features": {
            "url_support": True,
            "auto_asr": asr is not None
        }
    }


@app.post("/tts/single")
async def tts_single(request: TTSSingleRequest):
    """单个 TTS 请求的异步接口，支持外链音频和自动ASR"""
    try:
        speaker_audio = request.speaker_audio
        prompt_audio = request.prompt_audio
        prompt_text = request.prompt_text
        
        if is_url(speaker_audio):
            speaker_audio = await download_audio(speaker_audio)
        
        if is_url(prompt_audio):
            prompt_audio = await download_audio(prompt_audio)
        
        if prompt_text is None or prompt_text == "":
            prompt_text = transcribe_audio(prompt_audio)
            if not prompt_text:
                raise HTTPException(
                    status_code=400, 
                    detail="无法自动识别prompt_audio文本，请手动提供prompt_text"
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
            gpt_model=request.gpt_model,
            sovits_model=request.sovits_model,
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
    """批量 TTS 请求的异步接口，支持外链音频和自动ASR"""
    try:
        speaker_audio = request.speaker_audio
        prompt_audio = request.prompt_audio
        prompt_text = request.prompt_text
        
        if is_url(speaker_audio):
            speaker_audio = await download_audio(speaker_audio)
        
        if is_url(prompt_audio):
            prompt_audio = await download_audio(prompt_audio)
        
        if prompt_text is None or prompt_text == "":
            prompt_text = transcribe_audio(prompt_audio)
            if not prompt_text:
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
            gpt_model=request.gpt_model,
            sovits_model=request.sovits_model,
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


@app.post("/tts/stream")
async def tts_stream(request: TTSStreamRequest):
    """流式 TTS：token 级边合成边输出，NDJSON 逐行返回 base64 wav 音频块。

    首块在 GPT 仅生成少量语义 token（stream_chunk=25）时就经 SoVITS 解码输出，
    客户端收到第一块即可开始播放——首帧延迟从「整句完整合成」降到「首块合成」。
    每行 JSON：
        {"audio": "<base64 wav>", "audio_len": <秒>, "orig_text": "...", "subtitles": [...]}  # 一个音频块（subtitles 为字级时间戳，相对整句起点的全局时间轴）
        {"done": true}                                  # 全部结束
        {"error": "..."}                                # 出错
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
            prompt_text = transcribe_audio(prompt_audio)
            if not prompt_text:
                raise HTTPException(
                    status_code=400,
                    detail="无法自动识别prompt_audio文本，请手动提供prompt_text"
                )

        async def gen():
            try:
                async for clip in tts.infer_stream_async(
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
                    return_subtitles=True,
                    gpt_model=request.gpt_model,
                    sovits_model=request.sovits_model,
                    stream_chunk=request.stream_chunk,
                    overlap_len=request.overlap_len,
                    boost_first_chunk=request.boost_first_chunk,
                ):
                    buf = io.BytesIO()
                    sf.write(buf, clip.audio_data, clip.samplerate, format="WAV")
                    item = {
                        "audio": base64.b64encode(buf.getvalue()).decode("ascii"),
                        "audio_len": float(clip.audio_len_s),
                        "orig_text": clip.orig_text,
                        "subtitles": clip.subtitles,
                    }
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                yield json.dumps({"done": True}) + "\n"
            except Exception as e:
                logging.error(f"流式 TTS 失败: {e}")
                yield json.dumps({"error": str(e)}, ensure_ascii=False) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
