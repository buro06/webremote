@echo off
REM ===========================================================================
REM  webremote launcher
REM
REM  Finds a usable Python, builds a virtual environment, and starts the
REM  server. The media-session bindings that provide track info are compiled
REM  extensions with no Python 3.14 builds, so if 3.14 is all that is
REM  installed this script offers to fetch Python 3.13 and use that instead.
REM ===========================================================================
setlocal
cd /d "%~dp0"
title webremote

set "VENVPY=.venv\Scripts\python.exe"
set "PY313=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

echo.
echo   ============================================
echo    webremote - control this PC from your phone
echo   ============================================
echo.

if exist "%VENVPY%" goto :have_venv


REM ------------------------------------------------------------ first run --
:first_run
call :find_good_python
if defined GOODPY goto :build_env

call :find_any_python
if not defined ANYPY goto :no_python
goto :menu_new


REM --------------------------------------------------- an env already ex. --
:have_venv
"%VENVPY%" webremote.py --check
if not errorlevel 1 goto :launch

echo.
echo   ------------------------------------------------------------------
echo    This environment can control playback, but cannot read track info.
echo   ------------------------------------------------------------------
echo.
call :find_good_python
if defined GOODPY goto :menu_rebuild
goto :menu_existing


REM ------------------------------------------------------------- menus -----
:menu_new
echo   The only Python installed is %ANYLABEL%.
echo.
call :explain
echo     [1]  Install Python 3.13 and use it  -  recommended, full features
echo     [2]  Continue with %ANYLABEL%  -  buttons work, no track info
echo     [3]  Exit
echo.
set "ANS="
set /p "ANS=   Choose [1/2/3]: "
if "%ANS%"=="1" goto :install_313
if "%ANS%"=="2" goto :build_env_any
if "%ANS%"=="3" goto :bye
echo   Please type 1, 2 or 3.
echo.
goto :menu_new

:menu_rebuild
echo   Good news: %GOODLABEL% is installed and does support track info.
echo.
echo     [1]  Rebuild the environment with it  -  recommended
echo     [2]  Start anyway, without track info
echo     [3]  Exit
echo.
set "ANS="
set /p "ANS=   Choose [1/2/3]: "
if "%ANS%"=="1" goto :build_env
if "%ANS%"=="2" goto :launch
if "%ANS%"=="3" goto :bye
echo   Please type 1, 2 or 3.
echo.
goto :menu_rebuild

:menu_existing
call :explain
echo     [1]  Install Python 3.13 and rebuild  -  recommended, full features
echo     [2]  Start anyway, without track info
echo     [3]  Exit
echo.
set "ANS="
set /p "ANS=   Choose [1/2/3]: "
if "%ANS%"=="1" goto :install_313
if "%ANS%"=="2" goto :launch
if "%ANS%"=="3" goto :bye
echo   Please type 1, 2 or 3.
echo.
goto :menu_existing

:explain
echo   The title, artist, album art and seek bar come from the Windows
echo   media-session API. Its Python bindings are compiled extensions, and
echo   nobody publishes builds of them for Python 3.14 yet - so on 3.14 the
echo   remote can only send media keys. The buttons all work; the phone just
echo   shows no track info.
echo.
goto :eof


REM --------------------------------------------------------- install 3.13 --
:install_313
echo.
where winget >nul 2>&1
if errorlevel 1 goto :no_winget

echo   Installing Python 3.13 with winget. Accept any prompt Windows shows.
echo.
winget install --id Python.Python.3.13 -e --source winget --scope user --accept-package-agreements --accept-source-agreements
if not errorlevel 1 goto :after_313
echo.
echo   Per-user install failed; retrying as a machine-wide install...
winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements

:after_313
echo.
call :find_good_python
if defined GOODPY goto :build_env

echo   Python 3.13 was installed but is not visible in this window yet.
echo   Close this window, open a new one, and run run.bat again.
echo.
pause
exit /b 1

:no_winget
echo   winget is not available on this system, so I cannot install it for you.
echo.
echo   Download Python 3.13 here, install it, then run run.bat again:
echo     https://www.python.org/downloads/
echo.
echo   Tick "Add python.exe to PATH" in the installer.
echo.
pause
exit /b 1


REM ------------------------------------------------------- build the env ---
:build_env
set "BUILDWITH=%GOODPY%"
set "BUILDLABEL=%GOODLABEL%"
goto :do_build

:build_env_any
set "BUILDWITH=%ANYPY%"
set "BUILDLABEL=%ANYLABEL%"
goto :do_build

:do_build
if not exist ".venv" goto :make_venv
echo   Removing the old environment...
rmdir /s /q ".venv"

:make_venv
echo   Creating environment with %BUILDLABEL% ...
%BUILDWITH% -m venv .venv
if errorlevel 1 goto :error

echo   Installing dependencies...
"%VENVPY%" -m pip install --upgrade pip >nul 2>&1

REM --only-binary=:all: keeps pip from ever invoking a compiler, so a missing
REM wheel degrades gracefully instead of demanding Visual Studio.
"%VENVPY%" -m pip install --only-binary=:all: -r requirements.txt
if not errorlevel 1 goto :launch

echo.
echo   An optional extra had no prebuilt wheel here. Installing the core only;
echo   the remote will run in media-key mode.
echo.
"%VENVPY%" -m pip install flask
if errorlevel 1 goto :error
goto :launch


REM ------------------------------------------------------------- launch ----
:launch
echo.
"%VENVPY%" webremote.py %*
goto :bye


REM ------------------------------------------------------ python finders ---
:find_good_python
REM A Python that has prebuilt media-session wheels: 3.9 - 3.13.
set "GOODPY="
set "GOODLABEL="
for %%V in (3.13 3.12 3.11 3.10 3.9) do call :try_py %%V
if defined GOODPY goto :eof
if not exist "%PY313%" goto :eof
set "GOODPY="%PY313%""
set "GOODLABEL=Python 3.13"
goto :eof

:try_py
if defined GOODPY goto :eof
py -%1 -c "import sys" >nul 2>&1
if errorlevel 1 goto :eof
set "GOODPY=py -%1"
set "GOODLABEL=Python %1"
goto :eof

:find_any_python
set "ANYPY="
set "ANYLABEL="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "ANYPY=python"
if defined ANYPY goto :label_any
py -c "import sys" >nul 2>&1
if not errorlevel 1 set "ANYPY=py"
if not defined ANYPY goto :eof

:label_any
for /f "tokens=2" %%i in ('%ANYPY% --version 2^>^&1') do set "ANYLABEL=Python %%i"
goto :eof


REM -------------------------------------------------------------- exits ----
:no_python
echo   No Python installation found.
echo.
echo   Install Python 3.13 from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH", then run run.bat again.
echo.
pause
exit /b 1

:error
echo.
echo   Setup failed. See the messages above.
echo.
pause
exit /b 1

:bye
endlocal
