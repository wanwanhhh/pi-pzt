"""CS165MU 上机自检：一次开 SDK、采一帧原生图、存 16 位 PNG、打出统计量。

用法（**先关掉 ThorCam**，相机同一时刻只能有一个占用者）：
    .venv\\Scripts\\python.exe tools\\hwtest_ccd.py
    .venv\\Scripts\\python.exe tools\\hwtest_ccd.py --exposure-us 50000 --preview
    .venv\\Scripts\\python.exe tools\\hwtest_ccd.py --frames 3 --out data\\ccd_test.png

规矩（这台相机的脾气，见聊天记录与 docs/thorlabs/设备认识账.xml）：
  * SDK 进程内只开一次、退出前只关一次；绝不在循环里反复开关
    —— 每次开/关都会让 CYUSB3 重新枚举 USB 设备，Windows 会"叮咚"，而且短窗口内
       别的进程再开会拿到 error code 1。
  * ROI 会被相机按硬件对齐改写（要 259x259 实际是 260x260），所以永远读回实际值。
  * 打印走 utf-8；退出用 os._exit(0) 绕开原生 SDK 偶发的退出挂起。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from png16 import save_png_16  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
# 本机 SDK 解压位置（TL_SDK_DLLS 优先）。跟 backend/config.py 的兜底同一个意思，不写死用户名。
DLL_CANDIDATES = [
    Path(os.environ.get("USERPROFILE") or Path.home())
    / "Desktop/pzt/Scientific_Camera_Interfaces/Scientific Camera Interfaces/SDK/Python Toolkit/dlls/64_lib",
]
RAMP = " .:-=+*#%@"


def find_dll_dir() -> Path:
    env = os.environ.get("TL_SDK_DLLS")
    if env and (Path(env) / "thorlabs_tsi_camera_sdk.dll").exists():
        return Path(env)
    for p in DLL_CANDIDATES:
        if (p / "thorlabs_tsi_camera_sdk.dll").exists():
            return p
    raise SystemExit("找不到 thorlabs_tsi_camera_sdk.dll，请设 TL_SDK_DLLS 指向 dlls\\64_lib")


def open_sdk(tries: int = 15, gap: float = 2.0):
    """带重试地打开 SDK：设备刚被别的进程动过时，开头几次会报 error code 1。"""
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

    for i in range(1, tries + 1):
        try:
            return TLCameraSDK()
        except Exception as exc:
            print(f"  第 {i}/{tries} 次打开失败：{str(exc).splitlines()[0]}")
            sys.stdout.flush()
            time.sleep(gap)
    raise SystemExit("SDK 打不开：确认 ThorCam 已关闭、相机没被别的脚本占着")


def ascii_preview(img, cols: int = 72) -> str:
    """把图缩成字符画：这一步是为了在终端里肉眼确认"到底有没有东西"。"""
    h, w = img.shape
    rows = max(6, int(cols * h / w * 0.5))          # 字符比像素高，压一半
    step_y, step_x = h // rows, w // cols
    lo, hi = int(img.min()), int(img.max())
    if hi <= lo:
        return f"（整幅都是同一个值 {lo}）"
    out = [f"  灰阶 {lo}~{hi}，下列字符按这个范围归一化："]
    for r in range(rows):
        line = []
        for c in range(cols):
            block = img[r * step_y:(r + 1) * step_y, c * step_x:(c + 1) * step_x]
            v = int(block.mean())
            line.append(RAMP[min(len(RAMP) - 1, (v - lo) * len(RAMP) // (hi - lo))])
        out.append("  " + "".join(line))
    return "\n".join(out)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--exposure-us", type=int, default=20000, help="曝光（µs），默认 20000")
    # 默认 0 而不是"不动"：这台相机的增益是**掉电保持**的（我上个脚本设成 480 档，
    # 后面几次运行没写增益，相机就一直用 480 —— 整个"过曝"的假象就是这么来的）。
    ap.add_argument("--gain", type=int, default=0, help="增益档位（相机单位 0~480，不是 dB）；默认 0")
    ap.add_argument("--tries", type=int, default=15, help="SDK 打开失败的重试次数（0 = 不重试，直接失败）")
    ap.add_argument("--roi", type=str, default="", help="WxH，默认原生全幅")
    ap.add_argument("--frames", type=int, default=1, help="连续采几帧")
    ap.add_argument("--out", type=str, default="", help="保存路径，默认 data/ccd_<时间戳>.png")
    ap.add_argument("--preview", action="store_true", help="在终端打一张字符画")
    args = ap.parse_args()

    dll_dir = find_dll_dir()
    print(f"Python {sys.version.split()[0]} / {platform.machine()} / {platform.system()}")
    print(f"DLL: {dll_dir}")
    os.add_dll_directory(str(dll_dir))
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ["PATH"]

    t0 = time.perf_counter()
    sdk = open_sdk(tries=max(1, args.tries))
    print(f"SDK 打开：{1000*(time.perf_counter()-t0):.0f} ms")
    try:
        serial = sdk.discover_available_cameras()[0]
        cam = sdk.open_camera(serial)
        print(f"相机 {serial}：{cam.model}")
        print(f"  ROI 请求 {'原生全幅' if not args.roi else args.roi} | "
              f"相机实际 {cam.image_width_pixels}x{cam.image_height_pixels}")

        if args.roi:
            w, h = (int(v) for v in args.roi.lower().split("x"))
            cam.roi = (0, 0, w, h)          # 设完必须读回：相机会按硬件对齐改写
            print(f"  设 ROI {w}x{h} → 相机改写为 {cam.image_width_pixels}x{cam.image_height_pixels}")
        print("  自动曝光相关属性:", [a for a in dir(cam) if "auto_exposure" in a.lower()] or "无")
        exp_range = cam.exposure_time_range_us
        print(f"  曝光范围 {exp_range.min}~{exp_range.max} us（固件下限 {exp_range.min} us）| "
              f"增益档范围 {cam.gain_range.min}~{cam.gain_range.max}")
        cam.exposure_time_us = args.exposure_us
        cam.gain = args.gain
        time.sleep(0.05)          # 设完读回，确认真的生效（掉电保持的东西不能靠"设了就算"）
        print(f"  曝光 {cam.exposure_time_us/1000:.3f} ms（请求 {args.exposure_us} us，被夹/取整就是实际值）"
              f" | 增益档 {cam.gain}")

        out = Path(args.out) if args.out else (REPO / "data" / f"ccd_{time.strftime('%Y%m%d_%H%M%S')}.png")
        out.parent.mkdir(parents=True, exist_ok=True)

        cam.frames_per_trigger_zero_for_unlimited = 0
        cam.image_poll_timeout_ms = 2000
        cam.arm(1)
        for i in range(args.frames):
            t0 = time.perf_counter()
            cam.issue_software_trigger()
            frame = cam.get_pending_frame_or_null()
            t_grab = time.perf_counter()
            if frame is None:
                print("  轮询超时，没拿到帧")
                break
            img = frame.image_buffer.copy()      # 必须 copy：这块内存在下次轮询会被覆写
            stat = (f"  帧 #{frame.frame_count}: shape={img.shape} dtype={img.dtype} "
                    f"min={img.min()} max={img.max()} mean={img.mean():.1f} "
                    f"中位={statistics.median(img.ravel()[::7]) :.1f} | 取帧 {1000*(t_grab-t0):.0f} ms")
            if args.frames == 1:
                size = save_png_16(out, img)
                t_save = time.perf_counter()
                sat = int((img >= 4095).sum()) * 100.0 / img.size
                print(stat)
                print(f"  饱和(≥4095) {sat:.2f}% 的像素 | 编码+落盘 {1000*(t_save-t_grab):.0f} ms "
                      f"| 已存 {out.relative_to(REPO) if out.is_relative_to(REPO) else out}（{size/1e6:.2f} MB）")
                # 这台相机的读出上限实测是 1022（12 位 ADC 经 SDK 出来就这个量程），
                # 拿 4095 当饱和门限是错的 —— 过曝的帧会被判成"没饱和"，白忙一场。
                hi = int((img == img.max()).sum()) * 100.0 / img.size
                if img.max() <= 16 and img.min() == img.max():
                    print("  ⚠ 整幅同一个值且接近 0：确认镜头盖/光路快门")
                elif hi > 5.0:
                    print(f"  ⚠ 过曝：{hi:.1f}% 的像素顶在最大值 {img.max()}（最高灰度不是 4095，"
                          f"别看错）→ 降曝光/降增益/加衰减片")
                elif img.min() == img.max():
                    print(f"  ⚠ 整幅都是 {img.max()}：完全没有对比度（不是光太多就是光太少）")
                elif sat > 1.0:
                    print(f"  ⚠ 有像素 ≥4095：降曝光或降增益")
                if args.preview:
                    print(ascii_preview(img))
            else:
                print(stat)
        cam.disarm()
        cam.dispose()
    finally:
        sdk.dispose()
        print("已释放（相机与 SDK 各一次，没反复开关）")
    sys.stdout.flush()
    os._exit(0)          # 原生 SDK 退出时偶发挂起，这里强制退


if __name__ == "__main__":
    raise SystemExit(main())
