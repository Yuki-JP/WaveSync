@echo off
setlocal
cd /d "%~dp0"

echo.
echo ================================================================
echo WaveSync - remover relay de suporte da inicializacao
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_support_relay_startup.ps1" -Remove
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo Remocao do relay terminou com erro.
  echo Veja as mensagens acima e tente novamente.
  pause
)

exit /b %EXITCODE%
