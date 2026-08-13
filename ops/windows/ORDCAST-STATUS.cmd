@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Show-OrdCastStatus.ps1"
echo.
pause
