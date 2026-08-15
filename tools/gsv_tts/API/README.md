# GSV-TTS FastAPI 服务端使用说明

项目通过 `tts.bat` 启动本服务（`API/fastapi_server_example.py`），主程序
`src/tts/engine.py` 作为客户端调用以下接口。

## 1. 启动服务

```bash
cd API
pip install -r requirements.txt
python fastapi_server_example.py
```

- 监听 `0.0.0.0:8000`，启动时自动加载 TTS 模型并预热合成管线；
- 浏览器打开 `http://localhost:8000/docs` 查看交互式 API 文档；
- 模型目录：`API/models/`；合成音频暂存 `output/`，客户端下载后即删、启动时清空。

## 2. 接口说明

### 单个 TTS 请求：`POST /tts/single`

```json
{
  "text": "你好，这是测试。",
  "speaker_audio": "C:/path/ref.wav",
  "prompt_audio": "C:/path/ref.wav",
  "prompt_text": "参考音频对应的文本",
  "top_k": 5,
  "top_p": 0.9,
  "temperature": 1.0,
  "repetition_penalty": 1.35,
  "noise_scale": 0.5,
  "speed": 1.0
}
```

- `speaker_audio` / `prompt_audio` 支持本地路径或 HTTP/HTTPS URL（外链自动下载）；
- `prompt_text` 可选：缺省时自动用 ASR 识别 `prompt_audio`。

响应：`{"success": true, "audio_len": 1.72, "filename": "tts_xxx.wav", "prompt_text_used": "..."}`

### 批量 TTS 请求：`POST /tts/batch`

```json
{
  "texts": ["第一个请求", "第二个请求"],
  "speaker_audio": "C:/path/ref.wav",
  "prompt_audio": "C:/path/ref.wav",
  "prompt_text": "参考音频对应的文本",
  "cut_minlen": 10,
  "cut_mute": 0.3,
  "cut_mute_scale_map": {"。": 1.5, "、": 0.8}
}
```

批量请求可真正利用 GPU 并行，适合一次合成多条文本。
响应：`{"success": true, "count": 2, "filenames": ["tts_a.wav", "tts_b.wav"], "prompt_text_used": "..."}`

### 下载音频：`GET /audio/{filename}`

如 `http://localhost:8000/audio/tts_06a1a5fc.wav`。下载完成后服务端自动删除该文件。

## 3. ASR 与环境变量

- `USE_ASR=true`（默认）：启动时加载 ASR 模型（Qwen3-ASR），`prompt_text` 缺省时可自动识别；
- `USE_ASR=false`：禁用 ASR，请求必须提供 `prompt_text`。

## 4. 常见问题

- **端口被占用**：修改 `fastapi_server_example.py` 末尾 `uvicorn.run(app, host="0.0.0.0", port=8001)`；
- **ASR 识别不准确**：手动提供 `prompt_text`，并保证 `prompt_audio` 音质清晰；
- **外链音频下载失败**：检查 URL 是否可访问（`httpx` 默认超时 60 秒）。
