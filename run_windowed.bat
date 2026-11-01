@echo off
cd /d "%~dp0"
python main.py --windowed
if errorlevel 1 pause
