@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY_BIN=

rem 优先用 py 启动器（它指向真正安装的 Python）
where py >nul 2>nul
if %errorlevel% equ 0 (
    set PY_BIN=py -3
) else (
    rem python 可能是 Microsoft Store 的占位程序，跳过它
    for /f "delims=" %%i in ('where python 2^>nul') do (
        echo %%i | find /i "WindowsApps" >nul
        if errorlevel 1 if not defined PY_BIN set PY_BIN=python
    )
)

if "%PY_BIN%"=="" (
    echo [错误] 没有找到可用的 Python。
    echo 请先安装 Python 3.12 或 3.13，安装时务必勾选 "Add Python to PATH"。
    echo 下载地址: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo 使用的解释器: %PY_BIN%
%PY_BIN% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本过低，请升级到 3.12 或 3.13。
    pause
    exit /b 1
)

%PY_BIN% start.py
if errorlevel 1 (
    echo.
    echo [错误] 启动失败。若是依赖没装上，可手动执行：
    echo    %PY_BIN% -m pip install -r requirements.txt -i https://pypi.org/simple
    echo.
    pause
)
