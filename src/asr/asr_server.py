"""本地 ASR 流式 HTTP 服务（独立进程）：加载 FunASR paraformer-zh-streaming
流式模型，通过 8487 端口供主程序 STT 实时转写，主进程不占显存加载模型。

与旧 Qwen3-ASR 整段转写不同：流式模型按 600ms chunk 增量推理，客户端在
说话过程中持续 feed 音频块，服务端维护 cache 并返回 partial 文本；VAD
判定说话结束后提交 final，服务端立即返回完整文本——推理已在说话过程中
完成，省去"等整句话说完再推理"的延迟。

接口：
- GET  /health              → {"status": "ok"}
- POST /stream/start        → {"session_id": "..."}（创建识别会话）
- POST /stream/feed         → {"session_id", "audio": "<base64 float32 16k>"}
                             → {"text": "partial 文本（可能为空）"}
- POST /stream/end          → {"session_id", "audio": "<可选残余块>"}
                             → {"text": "最终完整文本"}
- POST /transcribe          → 一次性转写 {"path": "<wav>"}（流式循环兜底/兼容旧接口）

启动：根目录 启动asr.bat（或 python src/asr/asr_server.py）。
主程序 .env 配置 STT_ENGINE=local + STT_SERVER_URL=http://127.0.0.1:8487。
"""

import base64
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

# 项目根目录入 sys.path：独立进程运行时脚本目录（src/asr）在 sys.path[0]，
# 无法直接 import src.*，按文件位置向上定位到项目根（.bat 已 cd 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# 读取 .env（模型路径等配置与主进程保持一致）
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from src.asr import models

HOST = "127.0.0.1"
PORT = 8487
SAMPLE_RATE = 16000
# 模型：.env STT_LOCAL_MODEL_PATH 显式指定时优先；否则使用 src/asr/models
# 下的本地快照（缺失时自动从 ModelScope 下载，随项目走，不依赖用户缓存）
MODEL_NAME = os.environ.get("STT_LOCAL_MODEL_PATH") or str(models.ensure_asr_model())
MODEL_REVISION = os.environ.get("STT_LOCAL_MODEL_REVISION") or models.MODEL_REVISION

# paraformer-zh-streaming 流式推理参数（与官方 demo 变量名逐字一致）
chunk_size = [0, 10, 5]                  # [0,10,5] = 600ms 块粒度
encoder_chunk_look_back = 4              # encoder self-attention 回看块数
decoder_chunk_look_back = 1              # decoder cross-attention 回看 encoder 块数
chunk_stride = chunk_size[1] * 960       # 600ms = 9600 样本（与 demo 一致）


def _resample_to_16k(speech: np.ndarray, sr: int) -> np.ndarray:
    """线性重采样到 16kHz（/transcribe 兜底接口用；流式 feed 的音频已是 16k）。"""
    if sr == SAMPLE_RATE:
        return speech
    n = max(1, int(round(len(speech) * SAMPLE_RATE / sr)))
    return np.interp(np.linspace(0, len(speech) - 1, n),
                     np.arange(len(speech)), speech).astype(np.float32)


