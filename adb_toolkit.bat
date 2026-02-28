@echo off
:: ADB Toolkit - Launcher for Windows
:: Detects Python and launches the application

title ADB Toolkit - Backup, Recovery ^& Transfer

echo ============================================
echo    ADB Toolkit - Backup, Recovery ^& Transfer
echo ============================================
echo.

:: Check for Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Baixe em: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Check for dependencies
python -c "import customtkinter" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Instalando dependencias...
    pip install -r "%~dp0requirements.txt"
)

:: Run
cd /d "%~dp0"
python main.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERRO] A aplicacao encerrou com erro.
    pause
)
