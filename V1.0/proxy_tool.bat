@echo off
rem ============================================
rem  系统代理工具启动脚本（双击运行即可）
rem ============================================
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw proxy_tool.py
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw proxy_tool.py
    exit /b 0
)

echo [错误] 未找到 Python，请安装 Python 并勾选 "Add python.exe to PATH"。
pause
