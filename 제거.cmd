@echo off
rem NewsRadar uninstall. Double-click this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\NewsRadar\app\tools\uninstall.ps1"
if errorlevel 1 pause
