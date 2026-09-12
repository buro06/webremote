@echo off
REM Sets up a virtual environment on first run, then starts the remote.
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul

    REM --only-binary=:all: means pip never tries to compile anything, so a
    REM missing wheel degrades gracefully instead of demanding Visual Studio.
    ".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   ---------------------------------------------------------------
        echo   No prebuilt wheel for one of the optional extras on this Python.
        echo   Installing the core only - the remote will still run, but in
        echo   media-key mode: buttons work, track info does not.
        echo   To get track info, see README: "No wheel for your Python".
        echo   ---------------------------------------------------------------
        echo.
        ".venv\Scripts\python.exe" -m pip install flask || goto :error
    )
)

".venv\Scripts\python.exe" webremote.py %*
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.9+ is installed and on your PATH.
pause
