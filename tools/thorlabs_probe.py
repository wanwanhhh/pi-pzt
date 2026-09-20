"""CS165MU(Zelux) 联通性探针：确认 SDK 能装载 DLL、能发现相机、能抓一帧。

用法（先关掉 ThorCam）：
    .venv\\Scripts\\python.exe tools\\thorlabs_probe.py                 # 只发现
    .venv\\Scripts\\python.exe tools\\thorlabs_probe.py --grab 1        # 抓 1 帧
    .venv\\Scripts\\python.exe tools\\thorlabs_probe.py --grab 1 --save out.png

DLL 目录按顺序找：环境变量 TL_SDK_DLLS → 本机两个已知解压/安装位置。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from png16 import save_png_16  # noqa: E402  同目录公共实现，别再抄一份

# 本机 SDK 解压位置（TL_SDK_DLLS 优先）。跟 backend/config.py 的兜底同一个意思，不写死用户名。
DLL_CANDIDATES = [
    Path(os.environ.get("USERPROFILE") or Path.home())
    / "Desktop/pzt/Scientific_Camera_Interfaces/Scientific Camera Interfaces/SDK/Python Toolkit/dlls/64_lib",
]

# thorlabs_tsi_camera_sdk.h 里的错误码
ERROR_CODES = {
    0: "TL_ERROR_OK",
    1: "TL_ERROR_SDK_ALREADY_OPEN —— SDK 已被占用：同一进程里开了两个实例，"
       "或别的程序（ThorCam / 上次没退干净的脚本）还攥着相机",
    2: "TL_ERROR_INVALID_HANDLE",
    3: "TL_ERROR_DEVICE_NOT_FOUND",
    4: "TL_ERROR_DEVICE_ALREADY_OPEN",
    5: "TL_ERROR_DEVICE_NOT_SUPPORTED",
    6: "TL_ERROR_INVALID_ARGUMENT",
    7: "TL_ERROR_SDK_NOT_INITIALIZED",
}


def find_dll_dir() -> Path:
    env = os.environ.get("TL_SDK_DLLS")
    if env and (Path(env) / "thorlabs_tsi_camera_sdk.dll").exists():
        return Path(env)
    for p in DLL_CANDIDATES:
        if (p / "thorlabs_tsi_camera_sdk.dll").exists():
            return p
    raise SystemExit("找不到 thorlabs_tsi_camera_sdk.dll，请设 TL_SDK_DLLS 指向 dlls\\64_lib")


def raw_discover(sdk_path: Path) -> tuple[int, str]:
    """裸 ctypes 走一遍 SDK，报出确切错误码（Python 包装会把它变成一句无信息的话）。"""
    sdk = ctypes.WinDLL(str(sdk_path))
    code = sdk.tl_camera_open_sdk()
    if code != 0:
        return code, ""
    buf = ctypes.create_string_buffer(4096)
    if sdk.tl_camera_discover_available_cameras(buf, len(buf)) != 0:
        buf.value = b""
    serials = buf.value.decode(errors="replace")
    sdk.tl_camera_close_sdk()      # 不关掉的话，后面 Python 包装层拿到的就是 1004
    return 0, serials


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # 免得中文在 GBK 控制台里变成乱码
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", type=int, default=0, help="抓几帧")
    ap.add_argument("--exposure-us", type=int, default=20000)
    ap.add_argument("--save", type=str, default="", help="保存第一帧为 16 位 PNG")
    args = ap.parse_args()

    dll_dir = find_dll_dir()
    print(f"Python {sys.version.split()[0]} / {platform.machine()} / {platform.system()}")
    print(f"DLL 目录: {dll_dir}")
    os.add_dll_directory(str(dll_dir))
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ["PATH"]

    # 1) 先逐个 dlopen，把「缺哪个依赖」暴露出来，而不是笼统报错
    for name in ("thorlabs_tsi_camera_sdk.dll", "thorlabs_tsi_usb_driver.dll",
                 "thorlabs_tsi_zelux_camera_device.dll"):
        try:
            ctypes.WinDLL(str(dll_dir / name))
            print(f"  [ok]   {name}")
        except OSError as e:
            print(f"  [FAIL] {name}: {e}")

    # 2) 裸 SDK 发现（错误码看得见）
    code, serials = raw_discover(dll_dir / "thorlabs_tsi_camera_sdk.dll")
    if code != 0:
        print(f"tl_camera_open_sdk() -> {code}: {ERROR_CODES.get(code, '未知错误码')}")
        return 2
    print(f"发现相机(裸SDK): {serials or '（无）'}")

    # 3) Python 包装层：这才是业务代码要用的接口
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

    sdk = TLCameraSDK()
    try:
        serial_list = sdk.discover_available_cameras()
        print(f"发现相机(Python SDK): {serial_list or '（无）'}")
        if not serial_list:
            print("提示: 相机被别的进程占用时这里会是空的 —— 关掉 ThorCam / 别的采集脚本再试")
            return 2
        for serial in serial_list:
            with sdk.open_camera(serial) as cam:
                exp = cam.exposure_time_range_us
                print(f"  {serial}: {cam.model} | ROI {cam.image_width_pixels}x{cam.image_height_pixels} "
                      f"| 曝光 {exp.min}~{exp.max} us | 增益档 {cam.gain_range.min}~{cam.gain_range.max}")
                if args.grab:
                    _grab(cam, args)
    finally:
        sdk.dispose()
    return 0


def _grab(cam, args) -> None:
    cam.exposure_time_us = args.exposure_us
    cam.frames_per_trigger_zero_for_unlimited = 0   # 0 = 连续模式
    cam.image_poll_timeout_ms = 2000
    cam.arm(2)                                      # 2 帧缓冲
    cam.issue_software_trigger()
    got = 0
    while got < args.grab:
        frame = cam.get_pending_frame_or_null()
        if frame is None:
            print("  轮询超时，没拿到帧")
            break
        got += 1
        img = frame.image_buffer
        print(f"  帧 #{frame.frame_count}: shape={img.shape} dtype={img.dtype} "
              f"min={img.min()} max={img.max()} mean={img.mean():.1f}")
        if args.save and got == 1:
            save_png_16(Path(args.save), img)
            print(f"  已保存 {args.save}")
    cam.disarm()


if __name__ == "__main__":
    raise SystemExit(main())
