@echo off
chcp 65001 >nul
setlocal

REM ============================================================================
REM This file must stay 7-bit ASCII. cmd.exe decodes a batch file with the console
REM codepage, and any byte above 0x7F shifts its read position, which eats the head
REM of following lines and silently breaks if/errorlevel/start (a literal <repo>
REM even turned into a redirection). Paths with non-ASCII characters therefore come
REM from %~dp0 or from .env, never from this file.
REM ============================================================================

REM App config (LLM endpoint/model/key, ROOM_ID, SESSDATA, TTS + embedding URLs,
REM topic pool, TTS voice file) lives in <repo>\.env and is loaded by
REM node src\main.ts at startup (see src/loader/env-file.ts). Environment variables
REM set here win over .env, so no app config is duplicated below.
set "PROJ_DIR=%~dp0"
set "TTS_DIR=E:\AI\Qwen3-TTS-GGUF"

REM Optional first argument overrides the room; otherwise .env decides.
if not "%~1"=="" set "ROOM_ID=%~1"
if "%LLM_API_KEY%"=="" set /p "LLM_API_KEY=LLM_API_KEY [already in .env - press Enter to use .env]: "

REM Health-check URLs are only for this script's curl calls; app-side URLs come from .env.
set "HEALTH_TTS_URL=http://127.0.0.1:8756"
set "HEALTH_EMBEDDING_URL=http://127.0.0.1:8761"

REM The voice file name contains non-ASCII characters, so it is read from .env and
REM inherited by the TTS server process instead of being written into this file.
for /f "usebackq tokens=1,* delims==" %%a in ("%PROJ_DIR%.env") do if /i "%%a"=="TTS_VOICE_JSON" set "TTS_VOICE_JSON=%%b"
if not defined TTS_VOICE_JSON echo WARN: TTS_VOICE_JSON not set in .env - the TTS server will use its own default voice.

curl -s -m 3 %HEALTH_TTS_URL%/health >nul 2>&1
if errorlevel 1 (
  echo Starting TTS server in a new window...
  start "TTS server" cmd /k "chcp 65001 >nul & cd /d "%TTS_DIR%" & set "PYTHONUTF8=1" & set "TTS_ONNX_PROVIDER=CUDA" & .venv\Scripts\python.exe server.py"
  echo Waiting 7 seconds for TTS to load...
  timeout /t 7 /nobreak >nul
) else (
  echo TTS server already running.
)

curl -s -m 3 %HEALTH_EMBEDDING_URL%/health >nul 2>&1
if errorlevel 1 (
  echo Starting embedding server in a new window...
  start "Embedding server" cmd /k "chcp 65001 >nul & D:\llama.cpp\b11534\llama-server.exe -m D:\llama.cpp\model\embeddinggemma-2-UD-Q4_K_XL.gguf --embedding -ngl 0 -c 2048 --pooling mean --port 8761"
  echo Waiting 7 seconds for embedding model to load...
  timeout /t 7 /nobreak >nul
) else (
  echo Embedding server already running.
)

cd /d "%PROJ_DIR%"
if defined ROOM_ID (
  echo Starting live harness for room %ROOM_ID% from the command line argument. Press Ctrl+C to stop.
) else (
  echo Starting live harness; room, SESSDATA, model and topics come from .env. Press Ctrl+C to stop.
)
node src\main.ts
echo.
echo Live harness exited.
pause
