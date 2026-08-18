@echo off
cd /d "%~dp0"
if not exist "runtime\Scripts\activate.bat" cd /d "%~dp0.."
set "PATH=%CD%\runtime;%PATH%"
call runtime\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
python src\tts\server.py
pause