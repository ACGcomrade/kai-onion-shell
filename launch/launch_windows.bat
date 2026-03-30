@echo off
:: Onion Shell — Windows launcher
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..

cd /d "%PROJECT_DIR%"

:: Start watcher if not running
tasklist /FI "IMAGENAME eq python.exe" 2>NUL | find /I "python.exe" >NUL
if errorlevel 1 (
    start /B pythonw onion_shell.py _daemon
    echo Watcher started.
)

echo Onion Shell watcher is running. Use 'onion_shell.py status' to check.
pause
