@echo off
REM webremote launcher: builds a virtual environment on first run, then starts
REM the server and restarts it if it crashes. Any arguments are passed
REM through, e.g.  run.bat --token
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
REM One-shot commands are not servers; run them once and pass their result on.
for %%A in (%*) do (
    if /i "%%~A"=="--check" goto :run_once
    if /i "%%~A"=="--help" goto :run_once
    if /i "%%~A"=="-h" goto :run_once
)


REM ------------------------------------------------------------ supervisor --
REM Restart the server whenever it exits unexpectedly. What counts as
REM "expected" is set by webremote.py's exit codes:
REM   0            stopped on purpose (Ctrl+C)
REM   2            bad arguments or port already in use - a retry cannot help
REM   -1073741510  0xC000013A, killed by Ctrl+C / Ctrl+Break before it could
REM                exit cleanly
REM Anything else - a Python traceback, a crash inside a Windows component -
REM gets a restart. If it keeps dying within a minute of starting, the pause
REM between attempts grows, so a persistent fault does not spin the CPU.
set /a QUICKFAILS=0

:supervise
call :now STARTED
"%VENVPY%" webremote.py %*
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" goto :eof
if "%CODE%"=="2" goto :stopped_for_good
if "%CODE%"=="-1073741510" goto :eof

call :now ENDED
set /a RAN=ENDED-STARTED
if %RAN% LSS 0 set /a RAN+=86400
if %RAN% GEQ 60 (set /a QUICKFAILS=0) else (set /a QUICKFAILS+=1)

set /a WAIT=3
if %QUICKFAILS% GEQ 3 set /a WAIT=15
if %QUICKFAILS% GEQ 6 set /a WAIT=60

echo.
echo   ------------------------------------------------------------------
echo    %DATE% %TIME:~0,8%  webremote exited unexpectedly (code %CODE%)
echo    after %RAN% s. Restarting in %WAIT% s - press Ctrl+C to stop instead.
if %QUICKFAILS% GEQ 3 echo    It has failed %QUICKFAILS% times in a row soon after starting.
echo   ------------------------------------------------------------------
echo.
REM ping rather than timeout: timeout refuses to run without a console
REM (e.g. from Task Scheduler) and returns at once, which would spin.
set /a PINGS=WAIT+1
ping -n %PINGS% 127.0.0.1 >nul
goto :supervise

:stopped_for_good
echo.
echo   Not restarting: fix the problem above, then run this again.
pause
exit /b 2

:run_once
"%VENVPY%" webremote.py %*
exit /b %ERRORLEVEL%


REM ------------------------------------------------------------- helpers ---
:now
REM Seconds since midnight into the variable named by %1. %TIME% pads the hour
REM with a space; the "1xx-100" trick stops 08 and 09 being read as octal.
set "T=%TIME: =0%"
set /a "%1=(1%T:~0,2%-100)*3600 + (1%T:~3,2%-100)*60 + (1%T:~6,2%-100)"
goto :eof

:failed
echo Setup failed - see the messages above.
pause
exit /b 1
