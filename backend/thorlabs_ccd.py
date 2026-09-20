"""索雷博 CS165MU（Zelux）相机设备层。仅 Windows。

**一个进程里只有一个相机 owner 线程**，所有相机动作（开、关、改设置、取帧）都排队
进这一个线程执行 —— 跟位移台那条规矩一样，原生 SDK 不是线程安全的。

四条实测得来的硬约束，改这个文件前先读：

1. **原生 DLL 目录必须在启动 Python 之前进 PATH**。只在进程内
   `os.add_dll_directory()` 是不够的：实测那样 `tl_camera_open_sdk()` 会返回
   `error code 1`，而那个错误码的含义是"SDK 已被占用"，看着像设备被别的程序占着，
   实际是原生 SDK 自己的加载器找不到 `thorlabs_tsi_usb_driver.dll` 这类依赖。
   （在 Python 里改 `os.environ["PATH"]` 也没用，窗口已经过了。）
   没进 PATH 时本模块**直接拒绝启动并说明原因**，不让上层去猜那个错误码。

2. **SDK 进程内只开一次、退出前只关一次**。每次开/关都会让 CYUSB3 驱动重新枚举
   USB 设备（Windows 会"叮咚"），而且紧接着的其它进程会撞上 error code 1。
   所以：预展开一次、采图跟着预展、进程退出才关。

3. **增益和曝光都是掉电保持的**，进程退出后相机记着上次的值。每次连接都要显式写、
   并读回确认，绝不能假设默认值。踩过：上个脚本把增益拉到 480 档没还原，之后几轮
   全被误判成"光太强、要加衰减片"。

4. **ROI 会被硬件按对齐改写**（要 518x518 实际是 520x520），所有地方一律读回实际值。
   预览用小 ROI（相机端裁剪，帧率更高），**保存一律切回原生全幅**，一个像素都不裁。
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from .config import (
    IMAGE_DIR,
    TL_CAPTURE_TIMEOUT_S,
    TL_DLL_DIR,
    TL_EXPOSURE_US,
    TL_FULL_ROI,
    TL_GAIN,
    TL_JPEG_QUALITY,
    TL_OPEN_TIMEOUT_S,
    TL_PREVIEW_FPS,
    TL_PREVIEW_ROI,
    TL_PREVIEW_ROTATION,
    TL_SATURATION_ADU,
)

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """相机层错误。上层只管把话原样说给用户听。"""


_PRELOADED: dict = {}   # DLL 目录 → 是否已预加载（只用于少打重复日志）


def _require_dll_dir() -> str:
    """确认 DLL 目录并在本进程里把它们预加载好。

    关键的一步是**按全路径预加载厂商 DLL**：
      * 按短名加载会报 "Could not find module ... (or one of its dependencies)"，
        即使该目录已经在 PATH 里、也已经 os.add_dll_directory() 过；
      * 按全路径预加载之后，原生 SDK 内部再按短名要同一批 DLL 时，Windows 直接复用
        已加载的模块，问题消失（干净 PATH 下实测 19/19 成功，相机正常打开）。
    所以后端**不依赖**调用者先把目录塞进 PATH；run.bat 里设 PATH 是给工具脚本兜底的。
    """
    dll_dir = TL_DLL_DIR
    if not (Path(dll_dir) / "thorlabs_tsi_camera_sdk.dll").exists():
        raise CameraError(
            f"找不到 thorlabs_tsi_camera_sdk.dll（TL_SDK_DLLS={dll_dir}）。"
            "指向 SDK 包里的 dlls\\64_lib 目录。"
        )
    if _PRELOADED.get(dll_dir):
        return dll_dir
    try:
        os.add_dll_directory(dll_dir)
    except OSError as exc:
        raise CameraError(f"无法把 DLL 目录加进搜索路径（{dll_dir}）：{exc}") from exc

    failed = []
    for name in sorted(f for f in os.listdir(dll_dir) if f.lower().endswith(".dll")):
        try:
            ctypes.WinDLL(str(Path(dll_dir) / name))
        except OSError as exc:
            failed.append(f"{name}: {str(exc).splitlines()[0]}")
    _PRELOADED[dll_dir] = True
    if failed:
        log.warning("有 %d 个厂商 DLL 没预加载成功（单色相机用不到就不用管）：%s",
                    len(failed), "; ".join(failed[:3]))
    else:
        log.info("厂商 DLL 已按全路径预加载：%s", dll_dir)
    return dll_dir


class ThorlabsCamera:
    """CS165MU 的所有者：一个 owner 线程 + 一个任务队列。"""

    def __init__(
        self,
        dll_dir: str = TL_DLL_DIR,
        gain: int = TL_GAIN,
        exposure_us: int = TL_EXPOSURE_US,
        preview_roi: tuple = TL_PREVIEW_ROI,
        full_roi: tuple = TL_FULL_ROI,
        preview_fps: float = TL_PREVIEW_FPS,
        jpeg_quality: int = TL_JPEG_QUALITY,
    ) -> None:
        self._dll_dir = dll_dir
        self._gain = gain
        # **预览与采图共用一个曝光**：两边不一致的话，"预览里看着挺好"和"存下来的"
        # 就是两张亮度不同的图，对不上账。要调就一起调。
        self._exposure_us = exposure_us
        self._preview_roi = preview_roi
        self._full_roi = full_roi
        self._preview_fps = preview_fps
        self._jpeg_quality = jpeg_quality

        self._sdk = None
        self._cam = None
        self._serial = ""
        self._model = ""
        self._exposure_range = (0, 0)   # 相机自报的曝光范围（µs），打开时读一次
        self._opened_at = 0.0
        self._frames = 0                 # 预览累计帧数，用来判断"活没活"
        self._last_error = ""
        self._preview_on = False
        self._armed = False
        self._last_trigger = 0.0

        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._jpeg: Optional[bytes] = None
        self._centroid: Optional[dict] = None   # 最近一帧原生图的全局质心（不扣背景）
        # 预览显示朝向（0/90/180/270，顺时针）。**只影响预览**：保存永远写传感器原始朝向。
        # 环境变量写错（比如 45）只该退成 0 并说清楚 —— 一个显示偏好不该让相机对象建不起来，
        # 那会把 /api/ccd/status 变成一直 500，反而看不出是配置写错了。
        try:
            self._rotation = _norm_rotation(TL_PREVIEW_ROTATION)
        except CameraError as exc:
            log.error("PI_CCD_ROTATION 配置无效（%s），本次按 0°（传感器原始）走", exc)
            self._rotation = 0
        # 最近一次整帧采集：(ndarray, 实际曝光 us, ROI, 质心 dict（传感器坐标）)
        self._shot: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """开 owner 线程。**不碰相机** —— 相机在第一次要用时才打开。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="thorlabs-camera", daemon=True)
        self._thread.start()
        log.info("相机 owner 线程已启动（DLL %s）", self._dll_dir)

    def close(self, timeout: float = 10.0) -> None:
        """收工：让 owner 线程把相机和 SDK 各关一次。绝不在循环里反复开关。"""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ------------------------------------------------------------ 对外接口
    def latest_jpeg(self) -> Optional[bytes]:
        """取最近一帧预览（JPEG 字节）。不排队、不等设备 —— 没帧就返回 None。"""
        with self._lock:
            return self._jpeg

    def last_shot(self) -> Optional[tuple]:
        """最近一次整帧采集的原始数据 (ndarray, 曝光us, ROI)。给"保存原生帧"用。"""
        with self._lock:
            return self._shot

    def status(self) -> dict:
        with self._lock:
            return {
                "open": self._cam is not None,
                "serial": self._serial,
                "model": self._model,
                "frames": self._frames,
                "last_error": self._last_error,
                "exposure_us": self._exposure_us,
                "gain": self._gain,      # 固定 0，只读显示
                "gain_locked": True,
                "exposure_min_us": self._exposure_range[0],
                "exposure_max_us": self._exposure_range[1],
                "preview_roi": list(self._preview_roi),
                "full_roi": list(self._full_roi),
                "centroid": self._centroid,     # None = 还没有帧；预览停了就是最后一帧的残留
                "rotation": self._rotation,     # 预览朝向（保存的文件不受它影响）
                "saturation_adu": TL_SATURATION_ADU,   # 满量程：界面拿它写"峰值到多少算饱和"
                "opened_at": self._opened_at,
            }

    def preview(self, on: bool = True) -> dict:
        """开/关连续预览。关掉后相机仍然占着（只是不再取帧）。"""
        self._ensure_thread()
        return self._submit(("preview", bool(on)), timeout=TL_OPEN_TIMEOUT_S)

    def capture(self, scan_id: int, index: int, position_um: float) -> Optional[str]:
        """扫一点：切回原生全幅、采一帧、存 16 位 PNG，返回相对 DATA_DIR 的路径。

        走的是 ccd.Capture 契约，scanner 不关心这里怎么实现。
        存完自动切回预览 ROI，所以扫描中预览不会被永久改坏。
        """
        self._ensure_thread()
        job = ("capture", scan_id, index, position_um)
        return self._submit(job, timeout=TL_CAPTURE_TIMEOUT_S)

    # ------------------------------------------------------------ owner 线程
    def _ensure_thread(self) -> None:
        if self._thread is None:
            self.start()

    def _submit(self, job: tuple, timeout: float):
        if self._stop.is_set():
            raise CameraError("相机已关闭")
        box: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        self._jobs.put((job, box))
        self._wake.set()
        try:
            ok, payload = box.get(timeout=timeout)
        except queue.Empty:
            raise CameraError(f"相机任务超时（{timeout:.0f} s）：{job[0]}") from None
        if not ok:
            raise CameraError(str(payload))
        return payload

    def _loop(self) -> None:
        try:
            _require_dll_dir()
        except CameraError as exc:
            with self._lock:
                self._last_error = str(exc)
            log.error("相机不可用：%s", exc)
            return

        next_frame_at = 0.0
        while not self._stop.is_set():
            # 预览时用**短超时**，而且把"等到该取下一帧的时刻"直接当超时用：
            # 这两段等待是相加的 —— 各写 50 ms + 66 ms 的闸门，实测只剩 7 fps。
            if self._preview_on:
                timeout = max(0.0, min(0.05, next_frame_at - time.monotonic()))
            else:
                timeout = 0.3
            try:
                job, box = self._jobs.get(timeout=timeout)
            except queue.Empty:
                if self._preview_on and time.monotonic() >= next_frame_at:
                    try:
                        self._pump()
                    except Exception as exc:                  # 预览尽力而为：出错记下不崩
                        with self._lock:
                            self._last_error = f"{type(exc).__name__}: {exc}"
                        time.sleep(0.5)
                    next_frame_at = time.monotonic() + max(0.0, 1.0 / self._preview_fps)
                continue

            try:
                box.put((True, self._run(job)))
            except Exception as exc:
                log.warning("相机任务 %s 失败：%s", job[0], exc)
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                box.put((False, exc))

        self._teardown()

    def _run(self, job: tuple):
        kind = job[0]
        if kind == "preview":
            self._open()
            self._preview_on = job[1]
            if job[1]:
                self._apply(preview=True)
                self._pump()
            return self.status()
        if kind == "capture":
            return self._grab_full(*job[1:])
        if kind == "exposure":
            exposure_us = int(job[1])
            # 范围是相机自报的，所以要先把相机打开才谈得上校验（进硬件前的参数校验是安全边界）。
            # 顺序也要紧：**校验放在改 self._exposure_us 之前**，不合法就不留下一个假的记录值。
            self._open()
            lo, hi = self._exposure_range
            if not (lo <= exposure_us <= hi):
                raise CameraError(f"曝光 {exposure_us} µs 超出相机范围 {lo}~{hi} µs")
            self._exposure_us = exposure_us      # 预览与采图共用，所以记一个就够
            if self._preview_on and self._cam is not None:
                # **预览流正跑着：直接改，不停流、不 disarm、不补触发。**
                # 实测：set 3.4 ms、读回 1.6 ms，亮度 1~2 帧内就变（2000→8000 µs 时均值 62 → 248）。
                # 若为改一个数把停流再起，每次都要重新 arm + 300 ms 等待，实时调就没法做。
                self._set_exposure_now(exposure_us)
            return self.status()
        if kind == "save":
            # 用一个不落盘的编号采一帧：图像由调用方自己存（见 save_raw）
            return self._grab_full(0, 0, 0.0, save=False)
        raise CameraError(f"未知相机任务 {kind!r}")

    # ------------------------------------------------------------ 相机动作
    def _open(self) -> None:
        if self._cam is not None:
            return
        from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

        _require_dll_dir()          # 再确认一次：PATH 是启动前设的，进程内改不了
        t0 = time.perf_counter()
        serials = None
        last: Optional[Exception] = None
        # 只重试 3 次：固件刚被上一个占用者放开时开头几次会失败，但死等 15 s
        # 只会让前端的预览请求一起卡住，不如快速失败、把原因说清楚。
        for attempt in range(1, 4):
            try:
                self._sdk = TLCameraSDK()
                serials = self._sdk.discover_available_cameras()
                break
            except Exception as exc:
                last = exc
                if attempt < 3:
                    time.sleep(1.0)
        if serials is None:
            raise CameraError(
                f"打不开相机 SDK：{last}。确认 ThorCam 已关闭（相机同时只能有一个占用者）、"
                "相机没被别的脚本占着。"
            )
        if not serials:
            raise CameraError("SDK 能打开，但没发现相机：检查 USB 连接与相机电源。")

        self._serial = serials[0]
        self._cam = self._sdk.open_camera(self._serial)
        self._cam.frames_per_trigger_zero_for_unlimited = 0
        self._cam.image_poll_timeout_ms = 2000
        self._model = self._cam.model
        rng = self._cam.exposure_time_range_us
        self._exposure_range = (int(rng.min), int(rng.max))
        self._opened_at = time.time()
        log.info("相机已打开：%s %s（%.0f ms）", self._model, self._serial,
                 1000 * (time.perf_counter() - t0))

    def _set_exposure_now(self, exposure_us: int) -> None:
        """不打断流地写曝光并读回。用于预览中的实时调整。

        读回差得远就抛错 —— 那就是"设了没生效"，但**不能在这里停流**：
        停流会让固件那一帧超时，用户看到的是画面卡一下，比数值不对更迷惑。
        """
        cam = self._cam
        cam.exposure_time_us = exposure_us
        real = cam.exposure_time_us
        if abs(real - exposure_us) > max(50, exposure_us * 0.02):
            raise CameraError(f"曝光没生效：要 {exposure_us} µs，相机读回 {real} µs")
        log.debug("实时改曝光：%d µs（读回 %d）", exposure_us, real)

    def _apply(self, preview: bool) -> None:
        """停流、写设置、**读回确认** —— 增益和曝光是掉电保持的，不能假设。

        **必须先 disarm**：实测在采集中改 ROI，固件会直接拒绝
        （tl_camera_set_roi() → error code 1005 "Camera Running Error"）。
        """
        cam = self._cam
        # SDK 文档（tl_camera.py:918）明写：**发出软触发后至少等 300 ms 才能设曝光**。
        # 不等的话 set 不报错、但固件不认账 —— 实测就是这样：要 8000 us，读回还是预览的 2011 us。
        if self._last_trigger:
            wait = 0.3 - (time.monotonic() - self._last_trigger)
            if wait > 0:
                time.sleep(wait)
        if self._armed:
            cam.disarm()
            self._armed = False
        roi = self._preview_roi if preview else self._full_roi
        exposure = self._exposure_us      # 预览与采图同一个曝光
        cam.roi = roi                        # 设完读回：相机会按硬件对齐改写
        cam.exposure_time_us = exposure
        cam.gain = self._gain
        time.sleep(0.05)
        actual_roi = (cam.roi[0], cam.roi[1], cam.image_width_pixels, cam.image_height_pixels)
        if preview:
            self._preview_roi = actual_roi
        else:
            self._full_roi = actual_roi
        if cam.gain != self._gain:
            raise CameraError(f"增益没设上：要 {self._gain}，读回 {cam.gain}")
        # 曝光也要读回：掉电保持 + 固件按步进取整，设了不等于生效。
        # 只警告不抛：真出问题会在图像亮度上现形，而这里抛会把整条扫描打断。
        if abs(cam.exposure_time_us - exposure) > max(50, exposure * 0.02):
            log.warning("曝光没设上：要 %d us，读回 %d us（%s）",
                        exposure, cam.exposure_time_us, "预览" if preview else "采图")

    def _arm(self) -> None:
        cam = self._cam
        cam.arm(2)
        cam.issue_software_trigger()
        self._last_trigger = time.monotonic()

    def _next_frame(self):
        """拿一帧（uint16，是临时缓冲，调用方要用就得自己 copy）。"""
        frame = self._cam.get_pending_frame_or_null()
        if frame is None:
            self._cam.issue_software_trigger()      # 触发丢了或缓冲空了，补一发
            return None
        return frame

    def _pump(self) -> None:
        """预览取一帧：转 8 位、编码 JPEG、存成"最近一帧"。"""
        self._open()
        if not getattr(self, "_armed", False):
            self._apply(preview=True)
            self._arm()
            self._armed = True
        t0 = time.perf_counter()
        frame = self._next_frame()
        t_grab = time.perf_counter()
        if frame is None:
            return
        img = frame.image_buffer
        # 质心在**未旋转**的原生帧上算：它永远是**传感器坐标**，与保存的 PNG 同一套坐标，
        # 不受预览朝向影响（要拿读数去对文件里的像素，不用做任何换算）。
        # 预览 JPEG 只是投递用的，别拿它算数。
        centroid = global_centroid(img)
        # 预览朝向**只在这之后**作用于显示：np.rot90 是视图（不复制、不重采样，90° 整数倍是精确置换）。
        # 取 -k 是因为界面上的 90° 要**顺时针**（多数看图软件的习惯），np.rot90 默认是逆时针。
        rotation = self._rotation
        if rotation:
            import numpy as np      # 与本模块其它地方一样：用到才拉 numpy

            img = np.rot90(img, -(rotation // 90))
        jpeg = _to_jpeg(img, self._jpeg_quality)
        t_enc = time.perf_counter()
        with self._lock:
            self._jpeg = jpeg
            self._centroid = centroid
            self._frames += 1
        # 每 60 帧报一次各段耗时：全幅预览慢在哪要让日志说得清，别靠猜
        if self._frames % 60 == 1:
            log.info("预览 %dx%d：等帧 %.0f ms + 编码 %.0f ms = %.0f ms/帧（%d 字节）",
                     img.shape[1], img.shape[0], 1000 * (t_grab - t0), 1000 * (t_enc - t_grab),
                     1000 * (t_enc - t0), len(jpeg))

    def _grab_full(self, scan_id: int, index: int, position_um: float,
                   save: bool = True) -> Optional[str]:
        """切原生全幅采一帧存盘，再切回预览设置。"""
        self._open()
        armed_before = self._armed
        self._apply(preview=False)          # 内部会先 disarm，再切全幅、用采图曝光
        self._arm()
        # 曝光值要在**采帧之前**读：恢复预览设置会把相机上的曝光改回去，
        # 采完再读就把预览值当成了采图值记进元数据（踩过一次）。
        shot_exposure = self._cam.exposure_time_us
        img = None
        deadline = time.monotonic() + TL_CAPTURE_TIMEOUT_S
        while time.monotonic() < deadline:
            frame = self._next_frame()
            if frame is not None:
                img = frame.image_buffer.copy()   # 必须 copy：下一轮轮询会覆写这块内存
                break
            time.sleep(0.005)
        self._cam.disarm()
        if armed_before:                    # 预览还开着就恢复，别把预览弄死
            self._apply(preview=True)
            self._arm()
            self._armed = True
        if img is None:
            raise CameraError(f"第 {index} 点没采到帧（目标 {position_um:.4f} µm）")

        # 这一帧的质心（传感器坐标；口径与预览完全一样：不扣背景、不设阈值、不开窗）。
        # 同一帧只算一次 —— 存盘写进文件自己身上，save_raw 也拿这一份。
        cen = global_centroid(img)
        with self._lock:
            self._shot = (img, shot_exposure, self._full_roi, cen)
        if not save:
            log.info("采了一帧原生全幅（不落盘）：%dx%d，曝光 %d us，均值 %.1f",
                     img.shape[1], img.shape[0], shot_exposure, float(img.mean()))
            return None
        path = _image_path(scan_id, index)
        size = _save_png16(path, img, shot_exposure, cen)
        log.info("第 %d 点采图：%s（%dx%d，%d 字节，曝光 %d us，均值 %.1f）",
                 index, path.name, img.shape[1], img.shape[0], size, shot_exposure,
                 float(img.mean()))
        return f"images/{path.name}"

    def set_rotation(self, deg: int) -> dict:
        """改**预览显示朝向**（顺时针 0/90/180/270），立刻生效。

        这不是设备设置，也不碰数据：只是取帧后把它转过来显示（np.rot90 是视图，不重采样）。
        **保存的原生帧永远是传感器朝向**，**质心读数也永远是传感器坐标**（与文件同一套坐标）；
        转的只是"看的方向" —— 前端按朝向把十字线画到显示帧上，换算属于显示。
        """
        with self._lock:
            self._rotation = _norm_rotation(deg)
        return self.status()

    def set_exposure(self, exposure_us: int) -> dict:
        """改曝光（预览与采图**一起改**），立刻生效：预览开着时不停流直接改，1~2 帧内就变。

        **范围校验在这里**：进硬件前的参数校验是安全边界，不许只靠界面拦。

        设完之后采的每一帧，元数据里记的是**相机读回的实际曝光**（不是请求值）——
        图像必须自证用了哪次参数，免得事后没法比。
        """
        if exposure_us <= 0:
            raise CameraError("曝光必须是正数（µs）")
        # 相机已经开着就当场拒（省一次排队）；范围还没读到时由 _run 里那道校验兜底，
        # 那道才是权威的 —— 它持有相机自报的 min/max。
        lo, hi = self._exposure_range
        if lo and not (lo <= exposure_us <= hi):
            raise CameraError(f"曝光 {exposure_us} µs 超出相机范围 {lo}~{hi} µs")
        self._ensure_thread()
        return self._submit(("exposure", int(exposure_us)), timeout=TL_OPEN_TIMEOUT_S)

    def save_raw(self) -> dict:
        """采一帧**原生全幅**存 16 位 PNG，给界面上的「保存原生帧」用。

        与扫描采图走同一段代码（同样的曝光、同样的不裁剪），但不占扫描的编号，
        也不额外多做一次曝光 —— 采完的帧本来就在 last_shot 里。
        """
        self._ensure_thread()
        self._submit(("save",), timeout=TL_CAPTURE_TIMEOUT_S)
        shot = self.last_shot()
        if shot is None:
            raise CameraError("没有可保存的帧")
        img, exposure_us, roi, cen = shot
        # 名字只到秒；同一秒里连点两次要各落一张，所以撞了就往后加 _2、_3…
        # （库那边的登记是 upsert：不换名的话第二张会覆盖第一张的文件，界面却报"已保存"）
        stem = f"grab_{time.strftime('%Y%m%d_%H%M%S')}"
        name = f"{stem}.png"
        n = 2
        while (IMAGE_DIR / name).exists():
            name = f"{stem}_{n}.png"
            n += 1
        path = IMAGE_DIR / name
        size = _save_png16(path, img, exposure_us, cen)
        # **登记不在这里做**：本层在 store 之下，不许反向依赖（AGENTS.md 分层）。
        # 调用方（server 的 /api/ccd/capture）拿文件名去登记，图库只列登记过的。
        return {
            "path": f"images/{name}",
            "width": int(img.shape[1]),
            "height": int(img.shape[0]),
            "exposure_us": int(exposure_us),   # 相机读回值，不是请求值
            "centroid": [cen["cx"], cen["cy"]],   # 传感器坐标，与文件里写的是同一份
            "gain": int(self._gain),
            "roi": list(roi),
            "bytes": size,
            "mean": round(float(img.mean()), 1),
            "min": int(img.min()),
            "max": int(img.max()),
        }

    def _teardown(self) -> None:
        """收工：相机和 SDK 各关一次。"""
        if self._cam is not None:
            try:
                self._cam.dispose()
            except Exception as exc:
                log.warning("关相机失败：%s", exc)
            self._cam = None
        if self._sdk is not None:
            try:
                self._sdk.dispose()
            except Exception as exc:
                log.warning("关 SDK 失败：%s", exc)
            self._sdk = None
        log.info("相机与 SDK 已释放")


def _norm_rotation(deg: int) -> int:
    """朝向只接受 0/90/180/270（顺时针），别的直接拒绝 —— 不"取个近似值"糊过去。"""
    deg = int(deg) % 360
    if deg % 90:
        raise CameraError(f"预览朝向只能是 0/90/180/270 度，收到 {deg}")
    return deg


def global_centroid(img) -> dict:
    """整幅图的**强度加权重心**：cx = Σ(I·x)/ΣI，cy = Σ(I·y)/ΣI。

    **不做任何处理**：不扣背景、不设阈值、不开窗（这是用户定的口径）。所以它是
    "整幅图的亮度重心"，背景也照权重参与；光斑占总强度越小，它离光斑越远
    （实测：6 px 的小光斑放在 1440×1080、均值 400 ADU 的背景上，只占总强度 0.01%，
    光斑走 1 px 全局质心只动 0.0001 px —— 见 docs/thorlabs/设备认识账.xml）。

    算法上先按列/按行求和（int64，精确、不溢出），最后才除一次：
    比"把整幅乘上坐标网格"省一个十几 MB 的临时数组，整幅也就几毫秒。
    """
    import numpy as np

    arr = np.asarray(img)
    h, w = arr.shape
    col = arr.sum(axis=0, dtype=np.int64)
    row = arr.sum(axis=1, dtype=np.int64)
    total = int(col.sum())
    peak = int(arr.max())
    saturated = int((arr >= TL_SATURATION_ADU).sum())
    if total <= 0:
        return {"cx": None, "cy": None, "sum": 0, "peak": peak,
                "saturated": saturated, "width": int(w), "height": int(h)}
    cx = float((col * np.arange(w, dtype=np.int64)).sum() / total)
    cy = float((row * np.arange(h, dtype=np.int64)).sum() / total)
    return {"cx": cx, "cy": cy, "sum": total, "peak": peak,
            "saturated": saturated, "width": int(w), "height": int(h)}


# ---------------------------------------------------------------- 图像编码
def _thumb_cache_name(src: Path, max_side: int) -> str:
    """缩略图缓存键：文件名 + 修改时间 + 尺寸。

    **三样都不能少**，少了哪样都是静默错误：少了尺寸会让大图与小图互相顶掉
    （图库把 260 档当大图用），少了 mtime 则重存/改名之后还在看旧图。
    """
    return f"{src.stem}_{int(src.stat().st_mtime)}_{max_side}.jpg"


def thumb_jpeg(src: Path, max_side: int = 260) -> bytes:
    """把 PNG 转成 JPEG，给列表当缩略图 / 给弹窗看个大概。

    **只为显示**：16 位 PNG 一张 1~2 MB，列表里塞几十张会让浏览器一直解码大图。
    科学数据始终看原图（PNG），缩略图不参与任何测量。
    缓存落在 data/thumbs/，按"文件名 + 修改时间 + 尺寸"失效。

    降到 8 位的口径**按位深决定**（图库的原始帧是 16 位，扫描占位图是 8 位）：
    - 16 位：右移 2 位（本相机满量程实测 1022 → 8 位，与预览同一条口径）
    - 8 位：原样。一刀切右移会把 8 位图压暗 4 倍 —— 那不是"映射"，那是毁图。
    """
    from PIL import Image

    cache = IMAGE_DIR.parent / "thumbs" / _thumb_cache_name(src, max_side)
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    # **先读到 numpy、降到 8 位、再交给 PIL**：实测 16 位灰度 PNG 直接走
    # PIL 的 thumbnail()/point() 都会抛（L;16 模式没有对应实现）。
    import numpy as np

    arr = np.asarray(Image.open(src))
    if arr.dtype == np.uint16:
        arr = arr >> 2          # 满量程 1022 → 8 位（与预览同一条口径）
    elif arr.dtype != np.uint8:
        arr = arr.astype("uint8")
    img = Image.fromarray(arr.astype("uint8"), mode="L")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    cache.write_bytes(buf.getvalue())
    return buf.getvalue()


def _image_path(scan_id: int, index: int) -> Path:
    return IMAGE_DIR / f"scan{scan_id:04d}_{index:05d}.png"


def _to_jpeg(img, quality: int) -> bytes:
    """uint16 → 8 位 → JPEG。预览用，不做任何科学处理。"""
    import numpy as np
    from PIL import Image

    # 12 位满量程经 SDK 出来是 1022，>>2 落到 8 位；不是 >>4（那是 4095 的算法）
    buf = io.BytesIO()
    Image.fromarray(np.asarray(img >> 2, dtype="uint8"), mode="L").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


EXPOSURE_KEY = b"ExposureUs"      # 写进 PNG 的 tEXt 块：这一帧是用多少 µs 采的
CENTROID_KEY = b"CentroidPx"      # 同一个 tEXt：这一帧的质心 "cx,cy"（**传感器坐标**，与像素同一套）


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    import struct
    import zlib

    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def read_png_meta(path: Path) -> dict:
    """从 PNG 的 tEXt 块里读回这一帧自带的元数据：曝光（µs）与质心（像素）。

    写在文件**自己**身上：把 PNG 拷到别处、换个软件打开、甚至十年后翻出来，都还问得出来
    —— 不依赖本仓库的数据库。老图（加这个之前存的）没有这些块，两个字段都是 None
    （调用方按字段取，**不要拿 None 当 0**）。

    **只扫块头、不解码 IDAT**：1~2 MB 的文件读一遍就够，图库列几十张也不慢。
    """
    import struct

    out = {"exposure_us": None, "centroid": None}
    try:
        data = path.read_bytes()
    except OSError:
        return out
    pos = 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"tEXt":
            key, _, value = body.partition(b"\x00")
            if key == EXPOSURE_KEY:
                try:
                    out["exposure_us"] = int(value.decode("ascii"))
                except ValueError:
                    pass
            elif key == CENTROID_KEY:
                try:
                    cx, cy = value.decode("ascii").split(",")
                    out["centroid"] = [float(cx), float(cy)]
                except ValueError:
                    pass
        elif tag == b"IEND":
            break
        pos += 12 + length
    return out


def _save_png16(path: Path, img, exposure_us: Optional[int] = None,
                centroid: Optional[dict] = None) -> int:
    """16 位灰度 PNG，零依赖（与 tools/png16.py 同一实现，后端不能 import tools/）。

    给了就写进 tEXt 块：曝光（µs）与**质心**（"cx,cy"，传感器坐标）——
    让文件自带"这张图是怎么采的、亮心在哪"，不依赖数据库。
    质心在文件里只保留 2 位小数（够用且短），接口另外返回内存里那份全精度值 ——
    **要对数就拿文件里的**，那才是跟着图走的那份。
    """
    import zlib

    import struct

    h, w = img.shape
    raw = b"".join(b"\x00" + img[y].astype(">u2").tobytes() for y in range(h))

    text = b""
    if exposure_us is not None:
        text += _png_chunk(b"tEXt", EXPOSURE_KEY + b"\x00" + str(int(exposure_us)).encode("ascii"))
    if centroid and centroid.get("cx") is not None:
        pair = f"{centroid['cx']:.2f},{centroid['cy']:.2f}".encode("ascii")
        text += _png_chunk(b"tEXt", CENTROID_KEY + b"\x00" + pair)

    data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))
        + text
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    return len(data)
