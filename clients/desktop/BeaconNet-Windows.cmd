@echo off
rem BeaconNet Desktop for Windows: double-click to start. First run installs asyncssh.
setlocal
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python 3 is needed. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
  echo or from the Microsoft Store, then double-click this file again.
  pause
  exit /b 1
)
%PY% -c "import asyncssh" >nul 2>nul || (
  echo Installing asyncssh for your user account...
  %PY% -m pip install --user --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
)
rem pythonw runs without a console window; fall back to python if it is missing.
set PYW=%PY:python=pythonw%
if "%PY%"=="py -3" set PYW=pyw -3
start "BeaconNet" %PYW% "%~dp0beaconnet_desktop.py" || %PY% "%~dp0beaconnet_desktop.py"
