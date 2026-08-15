@echo off
rem 本地嵌入服务（llama-server）：情绪分类器 / 记忆检索共用 127.0.0.1:8081
rem 依赖：llama.cpp 已安装（llama-server.exe 在 D:\llama.cpp）
rem 端口 8081 与 .env 的 EMBEDDING_BASE_URL 保持一致
cd /d "%~dp0"
D:\llama.cpp\llama-server.exe -m D:\llama.cpp\model\Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings -c 512 --port 8081
pause
