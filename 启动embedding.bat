@echo off
cd /d "%~dp0"
rem 全量 GPU 卸载(-ngl 99) + 上下文提到 2048(-c) + 批处理尺寸对齐(-ub)，嵌入延迟 430ms→~20ms
D:\llama.cpp\llama-server.exe -m D:\llama.cpp\model\Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings -ngl 99 -c 2048 -ub 2048 --port 8081
pause
