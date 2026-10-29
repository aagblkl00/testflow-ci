@echo off
title TestFlow CI - Stopper
echo Stopping TestFlow CI (ports 8000 / 5173 / 8100) ...
echo.

for /f "tokens=5" %%a in ('netstat -aon ^| findstr /c:":8000 " ^| findstr /c:"LISTENING"') do (taskkill /f /pid %%a >nul 2>&1 && echo   port 8000 stopped ^(PID %%a^))
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /c:":5173 " ^| findstr /c:"LISTENING"') do (taskkill /f /pid %%a >nul 2>&1 && echo   port 5173 stopped ^(PID %%a^))
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /c:":8100 " ^| findstr /c:"LISTENING"') do (taskkill /f /pid %%a >nul 2>&1 && echo   port 8100 stopped ^(PID %%a^))

echo.
echo Done.
"%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul 2>&1
