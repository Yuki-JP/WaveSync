@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_windows.ps1"

if %ERRORLEVEL% NEQ 0 (
  echo.
  echo O instalador terminou com erro.
  pause
)

exit /b %ERRORLEVEL%
