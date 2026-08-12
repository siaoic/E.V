@echo off
cd /d D:\llama.cpp
D:\llama.cpp\llama-server.exe -m D:\llama.cpp\model\Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings --port 8081 -c 512
pause
