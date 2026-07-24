@echo off
REM Build the portable offline-bundle\ folder (requires Node + Maven + JDK 17).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\package-offline.ps1"
if errorlevel 1 exit /b 1
echo.
echo Package ready: offline-bundle\
exit /b 0
