@echo off
rem Bilibili danmaku service launcher (ASCII only, GBK/UTF-8 safe)
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

rem Clear env that may point to other projects' runtimes
set "PYTHONPATH="
set "PYTHONHOME="

set "PYTHON=%SCRIPT_DIR%\runtime\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] python not found: %PYTHON%
    pause
    exit /b 1
)

"%PYTHON%" -m src.danmaku.bili_danmaku
pause
