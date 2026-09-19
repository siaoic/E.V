# -*- coding: utf-8 -*-
"""A_memorix 记忆内网服务（embedding + FAISS 检索，单进程单组端点）。

对照迁移方案 §1.3.1：TS 侧经 HTTP 调用，JSON 序列化。
绑定 127.0.0.1，仅限本机访问。

用法：python services/a_memorix_server.py --port 8100 [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.A_memorix.core.embedding.manager import create_embedding_manager_from_config  # noqa: E402
from src.common.logger import get_logger  # noqa: E402

logger = get_logger("a_memorix_server")

app = FastAPI(title="A_memorix Kernel", docs_url=None, redoc_url=None)

# 全局状态（单 worker，启动时加载模型）
_model_loaded = False
_embedding_manager = None


class EmbedRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1, max_length=256)
    normalize: bool = True


class EmbedResponse(BaseModel):
    vectors: List[List[float]]


@app.get("/health")
def health():
    return {"status": "ready" if _model_loaded else "starting"}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    if not _model_loaded:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")
    try:
        vectors = _embedding_manager.encode_batch(req.texts, normalize=req.normalize)
        return {"vectors": [list(map(float, v)) for v in vectors]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/info")
def info():
    if not _model_loaded:
        return {"model_loaded": False}
    return {
        "model_loaded": True,
        "model_info": _embedding_manager.get_model_info(),
        "dimension": _embedding_manager.get_embedding_dimension(),
        "cache_hit_rate": _embedding_manager.cache_hit_rate,
    }


def main():
    parser = argparse.ArgumentParser(description="A_memorix 记忆内网服务")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-path", default="", help="sentence-transformers 模型路径")
    args = parser.parse_args()

    global _embedding_manager, _model_loaded
    try:
        from src.A_memorix.core.embedding.manager import EmbeddingManager

        _embedding_manager = create_embedding_manager_from_config(
            model_path=args.model_path or "",
        )
        _embedding_manager.load_model()
        _model_loaded = True
        logger.info("Embedding 模型加载完成")
    except Exception as exc:
        logger.error(f"Embedding 模型加载失败: {exc}", exc_info=True)
        # 保持 starting 状态启动，health 端点如实反映

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
