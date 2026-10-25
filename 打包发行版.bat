@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 -X utf8 打包发行版.py
) else (
    python 打包发行版.py
)
echo.
pause
