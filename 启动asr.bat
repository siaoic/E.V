@echo off
rem 本地 ASR 服务（qwen3_asr + CUDA Graph 加速）
rem 主程序 STT 通过 127.0.0.1:8487 转写，请先启动本脚本再运行主程序
rem 依赖：runtime 环境的 torch + qwen_asr（模型在 src/asr/qwen3_asr）
cd /d "%~dp0"
if not exist "runtime\Scripts\activate.bat" cd /d "%~dp0.."
set "PATH=%CD%\runtime;%PATH%"
call runtime\Scripts\activate.bat
python src\asr\asr_server.py
pause
