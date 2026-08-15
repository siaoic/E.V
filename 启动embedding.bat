@echo off
cd /d "%~dp0"
D:\llama.cpp\llama-server.exe -m D:\llama.cpp\model\Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings -c 512 --port 8081
pause
