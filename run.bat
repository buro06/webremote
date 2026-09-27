@echo off
REM webremote launcher: builds a virtual environment on first run, then starts
REM the server. Any arguments are passed through, e.g.  run.bat --token
setlocal
cd /d "%~dp0"
title webremote

set "VENVPY=.venv\Scripts\python.exe"
if exist "%VENVPY%" goto :launch

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

echo Creating the environment...
%PY% -m venv .venv || goto :failed
"%VENVPY%" -m pip install --quiet --upgrade pip

REM Installed in two steps so one unavailable piece cannot block the other.
REM --only-binary stops pip from trying to compile anything: if there is no
REM prebuilt package for this Python version, skip it rather than fail.
echo Installing media session support...
"%VENVPY%" -m pip install --quiet --only-binary=:all: winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media winrt-Windows.Media.Control winrt-Windows.Storage.Streams
if errorlevel 1 echo   ...not available for this Python version; buttons will use media keys.
echo Installing volume support...
"%VENVPY%" -m pip install --quiet --only-binary=psutil pycaw
if errorlevel 1 echo   ...not available; volume will use media keys.
echo.
"%VENVPY%" webremote.py --check
echo.

:launch
"%VENVPY%" webremote.py %*
goto :eof

:failed
echo Setup failed - see the messages above.
pause
exit /b 1
