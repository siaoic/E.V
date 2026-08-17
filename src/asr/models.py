"""FunASR 本地模型管理与自动下载（ModelScope）。

模型统一存放在项目内 `src/asr/models` 目录（不依赖 %USERPROFILE% 缓存）：
- fsmn-vad：语音活动检测（stt.py 主进程内流式 VAD）
- paraformer-zh-streaming：流式转写（asr_server.py 独立进程加载）

首次运行模型缺失时自动从 ModelScope 下载，已存在则直接使用本地快照
（离线可用，不重复下载）。
"""

import os
from pathlib import Path

from src.utils import console

# 模型根目录：src/asr/models（与源码同仓，随项目走）
MODELS_DIR = Path(__file__).resolve().parent / "models"

# ModelScope 模型 ID 与默认 revision（与 funasr 官方演示一致）
VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
ASR_MODEL_ID = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
MODEL_REVISION = "v2.0.4"


def _snapshot_candidates(model_id: str, revision: str):
    """模型快照目录候选（兼容不同下载布局，命中任一即视为已下载）。

    1) 规范化布局：<MODELS_DIR>/<owner>/<name>/snapshots/<revision>
       （本项目整理的本地目录，物理迁移/手工放置用）
    2) modelscope 写布局：<MODELS_DIR>/models/<owner>--<name>/snapshots/<revision>
       （snapshot_download(cache_dir=...) 的落盘结构）
    """
    owner, _, name = model_id.partition("/")
    return (
        MODELS_DIR / owner / name / "snapshots" / revision,
        MODELS_DIR / "models" / f"{owner}--{name}" / "snapshots" / revision,
    )


def ensure_model(model_id: str, revision: str) -> Path:
    """返回模型快照目录，缺失时自动从 ModelScope 下载。

    存在性判定以「快照目录非空」为准（不同模型文件清单不同，
    统一用目录内容判断，避免个别模型缺 config.yaml 被误判未下载）。
    """
    for candidate in _snapshot_candidates(model_id, revision):
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate
    from modelscope import snapshot_download

    label = f"{model_id}（revision {revision}）"
    console.info(f"[ASR] 模型 {label} 缺失，正在从 ModelScope 下载…")
    try:
        path = snapshot_download(
            model_id, revision=revision, cache_dir=str(MODELS_DIR))
    except Exception as e:
        console.error(
            f"[ASR] 模型 {label} 下载失败：{e}。请检查网络后重试"
            f"（模型将保存在 {MODELS_DIR}）。")
        raise
    console.info(f"[ASR] 模型 {label} 下载完成")
    return Path(path)


def ensure_vad_model() -> Path:
    """语音活动检测模型（fsmn-vad）本地目录。"""
    return ensure_model(VAD_MODEL_ID, MODEL_REVISION)


def ensure_asr_model() -> Path:
    """流式转写模型（paraformer-zh-streaming）本地目录。

    .env 的 STT_LOCAL_MODEL_PATH 显式指定本地目录时优先（兼容旧配置）；
    留空则使用 src/asr/models 自动下载。
    """
    override = os.environ.get("STT_LOCAL_MODEL_PATH") or ""
    if override:
        return Path(override)
    revision = os.environ.get("STT_LOCAL_MODEL_REVISION") or MODEL_REVISION
    return ensure_model(ASR_MODEL_ID, revision)


def ensure_stt_models() -> None:
    """确保 STT 全链路模型就绪（fsmn-vad + paraformer 流式转写）。

    供显式下载入口（下载stt模型.bat / asr_server.py --download-only）一次
    性下载两个模型；任一缺失自动从 ModelScope 下载，已存在则直接跳过。
    """
    ensure_vad_model()
    ensure_asr_model()
    console.info("[ASR] STT 模型全部就绪")
