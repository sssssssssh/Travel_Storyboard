@echo off
REM Travel Storyboard — double-click launcher for Windows.
REM Runs scripts\start.ps1 with a permissive execution policy so no manual
REM PowerShell configuration is needed.
setlocal
set "SCRIPT=%~dp0start.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
if errorlevel 1 (
  echo.
  echo Startup failed. Read the messages above.
  pause
)
endlocal
