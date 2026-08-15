@echo off
rem E.V control center launcher
rem 先切到自身目录；若此处没有 runtime，再上跳一级回到项目根。
cd /d "%~dp0"
if not exist "runtime\Scripts\activate.bat" cd /d "%~dp0.."
set "PATH=%CD%\runtime;%PATH%"
call runtime\Scripts\activate.bat
python tools\gsv_tts\API\fastapi_server_example.py
pause