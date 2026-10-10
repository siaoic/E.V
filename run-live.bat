@echo off
chcp 65001 >nul
setlocal

set "PROJ_DIR=E:\AI\直播"
set "TTS_DIR=E:\AI\Qwen3-TTS-GGUF"
set "ROOM=%~1"
if "%ROOM%"=="" set "ROOM=22748536"

if "%LLM_API_KEY%"=="" set /p "LLM_API_KEY=LLM_API_KEY: "

set "LLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4"
set "LLM_MODEL=glm-5.3-flash"
set "LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}"
set "TTS_URL=http://127.0.0.1:8756"
set "TTS_MODE=qwen"
set "EMBEDDING_URL=http://127.0.0.1:8761"
set "ROOM_ID=%ROOM%"
if "%LLM_TOPICS%"=="" set "LLM_TOPICS=最近学到的一件小事,小时候最怕的东西,如果中了五百万怎么花,第一次下厨的灾难,记忆里最热的一天,一个人的时候会做什么,最近一次熬夜是为了什么,养过或想养的宠物"

curl -s -m 3 %TTS_URL%/health >nul 2>&1
if errorlevel 1 (
  echo Starting TTS server in a new window...
  start "TTS server" cmd /k "chcp 65001 >nul & cd /d "%TTS_DIR%" & set "PYTHONUTF8=1" & set "TTS_ONNX_PROVIDER=CUDA" & set "TTS_VOICE_JSON=voices/灰原哀.json" & .venv\Scripts\python.exe server.py"
  echo Waiting 7 seconds for TTS to load...
  timeout /t 7 /nobreak >nul
) else (
  echo TTS server already running.
)

curl -s -m 3 %EMBEDDING_URL%/health >nul 2>&1
if errorlevel 1 (
  echo Starting embedding server in a new window...
  start "Embedding server" cmd /k "chcp 65001 >nul & D:\llama.cpp\b11534\llama-server.exe -m D:\llama.cpp\model\embeddinggemma-2-UD-Q4_K_XL.gguf --embedding -ngl 0 -c 2048 --pooling mean --port 8761"
  echo Waiting 7 seconds for embedding model to load...
  timeout /t 7 /nobreak >nul
) else (
  echo Embedding server already running.
)

cd /d "%PROJ_DIR%"
echo Starting live harness for room %ROOM% (plugins: %PROJ_DIR%\plugins). Press Ctrl+C to stop.
node src\main.ts
echo.
echo Live harness exited.
pause
