@echo off
setlocal
set "INSTALLER=%~dp0Install-OrdCastLocal.ps1"
echo OrdCast installs for your Windows account. Administrator approval is not required.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"
if errorlevel 1 echo Installation did not complete.
pause
