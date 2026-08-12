@echo off
rem E.V control center launcher
rem 先切到自身目录；若此处没有 runtime（说明本文件在 scripts/ 子目录），
rem 再上跳一级回到项目根。两种摆放位置都能正确启动。
cd /d "%~dp0"
if not exist "runtime\Scripts\activate.bat" cd /d "%~dp0.."
set "PATH=%CD%\runtime;%PATH%"
call runtime\Scripts\activate.bat
python -m ui.control_center
pause