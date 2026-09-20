@echo off
rem PI P-621.1CD 位移台控制台
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 找不到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem 索雷博相机（PI_CCD=thorlabs）：原生 DLL 目录必须在**启动 Python 之前**进 PATH。
rem 只在 Python 里 os.add_dll_directory() 不够 —— 实测那样 tl_camera_open_sdk() 会返回
rem error code 1，看着像"设备被占用"，其实是原生 SDK 自己的加载器找不到依赖 DLL。
if not defined TL_SDK_DLLS set "TL_SDK_DLLS=%USERPROFILE%\Desktop\pzt\Scientific_Camera_Interfaces\Scientific Camera Interfaces\SDK\Python Toolkit\dlls\64_lib"
if exist "%TL_SDK_DLLS%\thorlabs_tsi_camera_sdk.dll" set "PATH=%TL_SDK_DLLS%;%PATH%"

echo 界面地址：http://127.0.0.1:8000
echo 关闭本窗口即停止服务；位移台保持原位不动（要卸力请点界面上的"释放"）。
".venv\Scripts\python.exe" -m backend.server
pause
