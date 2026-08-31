@echo off
rem EV TUI launcher (dsh-TUI built-in EV mode). Replaces console chat mode.
rem DO NOT add non-ASCII comments here: cmd parses .bat in ANSI codepage.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "NODE_ENV=production"
where node >nul 2>nul
if errorlevel 1 (
  echo [ev-tui] Node.js not found. Please install Node.js 20+.
  pause
  exit /b 1
)
node "dsh-TUI-main\bin\ev-tui.js" %*
if errorlevel 1 (
  echo.
  echo [ev-tui] exited with error - window kept open for debugging.
  pause
)
endlocal
