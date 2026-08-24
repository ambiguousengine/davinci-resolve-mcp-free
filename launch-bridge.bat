@echo off
REM --- Kit activity marker: lets Kit's Tools tab light up when you launch this.
REM     Writes one empty file whose timestamp is the signal. Safe to delete.
mkdir "F:\AMBIGUITY\TOOLS\_engine\runs" 2>nul
echo.>"F:\AMBIGUITY\TOOLS\_engine\runs\davinci-bridge.run" 2>nul

setlocal
title DaVinci Resolve MCP Bridge (127.0.0.1:9876)

set "RESOLVE=D:\Program Files\Davinci\Resolve.exe"
set "FUSCRIPT=D:\Program Files\Davinci\fuscript.exe"
set "BRIDGE=F:\AMBIGUITY\TOOLS\davinci-bridge\src\CursorBridge.py"

REM --- start Resolve if it isn't already running ---
tasklist /FI "IMAGENAME eq Resolve.exe" 2>nul | find /I "Resolve.exe" >nul
if errorlevel 1 (
  echo Starting DaVinci Resolve...
  start "" "%RESOLVE%"
  echo Waiting for Resolve to load ^(open a project if prompted^)...
  timeout /t 20 /nobreak >nul
) else (
  echo DaVinci Resolve already running.
)

echo.
echo Launching in-app bridge on http://127.0.0.1:9876 ...
echo Runs inside Resolve's interpreter (no segfault). Leave this window open.
echo To stop: close this window or DaVinci Resolve.
echo.
"%FUSCRIPT%" -l py3 "%BRIDGE%"
