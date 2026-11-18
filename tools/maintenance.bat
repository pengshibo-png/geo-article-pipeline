@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" "tools\maintenance.py" %*
pause
