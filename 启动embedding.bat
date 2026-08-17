@echo off
D:\llama.cpp\llama-server.exe -m D:\llama.cpp\model\Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings -ngl 99 -c 1024 -ub 1024 --port 8081
pause
