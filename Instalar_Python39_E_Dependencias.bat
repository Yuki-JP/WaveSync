@echo off
setlocal
cd /d "%~dp0"

echo.
echo ================================================================
echo PluralEyes Clone - instalador de Python 3.9 e dependencias
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_python39_deps.ps1"

if %ERRORLEVEL% NEQ 0 (
  echo.
  echo O instalador terminou com erro.
  echo Veja as mensagens acima e tente novamente.
  pause
)

exit /b %ERRORLEVEL%
