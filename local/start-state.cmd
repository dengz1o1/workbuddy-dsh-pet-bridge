@echo off
rem Start the local state server (this machine, the UU controlled side).
rem Optional: set PET_TOKEN=... and PET_STATE_PORT=8787 before running.
rem KEEP THIS FILE ASCII-ONLY (cmd.exe reads .cmd in the OEM codepage).
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Install Python 3.9+ first.
  pause
  exit /b 1
)

python "%~dp0state_server.py" %*
