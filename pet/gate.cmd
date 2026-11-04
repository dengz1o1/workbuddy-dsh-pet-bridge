@echo off
rem ---------------------------------------------------------------------------
rem  Gate: run the bridge only while the UU port mapping is reachable.
rem  Stops the bridge and clears dsh-pet as soon as the tunnel goes down.
rem  The decision input is tunnel reachability, NOT the UU process name,
rem  so UU upgrades or renames do not affect this script.
rem
rem  KEEP THIS FILE ASCII-ONLY. cmd.exe reads .cmd in the OEM codepage
rem  (CP936 on zh-CN). Non-ASCII comments break line parsing.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PROBE=http://127.0.0.1:18787/health"
set "MARKER=WBPET-RELAY"
set "LOG=%~dp0gate.log"
set "WAS=0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Install Python 3.9+ first.
  pause
  exit /b 1
)

where curl >nul 2>nul
if errorlevel 1 (
  echo [ERROR] curl not found. Windows 10 1803+ ships curl.exe.
  pause
  exit /b 1
)

echo [%date% %time%] gate started >> "%LOG%"

:loop
rem ---- probe the tunnel: -f makes 4xx/5xx count as failure, -m 1 caps at 1s ----
if "%PET_TOKEN%"=="" (
  curl -f -s -m 1 -o nul "%PROBE%" 2>nul
) else (
  curl -f -s -m 1 -H "x-pet-token: %PET_TOKEN%" -o nul "%PROBE%" 2>nul
)
if errorlevel 1 goto offline

rem ---------------- tunnel is up ----------------
if "%WAS%"=="0" (
  echo [%date% %time%] tunnel up >> "%LOG%"
  set "WAS=1"
)
tasklist /v /fo csv /fi "imagename eq cmd.exe" 2>nul | findstr /c:"%MARKER%" >nul
if errorlevel 1 (
  echo [%date% %time%] starting bridge >> "%LOG%"
  start "%MARKER%" /min "%~dp0run-bridge.cmd"
)
goto tick

rem ---------------- tunnel is down ----------------
:offline
if "%WAS%"=="1" (
  echo [%date% %time%] tunnel down, stopping bridge >> "%LOG%"
  taskkill /f /t /fi "windowtitle eq %MARKER%" >nul 2>&1
  rem Last-resort clear: dsh-pet lives on loopback, so it works without the tunnel.
  rem Port comes from dsh-port.txt written by the bridge (desktop port may vary).
  set "DSHPORT=3080"
  if exist "%~dp0dsh-port.txt" set /p DSHPORT=<"%~dp0dsh-port.txt"
  if "%PET_TOKEN%"=="" (
    curl -s -m 2 -X DELETE "http://127.0.0.1:!DSHPORT!/dsh-pet-7340/work-status" >nul 2>&1
  ) else (
    curl -s -m 2 -X DELETE -H "x-pet-token: %PET_TOKEN%" "http://127.0.0.1:!DSHPORT!/dsh-pet-7340/work-status" >nul 2>&1
  )
  set "WAS=0"
)

:tick
rem ~1 second. ping is used instead of timeout to avoid console errors.
ping -n 2 127.0.0.1 >nul
goto loop
