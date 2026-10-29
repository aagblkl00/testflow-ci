@echo off
title TestFlow CI - Console
cd /d "%~dp0"

if not exist logs mkdir logs

echo ==================================================
echo    TestFlow CI  -  starting services
echo ==================================================
echo.

echo   [1/3] demo app   port 8100
start "" /b cmd /c ".venv\Scripts\python.exe -m uvicorn demo_app.main:app --host 127.0.0.1 --port 8100 > logs\demo_app.log 2>&1"

echo   [2/3] backend    port 8000
start "" /b cmd /c ".venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 > logs\backend.log 2>&1"

echo   [3/3] frontend   port 5173
start "" /b cmd /c "cd /d "%~dp0frontend" && npm run dev > ..\logs\frontend.log 2>&1"

echo.
echo   waiting 8s for services to be ready ...
"%SystemRoot%\System32\timeout.exe" /t 8 /nobreak >nul 2>&1

start "" http://127.0.0.1:5173

echo.
echo   --------------------------------------------------
echo    Web UI  : http://127.0.0.1:5173    admin / admin123
echo    API doc : http://127.0.0.1:8000/docs
echo    Demo    : http://127.0.0.1:8100/login    demo / demo123
echo    Logs    : %~dp0logs\
echo   --------------------------------------------------
echo.
echo   Services run in the background. No extra windows.
echo.
echo   *** KEEP THIS WINDOW OPEN ***
echo   Closing this window stops all services.
echo.

:hold
"%SystemRoot%\System32\timeout.exe" /t 3600 /nobreak >nul 2>&1
goto hold
