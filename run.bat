@echo off
rem PI P-621.1CD 位移台控制台
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 找不到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 界面地址：http://127.0.0.1:8000
echo 关闭本窗口即停止服务；位移台保持原位不动（要卸力请点界面上的"释放"）。
".venv\Scripts\python.exe" -m backend.server
pause
