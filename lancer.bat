@echo off
title MG Shorts Studio
cd /d "%~dp0"
if exist update.ps1 powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
where py >nul 2>nul
if %errorlevel%==0 (py -3 server.py --open) else (python server.py --open)
pause
