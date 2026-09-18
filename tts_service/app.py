# -*- coding: utf-8 -*-
"""基于 GSV-TTS-Lite 的流式语音合成服务。

把一段文本交给 GSV-TTS-Lite 流式（token 级）合成，并把每一段音频转为
`int16` 的单声道 PCM，通过 SSE 事件逐 chunk 下发给播放端，播放端即可边收边播。

推理链路对齐官方 GSV-TTS-Lite（https://github.com/chinokikiss/GSV-TTS-Lite）的推荐用法：
    - 异步接口 `TTS.infer_stream_async`：内部把同步流式推理放进当前事件循环
      的默认线程池执行（线程复用，无每次新建线程的冷启动开销），并用实例内置
      `_infer_lock` 串行化，规避 LangSegment 等静态变量的线程安全问题，
      因此服务层无需再自建线程与锁；
    - 构造参数严格对齐官方流式示例：`TTS(use_bert=True, sovits_cache=[50, 55])`，
      其中 sovits_cache 按官方注释公式 `[stream_chunk*2, stream_chunk*2+overlap_len]`
      从直播配置推导（默认 25/5 → [50, 55]）；gpt_cache 不传（官方流式示例亦不传，
      默认已含 batch=1 各档 CUDA Graph 缓存；官方 server 示例的 [1,4,8] 大批次
      缓存仅多用户并发受益）；
    - 推理参数严格对齐官方流式示例：只显式传 `stream_chunk` / `overlap_len` /
      `debug=False`；stream_mode 默认即 "token"、boost_first_chunk 默认即 True，
      各采样参数官方流式示例均未传，全部沿用库默认；
    - 模型在 lifespan 启动阶段构造并显式预加载（对齐官方 server 的 startup
      加载模式，load_gpt_model / load_sovits_model 为官方预加载 API），
      避免首个请求承担模型加载延迟；
    - 预热对齐官方推荐：`cache_spk_audio` / `cache_prompt_audio` 预提取参考音频
      缓存，并用真实参数跑一次完整流式推理（首次捕获 CUDA Graph + 复热推理线程）。

协议（`text/event-stream`）：
    event: meta        首段音频到来时携带采样率等元信息
        data: {"sample_rate": 32000, "origin_text": "..."}
    event: chunk       一段 int16 单声道 PCM，base64 编码
        data: <base64>
    event: error       合成过程出错
    event: done        全部合成完成

独立进程启动（需先为 GSV-TTS-Lite 准备 torch / 推理后端环境）：
    uv run python app.py --preload

依赖：fastapi uvicorn numpy soundfile gsv-tts-lite
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from gsv_tts import TTS

# 服务监听地址（避开 MaiBot 的 8000 / WebUI 8001 / 前端 7999）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8095

# 直播语音配置：参考音频默认值与预热参数都从这里读取，与客户端播放链路同源
_LIVE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "bilibili_live.toml"

# 预训练模型下载目录：默认放在 tts_service/models，避免散落到用户主目录缓存
DEFAULT_MODELS_DIR = Path(__file__).resolve().parent / "models"

# 预热用短文本（只用于触发 CUDA Graph 捕获与线程复热，不对外提供合成）
_WARMUP_TEXT = "你好呀，欢迎来到直播间。"

logger = logging.getLogger("gsv_tts_stream")

# TTS 实例：lifespan 启动阶段构造并加载（对齐官方 server 的全局变量 + startup 模式）
tts: TTS | None = None

# --preload 开关：在 uvicorn startup 阶段（服务事件循环内）执行官方推荐预热
_warmup_enabled = False

# 直播语音配置的进程内缓存（启动后配置不再变化，读一次即可）
_voice_config_cache: dict | None = None


def load_voice_config() -> dict:
    """读取直播语音配置 [voice]（与客户端 player.py 播放链路同源）。

    首次读取后缓存；文件不存在或解析失败时返回空 dict 并记录警告，
    由调用方（请求端点）以 400 明确报错，不静默兜底。
    """
    global _voice_config_cache
    if _voice_config_cache is None:
        try:
            with open(_LIVE_CONFIG_PATH, "rb") as f:
                _voice_config_cache = tomllib.load(f).get("voice") or {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("读取直播语音配置 %s 失败：%s", _LIVE_CONFIG_PATH, exc)
            _voice_config_cache = {}
    return _voice_config_cache


def compose_infer_params(
    *,
    text: str,
    prompt_audio_path: str,
    prompt_audio_text: str,
    spk_paths: list[str],
    stream_chunk: int,
    overlap_len: int,
) -> dict:
    """构造官方流式接口 infer_stream 的参数。

    - 主参考音频作风格参考（prompt），需其文本 `prompt_audio_text`；
    - 追加参考为音色参考（spk）：单个直接传字符串，多个以等权重 dict 融合；
    - 严格对齐官方流式示例：只显式传 `stream_chunk` / `overlap_len` / `debug=False`，
      stream_mode 默认即 "token"、boost_first_chunk 默认即 True，各采样参数官方
      流式示例均未传，全部沿用库默认。
    """
    if len(spk_paths) == 1:
        spk_audio_path = spk_paths[0]
    else:
        spk_audio_path = {path: 1.0 for path in spk_paths}
    return {
        "spk_audio_path": spk_audio_path,
        "prompt_audio_path": prompt_audio_path,
        "prompt_audio_text": prompt_audio_text,
        "text": text,
        "stream_chunk": int(stream_chunk),
        "overlap_len": int(overlap_len),
        "debug": False,
    }


def _pcm_to_base64(audio_data) -> str:
    """把 float32 波形转成 int16 单声道 PCM 的 base64 字符串。"""
    pcm = np.asarray(audio_data).reshape(-1).astype(np.float32)
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    return base64.b64encode(pcm16).decode("ascii")


async def _event_stream(params: dict) -> AsyncGenerator[str, None]:
    """按官方推荐的异步流式接口逐段下发 SSE 事件。

    `infer_stream_async` 内部已通过实例内置 `_infer_lock` 串行化并复用
    线程池线程，服务层只负责把 AudioClip 转成 SSE 事件。
    """
    first = True
    async for clip in tts.infer_stream_async(**params):
        if first:
            meta = {"sample_rate": int(clip.samplerate), "origin_text": clip.orig_text}
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            first = False
        yield f"event: chunk\ndata: {_pcm_to_base64(clip.audio_data)}\n\n"
    if first:
        # 官方包装器不会把子线程异常回传给协程（异常只留在 Future 里，最终打印于
        # stderr）；一个片段都没产出说明推理失败，这里显式下发 error 而非静默 done
        message = "合成失败：未产出任何音频片段，详见服务端日志"
        logger.error(message)
        yield f"event: error\ndata: {json.dumps({'message': message}, ensure_ascii=False)}\n\n"
        return
    yield "event: done\ndata:\n\n"


async def _warmup() -> None:
    """官方推荐预热：参考音频缓存 + 一次完整流式推理。

    必须在服务事件循环内进行：`infer_stream_async` 使用当前事件循环的默认
    线程池执行推理，这里跑热的线程正是后续请求复用的推理线程，可避免首个
    真实请求承担冷线程与 CUDA Graph 首次捕获的开销。
    """
    t0 = time.perf_counter()
    voice_config = load_voice_config()
    prompt_path = str(voice_config.get("spk_audio") or "")
    prompt_text = str(voice_config.get("spk_audio_text") or "")
    spk_extra = [str(p) for p in voice_config.get("spk_audio_additional") or [] if str(p)]
    if not prompt_path or not prompt_text:
        # 参考音频缺失时预热无法对齐真实链路，直接报错暴露，不留到首个请求才发现
        raise RuntimeError(
            f"预热需要参考音频：请在 {_LIVE_CONFIG_PATH} 的 [voice] 中配置 spk_audio / spk_audio_text"
        )

    # 官方推荐：cache_spk_audio / cache_prompt_audio 预提取参考音频缓存
    spk_paths = [prompt_path] + spk_extra
    tts.cache_spk_audio(*spk_paths)
    tts.cache_prompt_audio(prompt_audio_paths=prompt_path, prompt_audio_texts=prompt_text)

    # 用与真实请求一致的参数跑一次完整流式推理：捕获 CUDA Graph、复热推理线程
    params = compose_infer_params(
        text=_WARMUP_TEXT,
        prompt_audio_path=prompt_path,
        prompt_audio_text=prompt_text,
        spk_paths=spk_paths,
        stream_chunk=int(voice_config.get("stream_chunk") or 25),
        overlap_len=int(voice_config.get("overlap_len") or 5),
    )
    chunks = 0
    async for _clip in tts.infer_stream_async(**params):
        chunks += 1
    logger.info(
        "预热完成：音色参考 %d 个，推理产出 %d 段音频，耗时 %.1f s",
        len(spk_paths),
        chunks,
        time.perf_counter() - t0,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """对齐官方 fastapi_server_example.py 的 startup_event：启动即构造并加载 TTS，
    --preload 时再执行参考音频缓存与完整推理预热。"""
    global tts
    voice_config = load_voice_config()
    stream_chunk = int(voice_config.get("stream_chunk") or 25)
    overlap_len = int(voice_config.get("overlap_len") or 5)
    logger.info("正在加载 TTS 模型...")
    # 官方流式示例：use_bert=True 提升中文语义理解；models_dir 显式指定模型目录
    # （官方 server 做法）；sovits_cache 按官方注释公式从流式分块参数推导
    tts = TTS(
        use_bert=True,
        models_dir=str(DEFAULT_MODELS_DIR),
        sovits_cache=[stream_chunk * 2, stream_chunk * 2 + overlap_len],
    )
    # 官方预加载 API：启动阶段加载 GPT/SoVITS 模型，避免首个请求承担加载延迟
    tts.load_gpt_model()
    tts.load_sovits_model()
    logger.info("TTS 模型加载完成")
    if _warmup_enabled:
        await _warmup()
    yield


app = FastAPI(title="GSV-TTS-Lite 流式合成服务", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """存活探针。"""
    return {"status": "ok"}


@app.get("/tts/stream")
async def tts_stream(
    text: str = Query(..., min_length=1, description="要合成的文本"),
    spk_audio: str = Query("", description="主参考音频路径（兼作音色与风格参考）"),
    spk_audio_text: str = Query("", description="主参考音频对应文本"),
    spk_audio_extra: list[str] = Query(default_factory=list, description="追加音色参考音频路径（可多个，无需文本）"),
    stream_chunk: int = Query(25, ge=1, description="流式分块大小"),
    overlap_len: int = Query(5, ge=0, description="流式重叠长度"),
) -> StreamingResponse:
    """把文本流式合成为语音，以 SSE 逐 chunk 返回 PCM 音频。"""
    # 参考音频：请求未传时回退直播语音配置（与客户端真实链路同源），仍缺失则明确报错
    voice_config = load_voice_config()
    prompt_path = spk_audio or str(voice_config.get("spk_audio") or "")
    prompt_text = spk_audio_text or str(voice_config.get("spk_audio_text") or "")
    # 音色参考（spk）：主参考 + 追加参考
    spk_paths = ([spk_audio] if spk_audio else []) + [p for p in spk_audio_extra if p]
    if not spk_paths:
        spk_paths = [prompt_path] + [str(p) for p in voice_config.get("spk_audio_additional") or [] if str(p)]
    if not prompt_path or not prompt_text:
        raise HTTPException(
            status_code=400,
            detail="缺少参考音频：请传 spk_audio / spk_audio_text，或在 config/bilibili_live.toml 的 [voice] 中配置",
        )
    params = compose_infer_params(
        text=text,
        prompt_audio_path=prompt_path,
        prompt_audio_text=prompt_text,
        spk_paths=spk_paths,
        stream_chunk=stream_chunk,
        overlap_len=overlap_len,
    )
    logger.info("收到流式合成请求，文本=%r，音色参考数=%d", text[:40], len(spk_paths))
    return StreamingResponse(
        _event_stream(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    global _warmup_enabled
    parser = argparse.ArgumentParser(description="GSV-TTS-Lite 流式合成服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--preload",
        action="store_true",
        help="启动时按官方推荐预热：加载模型、缓存参考音频并跑一次完整推理（捕获 CUDA Graph、复热推理线程）",
    )
    args = parser.parse_args()
    _warmup_enabled = args.preload

    import uvicorn

    logger.info("GSV-TTS-Lite 流式合成服务启动 http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
