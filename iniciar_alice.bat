@echo off
chcp 65001 >nul
title Alice
cd /d "%~dp0"

echo.
echo   Iniciando Alice...
echo   Voz/chat:  http://127.0.0.1:8756
echo   Vision:    http://127.0.0.1:8757
echo   (Ctrl+C en esta ventana para apagarla)
echo.

REM Abre las dos interfaces en cuanto los servidores esten listos.
REM La vision tarda mas (carga los modelos de caras la 1a vez).
start "" /min cmd /c "timeout /t 5 /nobreak >nul & start "" http://127.0.0.1:8756 & timeout /t 6 /nobreak >nul & start "" http://127.0.0.1:8757"

REM Arranca Alice con el interprete del entorno virtual.
".\.venv\Scripts\python.exe" main.py

echo.
echo   Alice se ha detenido.
pause
