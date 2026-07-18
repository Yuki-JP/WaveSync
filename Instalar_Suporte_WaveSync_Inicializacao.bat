@echo off
setlocal
cd /d "%~dp0"

echo.
echo ================================================================
echo WaveSync - instalar relay de suporte na inicializacao
echo ================================================================
echo.
echo Este instalador registra o relay para abrir oculto junto com o Windows.
echo Para criar a regra de firewall automaticamente, execute como administrador.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_support_relay_startup.ps1"
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo Instalacao do relay terminou com erro.
  echo Veja as mensagens acima e tente novamente.
  pause
)

exit /b %EXITCODE%
