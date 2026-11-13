@echo off
rem NewsRadar install / update. Double-click this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\tools\install.ps1"
if errorlevel 1 pause
