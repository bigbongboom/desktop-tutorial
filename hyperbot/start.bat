@echo off
REM ===================================================================
REM  hyperbot - one-click start for Windows.
REM  Double-click this file, or run  start.bat  in a terminal.
REM  It installs what is missing, then opens the dashboard in a browser.
REM ===================================================================
setlocal
cd /d "%~dp0"

echo.
echo   hyperbot - Hyperliquid copy desk
echo   ================================
echo.

REM --- find Python -------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (where python >nul 2>&1 && set PY=python)
if not defined PY (
  echo   [X] Python is not installed, or not on your PATH.
  echo.
  echo       Install it from https://www.python.org/downloads/
  echo       IMPORTANT: tick "Add python.exe to PATH" in the installer.
  echo.
  pause
  exit /b 1
)
echo   [1/3] Using Python: %PY%

REM --- dependencies ------------------------------------------------
echo   [2/3] Checking dependencies...
%PY% -c "import httpx, websockets, yaml, aiohttp" >nul 2>&1
if errorlevel 1 (
  echo         Installing ^(first run only, takes a minute^)...
  %PY% -m pip install --quiet --upgrade pip
  %PY% -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo   [X] Install failed. Try running this by hand to see the error:
    echo       %PY% -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

REM --- config ------------------------------------------------------
if not exist config.yaml (
  copy /y config.example.yaml config.yaml >nul
  echo         Created config.yaml from the example.
)
if not exist .env (
  copy /y .env.example .env >nul
  echo         Created .env - add your account address there later.
)

REM --- go ----------------------------------------------------------
echo   [3/3] Starting the dashboard...
echo.
echo   ==========================================================
echo     Opening  http://localhost:8730
echo     This window must STAY OPEN - it is the server.
echo     Press Ctrl+C here to stop.
echo   ==========================================================
echo.
%PY% run.py serve
echo.
echo   Server stopped.
pause
