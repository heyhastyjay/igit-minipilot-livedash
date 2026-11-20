@echo off
setlocal
cd /d "%~dp0"
title SBU mini-pilot dashboard

echo.
echo  SBU mini-pilot live dashboard
echo  ------------------------------
echo.
echo  Leave this black window open while you use the dashboard.
echo  Closing it closes the dashboard.
echo.

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo  Python was not found.
  echo.
  echo  1. Install Python 3.9 or newer from https://www.python.org/downloads/
  echo  2. On the first installer page, check "Add python.exe to PATH".
  echo  3. Double-click START-DASHBOARD.bat again.
  echo.
  pause
  exit /b 1
)

%PY% -c "import numpy, matplotlib" >nul 2>&1
if errorlevel 1 (
  echo  Installing numpy and matplotlib. The first time needs internet and can take a minute.
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  The install did not finish. Read the message above, then double-click this file again.
    echo.
    pause
    exit /b 1
  )
) else (
  echo  Python packages are already installed.
)

echo.
echo  Opening the dashboard.
echo.
%PY% live_dashboard.py
if errorlevel 1 (
  echo.
  echo  The dashboard stopped because of the message above.
  echo.
  pause
)
