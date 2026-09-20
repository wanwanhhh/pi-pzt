@echo off
rem 带索雷博 CS165MU 相机的启动方式（PI_CCD=thorlabs）。
rem 没有相机、只想跑位移台时用 run.bat 就行。
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem 启用真相机
set PI_CCD=thorlabs

rem SDK 原生 DLL 目录。改过 SDK 解压位置就改这一行（或先设好 TL_SDK_DLLS 再跑本脚本）。
if not defined TL_SDK_DLLS set "TL_SDK_DLLS=%USERPROFILE%\Desktop\pzt\Scientific_Camera_Interfaces\Scientific Camera Interfaces\SDK\Python Toolkit\dlls\64_lib"
if exist "%TL_SDK_DLLS%\thorlabs_tsi_camera_sdk.dll" (
    set "PATH=%TL_SDK_DLLS%;%PATH%"
) else (
    echo 警告：在 %TL_SDK_DLLS% 找不到 thorlabs_tsi_camera_sdk.dll
    echo       相机接口会报错，位移台不受影响。请设 TL_SDK_DLLS 指向 SDK 的 dlls\64_lib。
)

if not exist ".venv\Scripts\python.exe" (
    echo 找不到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 界面地址：http://127.0.0.1:8000
echo 相机：CS165MU（预览见 /api/ccd/preview.jpg，扫描中预览会被拒）
echo 关闭本窗口即停止服务；位移台保持原位不动（要卸力请点界面上的"释放"）。
".venv\Scripts\python.exe" -m backend.server
pause
