@echo off
REM Sets up a virtual environment on first run, then starts the remote.
cd /d "%~dp0"
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" webremote.py %*
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.9+ is installed and on your PATH.
pause
