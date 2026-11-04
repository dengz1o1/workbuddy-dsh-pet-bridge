@echo off
rem Started by gate.cmd. Can also be run manually (then it is not gated:
rem it exits by itself after the tunnel is lost).
rem KEEP THIS FILE ASCII-ONLY (cmd.exe reads .cmd in the OEM codepage).
setlocal
cd /d "%~dp0"

rem If auto-discovery is slow or unstable, pin the DSH port here once you know it
rem (get it from: python bridge.py --discover). Uncomment the next line.
rem set "DSH_PORT=3080"

python "%~dp0bridge.py" -v %*
