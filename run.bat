@echo off
REM ===========================================================================
REM  webremote launcher
REM
REM  Finds a usable Python, builds or repairs the virtual environment, and
REM  starts the server. The media-session bindings that supply track info are
REM  compiled extensions with no Python 3.14 builds, so on a 3.14-only machine
REM  this script offers to fetch Python 3.13 and use that instead.
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


REM ------------------------------------------- an environment already ex. --
:have_venv
"%VENVPY%" webremote.py --check
if not errorlevel 1 goto :launch

echo.
echo   ------------------------------------------------------------------
echo    This environment is missing an optional feature - see above.
echo   ------------------------------------------------------------------
echo.

REM Can the venv's own Python take these packages, or is it too new?
"%VENVPY%" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info < (3,14) else 1)"
if not errorlevel 1 goto :menu_repair

call :find_good_python
if defined GOODPY goto :menu_rebuild
goto :menu_existing


REM ------------------------------------------------------------- menus -----
:menu_repair
echo   The Python in this environment can run the missing pieces - they just
echo   are not installed. Installing them needs no rebuild and no compiler.
echo.
echo     [1]  Install the missing pieces  -  recommended
echo     [2]  Start anyway
echo     [3]  Exit
echo.
set "ANS="
set /p "ANS=   Choose [1/2/3]: "
if "%ANS%"=="1" goto :install_deps
if "%ANS%"=="2" goto :launch
if "%ANS%"=="3" goto :bye
echo   Please type 1, 2 or 3.
echo.
goto :menu_repair

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
echo   %GOODLABEL% is installed and does support the missing features.
echo.
echo     [1]  Rebuild the environment with it  -  recommended
echo     [2]  Start anyway
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
echo     [2]  Start anyway
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
echo   winget is not available here, so I cannot install Python for you.
echo.
echo   Download Python 3.13, install it, then run run.bat again:
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
echo.
if not exist ".venv" goto :make_venv
echo   Removing the old environment...
rmdir /s /q ".venv"

:make_venv
echo   Creating environment with %BUILDLABEL% ...
%BUILDWITH% -m venv .venv
if errorlevel 1 goto :error
goto :install_deps


REM --------------------------------------------------------- dependencies --
REM Installed in stages, deliberately. --only-binary=:all: keeps pip from ever
REM invoking a compiler, and separate commands mean one unavailable extra
REM cannot take the others down with it.
:install_deps
"%VENVPY%" -m pip install --upgrade pip >nul 2>&1

echo.
echo   [1/3] core...
"%VENVPY%" -m pip install --only-binary=:all: --quiet flask
if errorlevel 1 goto :error

echo   [2/3] media session bindings, for track info...
"%VENVPY%" -c "import sys; raise SystemExit(0 if sys.version_info < (3,13) else 1)"
if errorlevel 1 goto :media_winrt
"%VENVPY%" -m pip install --only-binary=:all: --quiet winsdk
goto :after_media

:media_winrt
REM The [all] extra pulls in Windows.Foundation and Windows.Media, without
REM which the control module cannot be imported at all.
"%VENVPY%" -m pip install --only-binary=:all: --quiet "winrt-Windows.Media.Control[all]"

:after_media
if errorlevel 1 echo         ...unavailable here - the remote will run without track info.

echo   [3/3] system volume...
REM pycaw and comtypes are pure Python; only psutil underneath them is
REM compiled, so restrict the no-compile rule to that one package rather
REM than refusing source distributions across the board.
"%VENVPY%" -m pip install --only-binary=psutil --quiet pycaw comtypes
if errorlevel 1 echo         ...unavailable here - the volume slider will be disabled.

echo.
echo   Environment ready:
"%VENVPY%" webremote.py --check
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
