@echo off
:: ADB Toolkit - Unified Launcher (elevation with fallback)
:: Tries admin privileges first; falls back to normal mode with a warning.

title ADB Toolkit - Backup, Recovery ^& Transfer
setlocal EnableDelayedExpansion

:: ---- Check if already elevated ----
>nul 2>&1 net session
if %ERRORLEVEL% == 0 goto :RUN_ELEVATED

:: ---- Not elevated — try UAC ----
echo ============================================
echo    ADB Toolkit - Solicitando Elevacao
echo ============================================
echo.
echo Solicitando privilegios de administrador...
echo Se o prompt UAC for recusado, o app iniciara
echo em modo normal (sem drivers automaticos).
echo.

set "BATCH=%~f0"
set "ARGS=%*"

powershell -NoProfile -Command ^
  "try { Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList '/c \"\"%BATCH%\" --elevated %ARGS%\"' -WorkingDirectory '%~dp0'; exit 0 } catch { exit 1 }"

if %ERRORLEVEL% == 0 (
    :: UAC prompt accepted — elevated instance will handle it
    exit /b 0
)

:: ---- UAC failed or cancelled — fallback to normal ----
echo.
echo ############################################
echo # AVISO: Executando SEM privilegios admin  #
echo # - Instalacao de drivers indisponivel     #
echo # - Download do ADB pode falhar            #
echo # - Adicionar ADB ao PATH indisponivel     #
echo ############################################
echo.
goto :RUN_NORMAL

:RUN_ELEVATED
echo ============================================
echo    ADB Toolkit - Modo Administrador
echo    Privilegios elevados: ATIVO
echo ============================================
echo.

:: Remove the --elevated flag from args if present
set "CLEAN_ARGS="
for %%A in (%*) do (
    if /I not "%%~A"=="--elevated" (
        set "CLEAN_ARGS=!CLEAN_ARGS! %%A"
    )
)
set "ARGS=%CLEAN_ARGS%"

:RUN_NORMAL
cd /d "%~dp0"

:: ---- Check for Python ----
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Baixe em: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo Python encontrado:
python --version
echo.

:: ---- Check/install dependencies ----
python -c "import customtkinter" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Instalando dependencias...
    pip install -r "%~dp0requirements.txt"
    echo.
)

:: ---- Run application ----
echo Iniciando ADB Toolkit...
echo.
python main.py %ARGS%

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERRO] A aplicacao encerrou com erro ^(codigo: %ERRORLEVEL%^).
    pause
)

endlocal
