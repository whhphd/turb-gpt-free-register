@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0webui.ps1" restart
  goto :eof
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0webui.ps1" %*
