# 记忆检索用 llama.cpp 本地嵌入

Status: 已接受（2026-10-10 换用 embeddinggemma-2，构建升级到 b11534）

长期记忆的语义检索用本地 llama.cpp 服务承担：`D:\llama.cpp\b11534\llama-server.exe` 加载 `model/embeddinggemma-2-UD-Q4_K_XL.gguf`，以 `--embedding -ngl 0 -c 2048 --pooling mean --port 8761` 启动（768 维；EmbeddingGemma 用 mean pooling；旧构建 b11065 不认识 `gemma-embedding2` 架构，必须用 b11534+；模型元数据声明超大上下文，必须显式 `-c 2048`；GPU 显存被 TTS 占满，CPU 推理短文本毫秒级）。提供 OpenAI 兼容的 `/v1/embeddings`。

放弃的方案：A_memorix 的嵌入管线——`SDKMemoryKernel` 硬耦合 MaiBot 宿主（缺 `src/config`、`src/llm_models` 等模块）且需要宿主侧 embedding API，当前不可接；朴素的 bigram-only 检索——中文同义表述召回太差；Qwen3-VL-Embedding-2B——已被 embeddinggemma-2 取代（前者 last-token pooling、2048 维，且同样不支持图片输入）。

代价：嵌入服务是第二个常驻进程（TTS 之外），`run-live.bat` 会自动拉起；每次 `memory_save`/`memory_recall` 多一次 7 秒上限的本地推理请求，失败静默回退 bigram，不阻塞发言链。换嵌入模型后向量维度不同（2048→768），检索按维度一致才比较，旧向量自动回退 bigram，不会崩。

**图片向量化在 llama.cpp 不可行（实测 b11534）**：`/v1/embeddings` 是纯文本端点，请求里的 `image` 字段被静默忽略（不同图片、甚至不带图片，向量完全相同）；mmproj 只服务于对话生成，对嵌入无效。因此带图记忆的向量用**图片说明文字**生成（记录带 `image` 路径字段，检索结果以 `[图] 路径 — 说明` 呈现）。这与 MaiBot 的分层一致——它把 VLM 描述作为观察文本存档，图片另有独立图像嵌入池（OpenAI 兼容的图像嵌入服务）；若将来要真正的图→向量，需要另接一个图像嵌入服务（如 ONNX CLIP/SigLIP），llama.cpp 这条路不通。
