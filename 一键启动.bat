@echo off
cd /d "%~dp0"
set PY_BIN=
where py >nul 2>nul
if %errorlevel% equ 0 (
    set PY_BIN=py
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set PY_BIN=python
    )
)

if "%PY_BIN%"=="" (
    echo [ERROR] Python not found in PATH!
    echo Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

%PY_BIN% start.py
if %errorlevel% neq 0 (
    pause
)
