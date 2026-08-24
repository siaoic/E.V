@echo off
set PATH=.\node;%PATH%
.\node\node.exe sync_plugin_config.js
.\node\node.exe main.js
pause