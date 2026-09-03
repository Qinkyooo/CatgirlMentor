@echo off
setlocal
cd /d "%~dp0"
echo.
echo ============================================
echo    CatgirlMentor Installer
echo ============================================
rem ---------- 1/5 Check Python ----------
echo.
echo == [1/5] Check Python >=3.11 ==
set "PYCMD="
where python >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PYCMD=py"
)
if not defined PYCMD goto :NoPython
%PYCMD% --version >nul 2>&1
if errorlevel 1 goto :NoPython

%PYCMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 goto :OldPython
echo    OK: Python version ok

rem ---------- 2/5 Check frontend build tool ----------
echo.
echo == [2/5] Check frontend tool (only for WebUI) ==
set "HAVE_BUN="
set "HAVE_NODE="
where bun >nul 2>nul
if not errorlevel 1 set "HAVE_BUN=1"
where node >nul 2>nul
if not errorlevel 1 set "HAVE_NODE=1"
if defined HAVE_BUN (
    echo    OK: Found Bun
    goto :FrontendDone
)
if defined HAVE_NODE (
    echo    OK: Found Node.js, will build with npm
    goto :FrontendDone
)
echo    WARN: Bun / Node.js not detected
echo         Onboard wizard and QQ bot can work without frontend.
echo         WebUI requires Bun or Node.js.
set /p ANS=    Press Enter to continue, input N to abort and install Bun:
if /i "%ANS%"=="N" goto :NeedBun
:FrontendDone

rem ---------- 3/5 Create venv ----------
echo.
echo == [3/5] Create virtual environment ==
if not exist ".venv" %PYCMD% -m venv .venv
echo    Virtual env ready: .venv

rem ---------- 4/5 Install project ----------
echo.
echo == [4/5] Install project (editable mode, network required) ==
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :Fail
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :Fail
echo    Install finished

rem ---------- 5/5 Verify and launch wizard ----------
echo.
echo == [5/5] Verify and start config wizard ==
".venv\Scripts\nanobot.exe" --version
echo.
echo Starting interactive setup wizard...
".venv\Scripts\nanobot.exe" onboard --wizard
echo.
echo Done! Useful commands:
echo   nanobot webui      Open web browser UI (needs Bun/Node)
echo   nanobot gateway    Run background service
echo.
pause
exit /b 0

:NoPython
echo   Python not found. Please install Python 3.11+:
echo   winget install Python.Python.3.12
pause
exit /b 1

:OldPython
echo   Python version too old. Requires >=3.11. Please upgrade.
pause
exit /b 1

:NeedBun
echo   Please install Bun first: winget install Oven-sh.Bun
echo   Then re?run this script.
pause
exit /b 1

:Fail
echo   Install failed. Check network connection and retry.
pause
exit /b 1
