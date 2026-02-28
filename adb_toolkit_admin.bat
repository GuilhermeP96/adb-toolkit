@echo off
:: ADB Toolkit - Launcher with Administrator Privileges
:: Automatically requests UAC elevation on Windows

title ADB Toolkit - Modo Administrador

:: ---- Check if already elevated ----
>nul 2>&1 net session
if %ERRORLEVEL% == 0 goto :ELEVATED

:: ---- Request elevation via UAC ----
echo Solicitando privilegios de Administrador...
echo.

:: Build a PowerShell one-liner that triggers the UAC prompt
:: and re-launches this batch file elevated via cmd.exe
set "BATCH=%~f0"
set "ARGS=%*"

powershell -NoProfile -Command ^
  "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList '/c \"\"%BATCH%\" %ARGS%\"' -WorkingDirectory '%~dp0'"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERRO] Elevacao cancelada ou falhou.
    pause
)
exit /b 0

:ELEVATED
:: ---- Running with admin privileges ----
echo ============================================
echo    ADB Toolkit - Modo Administrador
echo    Privilegios elevados: ATIVO
echo ============================================
echo.

cd /d "%~dp0"

:: Check for Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Baixe em: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: Show Python version
echo Python encontrado:
python --version
echo.

:: Check/install dependencies
python -c "import customtkinter" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Instalando dependencias...
    pip install -r "%~dp0requirements.txt"
    echo.
)

:: Run application
echo Iniciando ADB Toolkit...
echo.
python main.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERRO] A aplicacao encerrou com erro ^(codigo: %ERRORLEVEL%^).
)
