@echo off
REM Source Mapping Tool - run script (ASCII-only to avoid cmd codepage issues)
REM Usage: double-click in Explorer / "run.bat" in cmd-PowerShell / "./run.bat" in Git Bash

setlocal enabledelayedexpansion
title Source Mapping Tool

echo.
echo ========================================
echo  Source Mapping Tool - starting...
echo ========================================
echo.

REM ---- 1. workdir ----
echo [1/5] workdir: %~dp0
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] cannot cd into %~dp0
    goto :end
)

REM ---- 2. JDK (prefer 17, fallback 21) ----
echo [2/5] looking for JDK...
if exist "C:\Program Files\Java\jdk-17\bin\java.exe" (
    set "JAVA_HOME=C:\Program Files\Java\jdk-17"
    echo       JDK 17 found at !JAVA_HOME!
) else if exist "C:\Program Files\Java\jdk-21\bin\java.exe" (
    set "JAVA_HOME=C:\Program Files\Java\jdk-21"
    echo       JDK 21 found at !JAVA_HOME!
) else (
    echo [ERROR] JDK 17 or 21 not found.
    echo         Install Eclipse Temurin from https://adoptium.net
    goto :end
)
set "PATH=%JAVA_HOME%\bin;%PATH%"

REM ---- 3. venv ----
echo [3/5] looking for venv...
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] venv not found at %VENV_PY%
    echo         First-time setup:
    echo            python -m venv .venv
    echo            .venv\Scripts\python -m pip install -r backend\requirements.txt
    goto :end
)
echo       venv OK: %VENV_PY%

REM ---- 4. port 8765 check ----
echo [4/5] checking port 8765...
netstat -ano | findstr ":8765 " | findstr LISTENING > nul
if %errorlevel%==0 (
    echo [WARN] port 8765 is already in use by:
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do (
        echo            PID %%a
    )
    echo.
    echo        Fix: run stop.bat, or reboot the PC if the PID is a zombie
    goto :end
)
echo       port 8765 is free

REM ---- 5. uvicorn ----
echo [5/5] launching FastAPI server...
echo.
echo ========================================
echo  Open in browser: http://localhost:8765
echo  Stop: press Ctrl+C in this window
echo ========================================
echo.

cd /d "%~dp0backend"
"%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port 8765
set EXITCODE=%errorlevel%

echo.
echo ========================================
if %EXITCODE%==0 (
    echo  Server stopped normally.
) else (
    echo  [ERROR] uvicorn exit code: %EXITCODE%
    echo  Read the lines above to find the cause.
)
echo ========================================

:end
echo.
echo Press any key to close this window...
pause > nul
endlocal