class _StreamingASR:
    """FunASR paraformer-zh-streaming 流式识别器（进程内单例）。"""

    def __init__(self) -> None:
        import torch
        from funasr import AutoModel

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.model = AutoModel(
            model=MODEL_NAME, model_revision=MODEL_REVISION, device=device,
            disable_update=True, disable_pbar=True)
        self._lock = threading.Lock()
        self._sessions: "dict[str, dict]" = {}

    # ---------- 流式会话 ----------

    def create_session(self) -> str:
        """创建识别会话（客户端串行 feed/end），返回 session_id。"""
        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = {}
        return sid

    def feed(self, session_id: str, audio: np.ndarray) -> str:
        """增量推理一个 600ms 块，返回 partial 文本（可能为空）。"""
        with self._lock:
            cache = self._sessions.get(session_id)
            if cache is None:
                return ""
            return self._infer(audio, cache, is_final=False)

    def finish(self, session_id: str, audio) -> str:
        """收尾推理（is_final=True，带可选残余块），返回完整文本并销毁会话。"""
        with self._lock:
            cache = self._sessions.pop(session_id, None)
            if cache is None:
                return ""
            # 无残余块时（末块恰好整 600ms 已 feed），完整文本已由客户端
            # 累积 partial 拼出，无需再推理
            if audio is None or len(audio) == 0:
                return ""
            return self._infer(audio, cache, is_final=True)

    def _infer(self, audio, cache: dict, is_final: bool) -> str:
        res = self.model.generate(
            input=audio, cache=cache, is_final=is_final,
            chunk_size=chunk_size,
            encoder_chunk_look_back=encoder_chunk_look_back,
            decoder_chunk_look_back=decoder_chunk_look_back)
        return (str(res[0].get("text") or "").strip()
                if res and isinstance(res[0], dict) else "")

    # ---------- 一次性转写（/transcribe 兜底） ----------

    def transcribe_file(self, wav_path: str) -> str:
        import soundfile as sf

        speech, sr = sf.read(wav_path, dtype="float32")
        speech = _resample_to_16k(speech, sr)
        cache = {}
        texts = []
        total = int((len(speech) - 1) / chunk_stride) + 1
        for i in range(total):
            chunk = speech[i * chunk_stride:(i + 1) * chunk_stride]
            is_final = i == total - 1
            texts.append(self._infer(chunk, cache, is_final))
        return "".join(t for t in texts if t).strip()


def _load_asr() -> _StreamingASR:
    """加载 FunASR 流式模型（首次运行自动从 ModelScope 下载）。"""
    print(f"[ASR] 模型: {MODEL_NAME}（revision {MODEL_REVISION}）", flush=True)
    return _StreamingASR()


class _AsrHandler(BaseHTTPRequestHandler):
    """请求处理：/health 探活，/stream/* 流式会话，/transcribe 一次性兜底。

    asr 实例由 main 注入（模块级），供 ThreadingHTTPServer 各请求线程访问。
    """

    asr = None

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path not in ("/stream/start", "/stream/feed", "/stream/end",
                        "/transcribe"):
            return self._send_json(404, {"detail": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send_json(400, {"detail": "请求体必须是 JSON"})
        try:
            if path == "/stream/start":
                return self._send_json(200, {"session_id": self.asr.create_session()})
            if path == "/stream/feed":
                session_id = str(body.get("session_id") or "")
                audio = self._decode_audio(body)
                return self._send_json(200, {"text": self.asr.feed(session_id, audio)})
            if path == "/stream/end":
                session_id = str(body.get("session_id") or "")
                audio = self._decode_audio(body)
                return self._send_json(200, {"text": self.asr.finish(session_id, audio)})
            # /transcribe
            wav_path = str(body.get("path") or "")
            if not wav_path or not os.path.isfile(wav_path):
                return self._send_json(400, {"detail": f"音频文件不存在: {wav_path}"})
            return self._send_json(200, {"text": self.asr.transcribe_file(wav_path)})
        except Exception as e:
            print(f"[ASR] 请求失败: {e}", flush=True)
            return self._send_json(500, {"detail": str(e)})

    @staticmethod
    def _decode_audio(body: dict) -> "np.ndarray | None":
        """解析 base64 float32 16k 音频（end 时可不带 audio）。"""
        b64 = body.get("audio")
        if not b64:
            return None
        return np.frombuffer(base64.b64decode(str(b64)), dtype=np.float32)

    def _send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    asr = _load_asr()
    print("[ASR] 模型加载完成", flush=True)
    _AsrHandler.asr = asr
    httpd = ThreadingHTTPServer((HOST, PORT), _AsrHandler)
    print(f"[ASR] 流式服务已启动: http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
