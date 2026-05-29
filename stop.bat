@echo off
REM Stop the FastAPI server listening on port 8765 (ASCII-only)

setlocal enabledelayedexpansion

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do (
    set "PID=%%a"
    goto :found
)

echo No server is running on port 8765.
goto :end

:found
echo Killing PID !PID! ...
taskkill /F /PID !PID!

:end
echo.
echo Press any key to close...
pause > nul
endlocal
