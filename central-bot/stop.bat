@echo off
REM Stops the central bot started via run.bat (reads logs\bot.pid).
cd /d %~dp0
if not exist logs\bot.pid (
    echo No pid file. Is the bot running?
    pause
    exit /b 1
)
set /p PID=<logs\bot.pid
taskkill /PID %PID% /F
echo Stopped %PID%.
pause
