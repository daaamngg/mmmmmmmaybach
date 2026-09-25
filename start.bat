@echo off
rem Windows launcher: double-click this file.
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo [finbot] Creating virtual environment...
    %PY% -m venv .venv || goto :nopython
)

echo [finbot] Checking dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :pipfail

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo [finbot] Notepad will open: paste your bot token after BOT_TOKEN= then save and close it.
    start /wait notepad ".env"
)

echo [finbot] Starting. Close this window or press Ctrl+C to stop the bot.
".venv\Scripts\python.exe" run.py
pause
exit /b 0

:nopython
echo.
echo [finbot] Python 3.10+ not found. Install it from https://www.python.org/downloads/
echo          and tick "Add python.exe to PATH" during installation.
pause
exit /b 1

:pipfail
echo.
echo [finbot] Could not install dependencies. Check your internet connection and try again.
pause
exit /b 1
