"""
FastAPI 服务端使用示例
展示如何使用异步 TTS 接口处理并发请求
支持外链音频URL和ASR自动识别
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from gsv_tts import TTS
import uuid
import os
import tempfile
import logging

app = FastAPI(title="GSV-TTS 异步 API", version="1.1")

models_dir = project_root / "API" / "models"
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

tts: Optional[TTS] = None
asr = None

temp_dir = tempfile.mkdtemp(prefix="gsv_tts_")

# 静态 CUDA graph 缓存：GPT 覆盖常用 (batch, seq_len) 组合，SoVITS 固定 50
_GPT_CACHE = [(b, c) for b in (1, 4, 8) for c in (512, 1024)]
_SOVITS_CACHE = [50]

# 推理文本语言：auto 走 LangSegment 自动识别（中日英混合均适用）
_TEXT_LANG = "auto"
# 参考音频文本语言
_REF_LANG = "zh"

# 段间静音（秒）：长文本被标点切段后，段与段拼接处插入的停顿。
# GSV 默认 cut_mute=0.4 过长（句号处 0.6s），听感像一字一顿地读；
# 0.0 即不插人工静音，纯靠模型自带停顿。
_CUT_MUTE = 0.3
# 文本切段最短长度（GSV 默认值，低于此长度与前后段合并）
_CUT_MINLEN = 10
# 各标点对段间静音的倍率（相对 _CUT_MUTE；句末长停稍长、顿号稍短）
_CUT_MUTE_SCALE_MAP = {
    "…": 2.0, ".": 1.5, "。": 1.5, "?": 1.5, "？": 1.5, "!": 1.5, "！": 1.5,
    ",": 1.0, "，": 1.0, ":": 1.0, "：": 1.0, ";": 1.0, "；": 1.0, "~": 1.0,
    "、": 0.8, "・": 0.8,
}


def is_url(path: str) -> bool:
    """检查是否为URL"""
    return path.startswith("http://") or path.startswith("https://")


def _env_value(key: str) -> str:
    """读取配置项：优先取环境变量，其次从项目根 .env 手动解析。

    tts.bat 只激活 runtime 虚拟环境、不加载 .env；客户端（主程序）与
    服务端共用同一份 .env，这里按同样规则取值，保证预热用的参考音频
    与真实请求一致。.env 常见写法支持 "value" / 'value' 引号包裹。
    """
    value = os.environ.get(key, "").strip()
    if value:
        return value
    try:
        for line in (project_root / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    except OSError:
        pass
    return ""


async def _warmup_server() -> None:
    """服务端启动预热：合成一句短文本但不播放、不落盘。

    模型在 startup 只加载权重，首次合成才编译 CUDA graph / 缓存参考
    音频特征；预热把这份开销提前消化，主播第一次说话不必再等。
    参考音频与提示文本读自 .env（GPTSOVITS_REF_AUDIO / PROMPT_TEXT），
    与客户端共用同一份配置；未配置或合成失败时静默跳过，不影响启动。
    """
    global tts
    if tts is None:
        return
    ref_audio = _env_value("GPTSOVITS_REF_AUDIO")
    if not ref_audio:
        print("ℹ️ 未配置 GPTSOVITS_REF_AUDIO，跳过启动预热")
        return
    prompt_text = _env_value("GPTSOVITS_PROMPT_TEXT")
    try:
        # 采样参数与客户端 engine.py _SYNTH_PARAMS 保持一致
        await tts.infer_batched_async(
            spk_audio_paths=[ref_audio],
            prompt_audio_paths=[ref_audio],
            prompt_audio_texts=[prompt_text],
            texts=["你好呀"],
            text_languages=_TEXT_LANG,
            prompt_languages=_REF_LANG,
            cut_minlen=_CUT_MINLEN,
            cut_mute=_CUT_MUTE,
            cut_mute_scale_map=_CUT_MUTE_SCALE_MAP,
            top_k=15, top_p=1.0, temperature=1.0,
            repetition_penalty=1.35, noise_scale=0.5, speed=1.0,
        )
        print("✅ 服务端预热完成（合成管线已就绪）")
    except Exception as e:
        print(f"⚠️ 服务端预热失败（不影响使用）：{e}")


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
    text_language: str = _TEXT_LANG
    prompt_language: str = _REF_LANG
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
    text_language: str = _TEXT_LANG
    prompt_language: str = _REF_LANG
    cut_minlen: int = _CUT_MINLEN
    cut_mute: float = _CUT_MUTE
    cut_mute_scale_map: dict = _CUT_MUTE_SCALE_MAP
    top_k: int = 5
    top_p: float = 0.9
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    noise_scale: float = 0.5
    speed: float = 1.0


@app.on_event("startup")
async def startup_event():
    global tts, asr
    print("🚀 正在加载 TTS 模型...")

    tts = TTS(
        models_dir=str(models_dir),
        gpt_cache=_GPT_CACHE,
        sovits_cache=_SOVITS_CACHE,
    )
    print("✅ TTS 模型加载完成！")
    
    use_asr = os.environ.get("USE_ASR", "true").lower() == "true"
    if use_asr:
        try:
            import torch
            
            local_model_path = models_dir / "qwen3_asr"
            
            if not (local_model_path.exists() and (local_model_path / "config.json").exists()):
                from gsv_tts.Download import download_file
                
                print("⬇️ 本地未找到ASR模型，正在从ModelScope下载: Qwen/Qwen3-ASR-0.6B")
                local_model_path.mkdir(parents=True, exist_ok=True)
                base_url = "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/%s"
                for filename in [
                    "config.json",
                    "chat_template.json",
                    "generation_config.json",
                    "merges.txt",
                    "model.safetensors",
                    "preprocessor_config.json",
                    "tokenizer_config.json",
                    "vocab.json",
                ]:
                    download_file(base_url % filename, str(local_model_path / filename))
                print("✅ ASR模型下载完成！")
            
            from qwen_asr import Qwen3ASRModel
            print("🚀 正在加载 ASR 模型...")
            asr = Qwen3ASRModel.from_pretrained(
                str(local_model_path),
                dtype=torch.bfloat16,
                device_map="cuda:0",
                local_files_only=True
            )
            print("✅ ASR 模型加载完成！")
        except Exception as e:
            print(f"⚠️ ASR 模型加载失败: {e}")
            print("💡 提示：如果没有提供 prompt_text，请求将会失败")
            asr = None
    else:
        print("ℹ️ ASR 模型已禁用")

    # 预热：启动后立即合成一句短文本（不播放、不落盘），提前编译
    # CUDA graph / 缓存参考音频特征，降低真实首句的合成延迟
    await _warmup_server()


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
            text_language=request.text_language,
            prompt_language=request.prompt_language,
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
            text_languages=request.text_language,
            prompt_languages=request.prompt_language,
            cut_minlen=request.cut_minlen,
            cut_mute=request.cut_mute,
            cut_mute_scale_map=request.cut_mute_scale_map,
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
