@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tkinter_app.py"
  exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "tools\bootstrap.py"
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python "tools\bootstrap.py"
  exit /b %ERRORLEVEL%
)

echo.
echo Python nao encontrado.
echo Abrindo instalador automatico de Python 3.9 e dependencias...
echo.
call "%~dp0Instalar_Python39_E_Dependencias.bat"
exit /b %ERRORLEVEL%
