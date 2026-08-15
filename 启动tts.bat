@echo off
cd /d "%~dp0"
if not exist "runtime\Scripts\activate.bat" cd /d "%~dp0.."
set "PATH=%CD%\runtime;%PATH%"
call runtime\Scripts\activate.bat
python tools\gsv_tts\API\fastapi_server_example.py
pause