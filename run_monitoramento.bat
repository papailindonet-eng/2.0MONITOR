@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Vai para a pasta deste script
cd /d "%~dp0"

echo ==========================================
echo   Sistema de Monitoramento de Logistica
echo ==========================================

echo [1/5] Verificando Python...
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=python"
    ) else (
        echo ERRO: Python nao encontrado no PATH.
        echo Instale Python 3.10+ e marque "Add Python to PATH".
        pause
        exit /b 1
    )
)

echo [2/5] Criando ambiente virtual (.venv) se necessario...
if not exist ".venv\Scripts\python.exe" (
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo ERRO: Falha ao criar ambiente virtual.
        pause
        exit /b 1
    )
)

echo [3/5] Instalando/atualizando dependencias...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO: Falha ao instalar dependencias.
    echo Verifique sua conexao com a internet e tente novamente.
    pause
    exit /b 1
)

echo [4/5] Preparando abertura automatica do navegador...
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 5; Start-Process 'http://localhost:5000/login'"

echo [5/5] Iniciando aplicacao Flask...
echo (Mantenha esta janela aberta para o sistema continuar rodando)
python app.py

endlocal
