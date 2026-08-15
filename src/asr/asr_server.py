"""本地 ASR HTTP 服务（独立进程）：加载 Qwen3-ASR（CUDA Graph 静态解码加速），
通过 8487 端口供主程序 STT 转写，主进程不再占 ~3GB 显存加载模型。

接口：
- GET  /health      → {"status": "ok"}（模型就绪）
- POST /transcribe  → 请求体 {"path": "<wav 绝对路径>"} → {"text": "..."}
  同一台机器共享文件系统，客户端传临时 wav 路径即可（无需 multipart 上传）。

启动：根目录 asr.bat（或 python src/asr/asr_server.py）。
主程序 .env 配置 STT_ENGINE=local + STT_SERVER_URL=http://127.0.0.1:8487。
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8487
# 模型目录默认与主程序 .env 的 STT_LOCAL_MODEL_PATH 一致（src/asr/qwen3_asr）
MODEL_PATH = os.environ.get("STT_LOCAL_MODEL_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "qwen3_asr")

# CUDA Graph 静态解码参数：固定 KV 缓存长度需覆盖 提示词 token 数 + 最大生成数
_GRAPH_CACHE_LEN = 1024
# Qwen3 tokenizer 结束符（</s> 与 <im_end>），与 eager 生成一致的停止条件
_GRAPH_EOS_IDS = {151645, 151643}


class _LocalASRGraph:
    """本地 ASR 的 CUDA Graph 静态解码器（进程内单例）。

    Qwen3-ASR 的逐 token 解码步形状固定（input_ids(1,1) + StaticCache 定长
    KV），可捕获为 CUDA Graph 回放，消除逐 kernel 启动开销（实测整段解码约
    240ms，eager 约 1.3s）。prefill 输入长度随语音变化，保持 eager。

    缓存复用语义：每段语音 prefill 用 index_copy_ 覆盖缓存 0..L-1 位置，
    图内解码从 L 起继续写；因果注意力保证旧段残留数据（位置 ≥ 当前写点）
    永远不会被读到。HTTP 服务每请求一个线程，回放同一张图必须互斥。
    """

    def __init__(self, asr) -> None:
        self.asr = asr
        self.thinker = asr.model.thinker
        self.processor = asr.processor
        self.device = asr.device
        self.dtype = asr.dtype
        self._cache = None
        self._graph = None
        self._s_input_ids = None
        self._s_cp = None
        self._s_out = None
        self._lock = threading.Lock()

    def transcribe(self, wav_path: str):
        """转写单个音频，返回与 asr.transcribe 一致的 ASRTranscription 列表。

        图构建或回放失败时回退 eager asr.transcribe，识别可用性不受影响。
        """
        import torch

        try:
            with self._lock:
                with torch.no_grad():
                    return self._transcribe_graph(wav_path)
        except Exception as e:
            print(f"[ASR] CUDA Graph 转写失败，回退 eager：{e}", flush=True)
            return self.asr.transcribe(wav_path)

    def _transcribe_graph(self, wav_path: str):
        """镜像 qwen_asr.transcribe 的单音频流程：prefill + 图解码 + 解析。"""
        from qwen_asr.inference.qwen3_asr import ASRTranscription
        from qwen_asr.inference.utils import (
            MAX_ASR_INPUT_SECONDS,
            SAMPLE_RATE,
            merge_languages,
            normalize_audios,
            parse_asr_output,
            split_audio_into_chunks,
        )

        wav = normalize_audios(wav_path)[0]
        chunks = split_audio_into_chunks(
            wav=wav, sr=SAMPLE_RATE, max_chunk_sec=MAX_ASR_INPUT_SECONDS)
        langs, texts = [], []
        for cwav, _offset in chunks:
            raw = (self._decode_first(cwav) if self._graph is None
                   else self._decode_chunk(cwav))
            lang, txt = parse_asr_output(raw, user_language=None)
            langs.append(lang)
            texts.append(txt)
        return [ASRTranscription(
            language=merge_languages(langs), text="".join(texts))]

    def _prefill(self, wav):
        """与 _infer_asr_transformers 一致的输入对齐 + eager prefill。

        返回 (提示词长度 L, 首个决策 token)，同时完成本段语音的 KV 预填充。
        输入超长抛异常，由 transcribe 统一回退 eager。
        """
        import torch
        from transformers import StaticCache

        text = [self.asr._build_text_prompt(context="", force_language=None)]
        inputs = self.processor(text=text, audio=[wav], return_tensors="pt",
                                padding=True)
        inputs = inputs.to(self.device).to(self.dtype)

        input_ids = inputs["input_ids"].to(torch.long)
        input_features = inputs["input_features"].to(self.dtype)
        feature_attention_mask = inputs["feature_attention_mask"].to(torch.long)
        attention_mask = inputs["attention_mask"].to(self.dtype)
        L = input_ids.shape[1]
        if L + self.asr.max_new_tokens > _GRAPH_CACHE_LEN:
            raise RuntimeError(
                f"提示词过长(L={L})，超出固定缓存长度({_GRAPH_CACHE_LEN})，回退 eager")

        if self._cache is None:
            self._cache = StaticCache(
                config=self.thinker.config, max_cache_len=_GRAPH_CACHE_LEN,
                device=self.device, dtype=self.dtype)
        out = self.thinker(
            input_ids=input_ids, input_features=input_features,
            feature_attention_mask=feature_attention_mask,
            attention_mask=attention_mask,
            past_key_values=self._cache, use_cache=True,
            cache_position=torch.arange(L, device=self.device),
            position_ids=None,
        )
        return L, out.logits[:, -1, :].argmax(-1, keepdim=True)

    def _decode_first(self, wav):
        """首段语音：eager prefill 后捕获解码步 CUDA Graph，并完成本段解码。"""
        import torch

        L, next_id = self._prefill(wav)

        s_input_ids = torch.zeros(1, 1, dtype=torch.long, device=self.device)
        s_cp = torch.zeros(1, dtype=torch.long, device=self.device)
        s_out = torch.zeros(1, 1, dtype=torch.long, device=self.device)
        s_input_ids.copy_(next_id)
        s_cp.fill_(L)

        # 侧流预热 3 步：图捕获与首轮回放不包含 CUDA 初始化开销
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            for _ in range(3):
                out = self.thinker(
                    input_ids=s_input_ids, attention_mask=None,
                    past_key_values=self._cache, use_cache=True,
                    cache_position=s_cp, position_ids=None,
                )
                s_out.copy_(out.logits[:, -1, :].argmax(-1, keepdim=True))
        torch.cuda.current_stream().wait_stream(stream)

        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            out = self.thinker(
                input_ids=s_input_ids, attention_mask=None,
                past_key_values=self._cache, use_cache=True,
                cache_position=s_cp, position_ids=None,
            )
            s_out.copy_(out.logits[:, -1, :].argmax(-1, keepdim=True))

        self._graph = graph
        self._s_input_ids, self._s_cp, self._s_out = s_input_ids, s_cp, s_out
        return self._decode_loop(L, next_id)

    def _decode_chunk(self, wav):
        """后续段语音：复用已捕获的解码图（prefill 覆盖缓存 0..L-1 位置）。"""
        L, next_id = self._prefill(wav)
        return self._decode_loop(L, next_id)

    def _decode_loop(self, L, next_id):
        """贪心解码：回放图逐 token 生成，直至 EOS 或生成上限。"""
        import torch

        gen_ids = []
        for k in range(self.asr.max_new_tokens):
            tid = int(next_id[0, 0])
            if tid in _GRAPH_EOS_IDS:
                break
            gen_ids.append(tid)
            self._s_input_ids.copy_(next_id)
            self._s_cp.fill_(L + k)
            self._graph.replay()
            next_id.copy_(self._s_out)
        return self.processor.batch_decode(
            [torch.tensor(gen_ids, device=self.device)],
            skip_special_tokens=True, clean_up_tokenization_spaces=False,
        )[0]


def _load_asr():
    """加载 Qwen3-ASR 模型（CUDA 用 bf16，CPU 用 fp32）。"""
    import torch
    from qwen_asr import Qwen3ASRModel

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    return Qwen3ASRModel.from_pretrained(
        MODEL_PATH,
        dtype=(torch.bfloat16 if device.startswith("cuda") else torch.float32),
        device_map=device,
        local_files_only=True,
    )


class _TranscribeHandler(BaseHTTPRequestHandler):
    """单次请求处理：/health 探活，/transcribe 转写（runner 由 main 注入）。"""

    runner = None

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/transcribe":
            return self._send_json(404, {"detail": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            wav_path = str(body.get("path") or "")
        except Exception:
            return self._send_json(
                400, {"detail": "请求体必须是 {\"path\": <wav路径>} JSON"})
        if not wav_path or not os.path.isfile(wav_path):
            return self._send_json(
                400, {"detail": f"音频文件不存在: {wav_path}"})
        try:
            results = self.runner.transcribe(wav_path)
            text = (str(getattr(results[0], "text", "") or "").strip()
                    if results else "")
            return self._send_json(200, {"text": text})
        except Exception as e:
            print(f"[ASR] 转写失败: {e}", flush=True)
            return self._send_json(500, {"detail": str(e)})

    def _send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    print(f"[ASR] 模型目录: {MODEL_PATH}", flush=True)
    asr = _load_asr()
    print(f"[ASR] 模型加载完成（{asr.device}）", flush=True)
    _TranscribeHandler.runner = _LocalASRGraph(asr)
    httpd = ThreadingHTTPServer((HOST, PORT), _TranscribeHandler)
    print(f"[ASR] 服务已启动: http://{HOST}:{PORT}（首次转写时构建 CUDA Graph）",
          flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
