@echo off
REM Central bot starter (Windows) - double-click, keep the window open.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo No venv found. Run: python -m venv .venv ^&^& .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)
.venv\Scripts\python.exe main.py
pause
