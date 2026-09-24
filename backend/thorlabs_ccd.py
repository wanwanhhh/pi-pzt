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
    TL_OPEN_WAIT_S,
    TL_SCAN_OPEN_WAIT_S,
    TL_PREVIEW_FPS,
    TL_PREVIEW_ROI,
    TL_PREVIEW_ROTATION,
    TL_SATURATION_ADU,
)

log = logging.getLogger(__name__)


def _sdk_class():
    """拿到厂家 SDK 的入口类。单开一层：离线测试把它换成假的，不用碰 DLL。"""
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

    return TLCameraSDK


def _roi_ok(roi) -> bool:
    """ROI 必须是非负起点 + 正尺寸。

    掉线时 SDK 会回**负数**（实测：tl_camera_set_roi() error 1003「A parameter is negative」）——
    这种值一旦被缓存下来，之后每次 _apply 都拿它去设 ROI，换句柄也救不回来。
    """
    try:
        x, y, w, h = (int(v) for v in roi)
    except (TypeError, ValueError):
        return False
    return x >= 0 and y >= 0 and w > 0 and h > 0


class CameraError(RuntimeError):
    """相机层错误：设备/会话级。出这种错就把会话丢掉。"""


class CameraInputError(CameraError):
    """输入校验错误（曝光越界、朝向非法…）：不影响会话，只是这一次请求被拒。

    owner 线程的规则是「除它以外任何异常都说明会话不可信、要丢掉」，所以「我们自己拦下的
    参数错误」必须有独立类型 —— 否则填错一个曝光值就会把好好的相机会话扔掉。
    """
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


class _Session:
    """一次 open_camera 产生的一切 —— **外加它用的那个 SDK 实例**。

    为什么 SDK 也算会话的一部分：实测（tools/thorlabs_replug_probe.py，2026-09-23）
    拔掉再插回之后，旧 SDK 实例的 discover **还看得见设备**，但在它上面 open_camera 会
    触发原生 access violation —— **进程直接死，Python 层兜不住**。所以「重建」必须是
    句柄 + SDK 一起重建；而旧 SDK 的 dispose 是安全的（实测 2 ms）。

    会话之外不留任何「上一次打开」的残留：ROI / armed / 帧 / 质心 / 时间戳全在这里，
    会话一丢就一起没 —— 掉线时读回的垃圾值因此不可能污染下一次打开（旧代码就是被
    `(200001, 29184, -200000, 1)` 这种值毒死 ROI 缓存的）。
    """

    def __init__(self, sdk, cam, serial: str, roi_cfg: tuple) -> None:
        self.sdk = sdk
        self.cam = cam
        self.serial = serial
        self.model = cam.model
        rng = cam.exposure_time_range_us
        self.exposure_min_us, self.exposure_max_us = int(rng.min), int(rng.max)
        self.preview_roi, self.full_roi = roi_cfg
        self.armed = False
        self.last_trigger = 0.0
        self.jpeg: Optional[bytes] = None
        self.centroid: Optional[dict] = None
        self.live: Optional[object] = None    # 原生 16 位快照（轮廓图用）
        self.shot: Optional[tuple] = None     # 最近一次整帧采集
        self.frames = 0
        self.last_frame_at = 0.0
        self.opened_at = time.time()


class ThorlabsCamera:
    """CS165MU 的所有者：一个 owner 线程 + 一个任务队列。

    长期字段只有三类（其余一切都在 `_Session` 里，会话丢了就没了）：

      1. **人想要什么**：曝光 / 增益 / 朝向 / 预览意图（`_preview_wanted`）/ 扫描持有（`_hold`）
      2. **最近一次失败**：`_failure`（空 = 没失败过；非空 = 需要人点「重开相机」）
      3. **当前会话**：`_sess`（None = 现在没有相机句柄，任何路径都碰不到 SDK）

    规则三条：
      R1 会话只在「使用窗口」内存在（窗口 = 预览意图 ∪ 扫描持有 ∪ 一次动作进行中）；
      R2 任何异常、任何不合理的读回 → **丢弃会话**（句柄 + SDK 一起），记 failure，不重试；
      R3 没有会话 ⇒ 不可能有垃圾值、不可能调 SDK —— 这是结构保证，不靠记标志位。
    """

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
        self._preview_fps = preview_fps
        self._jpeg_quality = jpeg_quality
        # 配置里的 ROI 是**出厂值**：每个新会话都从它开始。当前 ROI 只存在于会话里，
        # 所以不存在「被掉线时的垃圾读回污染」这回事（旧代码的 1003 就是这么来的）。
        self._roi_cfg = (tuple(preview_roi), tuple(full_roi))
        # 预览显示朝向（0/90/180/270，顺时针）。**只影响预览**：保存永远写传感器原始朝向。
        # 环境变量写错（比如 45）只该退成 0 并说清楚 —— 一个显示偏好不该让相机对象建不起来，
        # 那会把 /api/ccd/status 变成一直 500，反而看不出是配置写错了。
        try:
            self._rotation = _norm_rotation(TL_PREVIEW_ROTATION)
        except CameraError as exc:
            log.error("PI_CCD_ROTATION 配置无效（%s），本次按 0°（传感器原始）走", exc)
            self._rotation = 0

        # ---- 长期字段：人想要什么 / 最近一次失败 / 当前会话 ----
        self._preview_wanted = False   # 人想要预览吗（不是「现在在不在取帧」）
        self._hold = 0                 # 扫描持有计数：>0 时相机归扫描用
        self._sess: Optional[_Session] = None
        self._failure = ""             # 会话为什么没了（空 = 没失败过）
        self._opening = False          # owner 线程正在建会话（只给状态用）

        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
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
        """最近一帧预览（JPEG）。**只有「预览要着、会话还在、帧还新鲜」时才给**，否则 None。

        帧挂在会话上：没有会话就没有帧可发（掉线后不可能拿上一帧冒充实时 —— 实测界面
        被这么骗过：以为「重开没用」，其实早就没在出帧）。新鲜度按曝光/帧周期算，
        不写死秒数（曝光可以到几十秒，写死 1 s 会把长曝光的正常预览判成掉线）。
        """
        sess = self._sess
        if sess is None or not self._preview_wanted or self._hold:
            return None
        if time.monotonic() - sess.last_frame_at > self._frame_budget():
            return None
        with self._lock:
            return sess.jpeg

    def _frame_budget(self) -> float:
        """多久没出新帧就算「这一帧不能代表现在」：跟着曝光与帧周期走，不写死秒数。"""
        return max(2.0 * self._exposure_us / 1e6, 2.0 / max(0.1, self._preview_fps)) + 1.0

    def state(self) -> str:
        """派生状态：off / idle / opening / preview / held / failed（off 由 HTTP 层判后端）。"""
        if self._sess is not None:
            return "held" if self._hold else "preview"
        if self._opening:
            return "opening"
        return "failed" if self._failure else "idle"

    def status(self) -> dict:
        """给界面看的快照。每个字段要么来自当前会话、要么来自意图、要么来自 failure。"""
        sess = self._sess
        with self._lock:
            failure = self._failure
        return {
            "state": self.state(),
            "open": sess is not None,
            "serial": sess.serial if sess else "",
            "model": sess.model if sess else "",
            "frames": sess.frames if sess else 0,
            "failure": failure,          # 非空 = 上一次为什么没了；点「重开相机」
            "preview_wanted": self._preview_wanted,
            "exposure_us": self._exposure_us,
            "gain": self._gain,          # 固定 0，只读显示
            "gain_locked": True,
            "exposure_min_us": sess.exposure_min_us if sess else 0,
            "exposure_max_us": sess.exposure_max_us if sess else 0,
            "preview_roi": list(sess.preview_roi if sess else self._roi_cfg[0]),
            "full_roi": list(sess.full_roi if sess else self._roi_cfg[1]),
            "centroid": sess.centroid if sess else None,
            "rotation": self._rotation,   # 预览朝向（保存的文件不受它影响）
            "saturation_adu": TL_SATURATION_ADU,
            "opened_at": sess.opened_at if sess else 0.0,
        }

    def preview(self, on: bool = True, wait_s: float = TL_OPEN_WAIT_S) -> dict:
        """开/关连续预览。**预览意图是长期字段**：关掉之后会话就收掉（窗口结束）。"""
        self._ensure_thread()
        return self._submit(("preview", bool(on), wait_s), timeout=TL_OPEN_TIMEOUT_S + wait_s)

    def reopen(self, wait_s: float = TL_OPEN_WAIT_S) -> dict:
        """重开相机：丢掉现在这个会话（句柄 + SDK），再建一个新的。**手动功能**。

        实测（tools/thorlabs_replug_probe.py）：掉线后旧 SDK 实例的 discover 还看得见设备，
        但在它上面 open_camera 会触发原生 access violation、**进程直接死** —— 所以这里必须
        连 SDK 一起重建，不能只换句柄。等待预算给「刚插上、USB 还在枚举」留时间。
        """
        self._ensure_thread()
        return self._submit(("reopen", wait_s), timeout=TL_OPEN_TIMEOUT_S + wait_s)

    def begin_scan(self, timeout: float = TL_OPEN_TIMEOUT_S + TL_SCAN_OPEN_WAIT_S) -> dict:
        """扫描接手相机：持有会话、预览让位、**把采图设置配好并 arm 起来**（状态变 held）。

        配置只在这里做一次：全幅 + 采图曝光 + 增益，然后 arm。之后每点只有
        「补一发软触发 → 取帧」—— 从前每点都 disarm/配置/arm 一遍，实测那一段要 ~585 ms，
        而预览那条路早就证明"arm 一次、每帧补触发"是可行的（全幅 34.8 fps）。
        """
        self._ensure_thread()
        return self._submit(("begin_scan",), timeout=timeout)

    def end_scan(self, timeout: float = TL_OPEN_TIMEOUT_S) -> dict:
        """扫描交还相机：还想要预览就接着取帧，不要就把会话收掉。"""
        self._ensure_thread()
        return self._submit(("end_scan",), timeout=timeout)

    def trigger(self) -> None:
        """补一发软触发：**曝光从这一刻开始**。

        scanner 在读到位置之前先调它，于是那次串口读数（~60 ms）落在曝光窗里，
        不另占时间。相机没 arm 就现 arm（幂等），会话没了就抛 —— 扫描中相机出错整条 failed。
        """
        self._ensure_thread()
        self._submit(("trigger",), timeout=TL_CAPTURE_TIMEOUT_S)

    def capture(self, scan_id: int, index: int, position_um: float) -> tuple[Optional[str], Optional[int]]:
        """取这一点的帧（触发由 trigger() 发过）并存 16 位 PNG。

        返回 (相对 DATA_DIR 的路径, 这一帧采图时相机上的曝光 µs)。
        走的是 ccd.Capture 契约，scanner 不关心这里怎么实现。
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
                self._failure = str(exc)     # DLL 都装不上：状态就是 failed，等人修
            log.error("相机不可用：%s", exc)
            return

        next_frame_at = 0.0
        while not self._stop.is_set():
            # 取帧节拍：只有「预览要着 + 有会话 + 不归扫描」时才按帧率醒来；
            # 这两段等待是相加的，各写 50 ms + 66 ms 的闸门实测只剩 7 fps。
            pumping = self._preview_wanted and self._sess is not None and not self._hold
            timeout = (max(0.0, min(0.05, next_frame_at - time.monotonic()))
                       if pumping else 0.3)
            try:
                job, box = self._jobs.get(timeout=timeout)
            except queue.Empty:
                next_frame_at = self._maybe_pump(next_frame_at)
                continue

            try:
                box.put((True, self._run(job)))
            except Exception as exc:
                log.warning("相机任务 %s 失败：%s", job[0], exc)
                self._after_job_error(str(job[0]), exc)
                box.put((False, exc))

        self._discard("退出", failed=False)   # 收工：会话（句柄 + SDK）一起丢

    def _run(self, job: tuple):
        kind = job[0]
        if kind == "preview":
            on, wait_s = bool(job[1]), float(job[2])
            self._preview_wanted = on
            if on:
                sess = self._ensure_session(wait_s)
                self._apply(sess, preview=True)
                self._arm(sess)
                self._pump(sess)
            elif not self._hold:
                self._discard("预览已停", failed=False)   # 窗口结束：会话收掉
            return self.status()
        if kind == "reopen":
            wait_s = float(job[1])
            log.info("重开相机（手动）：丢掉会话（句柄 + SDK）再建；上次失败：%s",
                     self._failure or "无")
            self._discard("重开相机", failed=False)
            # 按钮就在预览页上，语义是「我要重新用它」：把预览意图置上，重开完直接出画面。
            # （掉线前预览本来是开着的，_preview_wanted 也一直是 True，这条只补「空闲时点重开」那种情况）
            self._preview_wanted = True
            sess = self._ensure_session(wait_s)
            self._apply(sess, preview=True)
            self._arm(sess)
            self._pump(sess)
            return self.status()
        if kind == "begin_scan":
            self._hold += 1
            sess = self._ensure_session(TL_SCAN_OPEN_WAIT_S)
            # **配置只在这一次**：整个扫描期间相机就停在全幅 + 采图曝光上，一直 arm 着。
            # 从前是每点配一遍（disarm → 设 ROI/曝光/增益 → 读回 → arm），实测 ~585 ms/点，
            # 全是白花的 —— 扫描期间相机本来就归扫描独占，中途没人会改它。
            t0 = time.perf_counter()
            self._apply(sess, preview=False)
            self._arm(sess)
            log.info("扫描接手相机：全幅配置一次 %.0f ms（%dx%d，曝光 %d us）—— 之后每点只补触发",
                     1000 * (time.perf_counter() - t0), sess.full_roi[2], sess.full_roi[3],
                     sess.cam.exposure_time_us)
            return self.status()
        if kind == "end_scan":
            self._hold = max(0, self._hold - 1)
            sess = self._sess
            if sess is not None and sess.armed:
                # 先退出触发模式：扫描结束不该把相机留在 armed 上（预览要不要另说）
                sess.cam.disarm()
                sess.armed = False
            if sess is not None and not self._hold:
                if self._preview_wanted:
                    self._apply(sess, preview=True)
                    self._arm(sess)
                else:
                    self._discard("扫描结束、没人要预览", failed=False)
            return self.status()
        if kind == "trigger":
            sess = self._sess
            if sess is None:
                raise CameraError("相机没有会话（掉线？）—— 扫描中止")
            if not sess.armed:
                self._arm(sess)                  # arm 掉过就补上（幂等）
            else:
                sess.cam.issue_software_trigger()
                sess.last_trigger = time.monotonic()
            return None
        if kind == "capture":
            return self._grab_frame(*job[1:])
        if kind == "exposure":
            exposure_us = int(job[1])
            # 曝光范围是相机自报的 —— 要校验就得有会话（进硬件前的参数校验是安全边界）。
            # 本来没会话（预览关着）时，这次校验用的会话属于「一次性动作」，动作完就收掉。
            one_shot = self._sess is None
            sess = self._ensure_session(TL_OPEN_WAIT_S if one_shot else 0.0)
            try:
                if not (sess.exposure_min_us <= exposure_us <= sess.exposure_max_us):
                    raise CameraInputError(
                        f"曝光 {exposure_us} µs 超出相机范围 "
                        f"{sess.exposure_min_us}~{sess.exposure_max_us} µs")
                self._exposure_us = exposure_us   # 预览与采图共用，所以记一个就够
                if self._preview_wanted and not self._hold:
                    # **预览流正跑着：直接改，不停流、不 disarm、不补触发。**
                    # 实测：set 3.4 ms、读回 1.6 ms，亮度 1~2 帧内就变；停流再起每次要重新
                    # arm + 300 ms 等待，实时调就没法做。
                    self._set_exposure_now(sess, exposure_us)
            finally:
                if one_shot and self._sess is sess and not self._preview_wanted \
                        and not self._hold:
                    self._discard("改完曝光就收工", failed=False)
            return self.status()
        if kind == "rotation":
            self._rotation = _norm_rotation(int(job[1]))   # 输入校验：非法值抛 CameraInputError
            return self.status()
        if kind == "save":
            # 用一个不落盘的编号采一帧：图像由调用方自己存（见 save_raw）
            self._capture_once()
            sess = self._sess
            return {"shot": sess.shot if sess else None}   # 侧信道（_last_shot）删掉了
        raise CameraInputError(f"未知相机任务 {kind!r}")

    # ------------------------------------------------------------ 会话
    def _ensure_session(self, wait_s: float = 0.0) -> _Session:
        """拿到当前会话；没有就建一个（唯一建会话的地方）。

        wait_s > 0：等设备出现（最多这么久）再放弃 —— 拔插之后 USB 要重新枚举，人手刚插上
        就点按钮时第一次枚举不到不代表相机不在。
        """
        if self._sess is not None:
            return self._sess
        self._opening = True
        try:
            sess = self._open_session(wait_s)
        finally:
            self._opening = False
        self._sess = sess
        self._failure = ""             # 建起来了：上一次的失败不再是当前状态
        return sess

    def _open_session(self, wait_s: float) -> _Session:
        """建一个新会话：SDK 实例 + 相机句柄，读一次型号/曝光范围/ROI 出厂值。

        **SDK 只建一次**：它是进程级单例，建第二个会抛 "TLCameraSDK is already in use"
        （旧代码在重试循环里反复 new，于是第一次 discover 为空之后就永远失败、还报成
        「打不开相机 SDK…确认 ThorCam 已关闭」）。等设备只用重跑 discover。
        """
        _require_dll_dir()          # 再确认一次：PATH 是启动前设的，进程内改不了
        t0 = time.perf_counter()
        sdk = _sdk_class()()
        try:
            deadline = time.monotonic() + max(0.0, wait_s)
            while True:
                serials = sdk.discover_available_cameras()
                if serials:
                    break
                if time.monotonic() >= deadline:
                    raise CameraError(
                        "没发现相机：检查 USB 连接与相机电源（刚插上时等一两秒再点一次）。")
                time.sleep(0.5)     # 等它枚举出来
            serial = serials[0]
            cam = sdk.open_camera(serial)
        except BaseException:
            # 建不起来就别把 SDK 实例留着（闩锁 + 占着设备）
            try:
                sdk.dispose()
            except Exception as exc:                      # noqa: BLE001
                log.warning("放弃会话时关 SDK 失败：%s（之后可能只能重启后端）", exc)
            raise
        cam.frames_per_trigger_zero_for_unlimited = 0
        cam.image_poll_timeout_ms = 2000
        sess = _Session(sdk, cam, serial, self._roi_cfg)
        log.info("相机会话已建立：%s %s（%.0f ms）", sess.model, sess.serial,
                 1000 * (time.perf_counter() - t0))
        return sess

    def _discard(self, why: str, failed: bool = True) -> None:
        """终结当前会话：**句柄和它用的 SDK 实例一起丢**。

        为什么 SDK 也要丢：实测（tools/thorlabs_replug_probe.py）拔插之后，旧 SDK 实例的
        discover 还看得见设备，但在它上面 open_camera 会触发原生 access violation ——
        进程直接死。所以「只换句柄、留着 SDK」是不行的；重建必须两者一起重建。
        failed=True 时把原因记进 _failure（界面据此显示「点重开相机」）。
        """
        sess, self._sess = self._sess, None
        if sess is None:
            return
        for label, obj in (("句柄", sess.cam), ("SDK 实例", sess.sdk)):
            try:
                obj.dispose()
            except Exception as exc:                      # noqa: BLE001
                # SDK 的 dispose 失败会让它的类级闩锁一直为 True —— 之后再建实例会抛
                # "already in use"，那种情况只能重启后端。如实记下来，不假装还能重开。
                log.warning("关%s失败：%s", label, exc)
        if failed and not self._stop.is_set():
            with self._lock:
                self._failure = why
        log.warning("相机会话已丢弃（%s）：%s", "失败" if failed else "正常收工", why)

    def _after_job_error(self, kind: str, exc: BaseException) -> None:
        """任务抛异常之后怎么办：**只有「我们自己拦下的输入错误」留着会话，其余一律丢弃。**

        判据就这一条，不按异常类型逐个列举（旧代码分了两套谓词、还都不完备）：DLL 报错、
        垃圾值引出的 TypeError、属性异常……都说明这个会话已经不可信，丢掉（句柄 + SDK）、
        记 failure、等人点「重开相机」。
        """
        if not isinstance(exc, CameraInputError):
            self._discard(f"{kind} 失败：{exc}", failed=True)

    def _maybe_pump(self, next_frame_at: float) -> float:
        """取帧节拍：该取就取一帧；帧停了太久就判这个会话不可信，丢掉。"""
        sess = self._sess
        if sess is None or not self._preview_wanted or self._hold:
            return next_frame_at
        now = time.monotonic()
        if sess.frames and now - sess.last_frame_at > self._frame_budget():
            # 预算跟着曝光/帧周期走：长曝光不会被误判（旧代码写死 1 s，>1 s 曝光必误报）
            self._discard(
                f"预览 {now - sess.last_frame_at:.1f}s 没出新帧（相机掉线？）", failed=True)
            return now
        if now < next_frame_at:
            return next_frame_at
        try:
            self._pump(sess)
        except Exception as exc:                          # noqa: BLE001
            self._discard(f"取帧失败：{exc}", failed=True)
        return time.monotonic() + max(0.0, 1.0 / self._preview_fps)

    def _set_exposure_now(self, sess: _Session, exposure_us: int) -> None:
        """不打断流地写曝光并读回。用于预览中的实时调整。

        读回差得远就抛错 —— 那就是"设了没生效"，但**不能在这里停流**：
        停流会让固件那一帧超时，用户看到的是画面卡一下，比数值不对更迷惑。
        """
        cam = sess.cam
        cam.exposure_time_us = exposure_us
        real = cam.exposure_time_us
        if abs(real - exposure_us) > max(50, exposure_us * 0.02):
            raise CameraError(f"曝光没生效：要 {exposure_us} µs，相机读回 {real} µs")
        log.debug("实时改曝光：%d µs（读回 %d）", exposure_us, real)

    def _apply(self, sess: _Session, preview: bool) -> None:
        """停流、写设置、**读回确认** —— 增益和曝光是掉电保持的，不能假设。

        **必须先 disarm**：实测在采集中改 ROI，固件会直接拒绝
        （tl_camera_set_roi() → error code 1005 "Camera Running Error"）。
        """
        cam = sess.cam
        # SDK 文档（tl_camera.py:918）明写：**发出软触发后至少等 300 ms 才能设曝光**。
        # 不等的话 set 不报错、但固件不认账 —— 实测就是这样：要 8000 us，读回还是预览的 2011 us。
        if sess.last_trigger:
            wait = 0.3 - (time.monotonic() - sess.last_trigger)
            if wait > 0:
                time.sleep(wait)
        if sess.armed:
            cam.disarm()
            sess.armed = False
        roi = sess.preview_roi if preview else sess.full_roi
        exposure = self._exposure_us      # 预览与采图同一个曝光
        cam.roi = roi                     # 设完读回：相机会按硬件对齐改写（角点语义，见 _roi_ok）
        cam.exposure_time_us = exposure
        cam.gain = self._gain
        time.sleep(0.05)
        actual = (cam.roi[0], cam.roi[1], cam.image_width_pixels, cam.image_height_pixels)
        if not _roi_ok(actual):
            # 读回是垃圾（设备半死）→ **不缓存、不修**：抛出去让上层丢掉这个会话。
            # 旧代码把垃圾值缓存下来，于是之后每次 set_roi 都撞 error 1003，换句柄也救不回来。
            raise CameraError(f"相机读回的 ROI {actual!r} 不合法（设备掉线？）")
        if preview:
            sess.preview_roi = actual
        else:
            sess.full_roi = actual
        if cam.gain != self._gain:
            raise CameraError(f"增益没设上：要 {self._gain}，读回 {cam.gain}")
        # 曝光也要读回：掉电保持 + 固件按步进取整，设了不等于生效。
        # 只警告不抛：真出问题会在图像亮度上现形，而这里抛会把整条扫描打断。
        if abs(cam.exposure_time_us - exposure) > max(50, exposure * 0.02):
            log.warning("曝光没设上：要 %d us，读回 %d us（%s）",
                        exposure, cam.exposure_time_us, "预览" if preview else "采图")

    def _arm(self, sess: _Session) -> None:
        sess.cam.arm(2)
        sess.cam.issue_software_trigger()
        sess.last_trigger = time.monotonic()
        sess.armed = True

    def _next_frame(self, sess: _Session):
        """拿一帧（uint16，是临时缓冲，调用方要用就得自己 copy）。"""
        frame = sess.cam.get_pending_frame_or_null()
        if frame is None:
            sess.cam.issue_software_trigger()      # 触发丢了或缓冲空了，补一发
            return None
        return frame

    def _pump(self, sess: _Session) -> None:
        """预览取一帧：转 8 位、编码 JPEG、存成"最近一帧"。"""
        if not sess.armed:
            self._apply(sess, preview=True)
            self._arm(sess)
        t0 = time.perf_counter()
        frame = self._next_frame(sess)
        t_grab = time.perf_counter()
        if frame is None:
            return
        img = frame.image_buffer
        # 质心在**未旋转**的原生帧上算：它永远是**传感器坐标**，与保存的 PNG 同一套坐标，
        # 不受预览朝向影响（要拿读数去对文件里的像素，不用做任何换算）。
        # 预览 JPEG 只是投递用的，别拿它算数。
        centroid = global_centroid(img)
        # 留一份原生帧给轮廓图：**必须 copy** —— SDK 的 buffer 下一轮就覆写了，
        # 而剖面是 HTTP 线程随时来取的（跨线程读 SDK 缓冲是数据竞争）。约 3 MB/帧，1 ms 级。
        snapshot = img.copy()
        # 预览朝向**只在这之后**作用于显示：np.rot90 是视图（不复制、不重采样，90° 整数倍是精确置换）。
        # 取 -k 是因为界面上的 90° 要**顺时针**（多数看图软件的习惯），np.rot90 默认是逆时针。
        rotation = self._rotation
        if rotation:
            import numpy as np      # 与本模块其它地方一样：用到才拉 numpy

            img = np.rot90(img, -(rotation // 90))
        jpeg = _to_jpeg(img, self._jpeg_quality)
        t_enc = time.perf_counter()
        with self._lock:
            sess.jpeg = jpeg
            sess.centroid = centroid
            sess.live = snapshot
            sess.frames += 1
            sess.last_frame_at = time.monotonic()   # 真出帧了：新鲜度判断看这个
        # 每 60 帧报一次各段耗时：全幅预览慢在哪要让日志说得清，别靠猜
        if sess.frames % 60 == 1:
            log.info("预览 %dx%d：等帧 %.0f ms + 编码 %.0f ms = %.0f ms/帧（%d 字节）",
                     img.shape[1], img.shape[0], 1000 * (t_grab - t0), 1000 * (t_enc - t_grab),
                     1000 * (t_enc - t0), len(jpeg))

    def _grab_frame(self, scan_id: int, index: int, position_um: float,
                    save: bool = True) -> Optional[str]:
        """取**已经触发**的那一帧，算质心、存 16 位 PNG。

        扫描路径：设置与 arm 在 begin_scan 做过一次，触发由 trigger() 发过，这里只等帧 + 落盘。
        两段各记一笔（等帧 / 落盘），跟预览那条「等帧 + 编码」的日志同一个用意：
        点周期慢在哪要让日志说得清，别靠猜。
        """
        t0 = time.perf_counter()
        sess = self._sess
        if sess is None:
            raise CameraError("相机没有会话（掉线？）—— 这一点没采到")
        # 曝光值在这一帧采完之后就读不到了（恢复预览设置会把它改回去，见 _apply），
        # 而且元数据里记的必须是**这一帧**用的那个值。
        shot_exposure = sess.cam.exposure_time_us
        img = None
        deadline = time.monotonic() + TL_CAPTURE_TIMEOUT_S
        while time.monotonic() < deadline:
            frame = self._next_frame(sess)
            if frame is not None:
                img = frame.image_buffer.copy()   # 必须 copy：下一轮轮询会覆写这块内存
                break
            time.sleep(0.005)
        t_frame = time.perf_counter()
        if img is None:
            raise CameraError(f"第 {index} 点没采到帧（目标 {position_um:.4f} µm）")

        # 这一帧的质心（传感器坐标；口径与预览完全一样：不扣背景、不设阈值、不开窗）。
        # 同一帧只算一次 —— 存盘写进文件自己身上，save_raw 也拿这一份。
        cen = global_centroid(img)
        with self._lock:
            sess.shot = (img, shot_exposure, sess.full_roi, cen)
        if not save:
            log.info("采了一帧原生全幅（不落盘）：%dx%d，曝光 %d us，均值 %.1f",
                     img.shape[1], img.shape[0], shot_exposure, float(img.mean()))
            return None, shot_exposure
        path = _image_path(scan_id, index)
        size = _save_png16(path, img, shot_exposure, cen)
        t_end = time.perf_counter()
        log.info("第 %d 点采图：%s（%dx%d，%d 字节，曝光 %d us，均值 %.1f；"
                 "等帧 %.0f + 落盘 %.0f = %.0f ms）",
                 index, path.name, img.shape[1], img.shape[0], size, shot_exposure,
                 float(img.mean()), 1000 * (t_frame - t0), 1000 * (t_end - t_frame),
                 1000 * (t_end - t0))
        return f"images/{path.name}", shot_exposure

    def _capture_once(self) -> None:
        """一次性采一帧（手动「保存原生帧」用）：自己配置、自己触发、采完恢复预览设置。

        **扫描不走这里** —— 那边 begin_scan 配一次就够（见 _run 的 begin_scan）。
        这条路上配置是必须的：用户可能正开着预览，而预览的 ROI/曝光跟采图不是一套。
        """
        sess = self._ensure_session(0.0)
        armed_before = sess.armed
        self._apply(sess, preview=False)
        self._arm(sess)
        try:
            self._grab_frame(0, 0, 0.0, save=False)
        finally:
            if sess.armed:
                sess.cam.disarm()
                sess.armed = False
            if armed_before:              # 预览还开着就恢复，别把预览弄死
                self._apply(sess, preview=True)
                self._arm(sess)

    def profile(self, x: int, y: int) -> dict:
        """过 (x, y) 的两条**全长**剖面：水平取整行、垂直取整列。

        **取自最近一帧的原生 16 位数据**（不是预览 JPEG），值就是相机给的那串整数（0~1022 ADU），
        不做任何处理 —— 不做背景、不平滑、不归一化。
        坐标是**显示坐标**：先按当前朝向转成视图再切，所以转向之后"水平"仍然是你眼睛看到的水平；
        文件里的那个坐标是传感器坐标，两者在转了 90° 时会差一个转置（界面会写明当前朝向）。
        """
        import numpy as np

        sess = self._sess
        with self._lock:
            frame = sess.live if sess else None    # 帧挂会话：没有会话就没有帧
            rotation = self._rotation
        if frame is None:
            raise CameraError("还没有帧：先开预览")
        view = np.rot90(frame, -(rotation // 90)) if rotation else frame
        h, w = view.shape
        if not (0 <= x < w and 0 <= y < h):
            raise CameraError(f"点 ({x}, {y}) 超出画面 {w}×{h}")
        return {
            "x": int(x), "y": int(y), "width": int(w), "height": int(h),
            "rotation": rotation, "bits": 16, "full_scale": TL_SATURATION_ADU,
            "horizontal": [int(v) for v in view[y, :]],
            "vertical": [int(v) for v in view[:, x]],
        }

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
            raise CameraInputError("曝光必须是正数（µs）")
        # 范围（相机自报的 min/max）只有会话里才有，所以权威校验在 owner 线程做 —— 见 _run：
        # 没会话时它会为这次校验现建一个、用完收掉（一次性动作）。
        self._ensure_thread()
        return self._submit(("exposure", int(exposure_us)),
                            timeout=TL_OPEN_TIMEOUT_S + TL_OPEN_WAIT_S)

    def save_raw(self) -> dict:
        """采一帧**原生全幅**存 16 位 PNG，给界面上的「保存原生帧」用。

        与扫描采图走同一段代码（同样的曝光、同样的不裁剪），但不占扫描的编号，
        也不额外多做一次曝光 —— 采完的帧就在会话里（_run 的 save 分支带回来）。
        """
        self._ensure_thread()
        st = self._submit(("save",), timeout=TL_CAPTURE_TIMEOUT_S)
        shot = st.get("shot") if isinstance(st, dict) else None
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




def _norm_rotation(deg: int) -> int:
    """朝向只接受 0/90/180/270（顺时针），别的直接拒绝 —— 不"取个近似值"糊过去。"""
    deg = int(deg) % 360
    if deg % 90:
        raise CameraInputError(f"预览朝向只能是 0/90/180/270 度，收到 {deg}")
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


def png_profile(path: Path, x: int, y: int) -> dict:
    """从**存下来的 PNG** 里过 (x, y) 取两条全长剖面（水平整行 + 垂直整列）。

    文件永远是**传感器朝向**（保存从不旋转），大图显示的也是它，所以这里的坐标就是你在图上
    点的那个位置。16 位值原样返回；假相机那种 8 位占位图也照实说（bits/full_scale 跟着变）。
    """
    import numpy as np
    from PIL import Image

    arr = np.asarray(Image.open(path))
    h, w = arr.shape
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"点 ({x}, {y}) 超出画面 {w}×{h}")
    bits = 16 if arr.dtype == np.uint16 else 8
    return {
        "x": int(x), "y": int(y), "width": int(w), "height": int(h),
        "rotation": 0, "bits": bits,
        "full_scale": TL_SATURATION_ADU if bits == 16 else 255,
        "horizontal": [int(v) for v in arr[y, :]],
        "vertical": [int(v) for v in arr[:, x]],
    }


def _png_gray(path: Path):
    """读成二维灰度数组；读不动（被删/被占用/不是图片）或不是灰度 → None。

    PNG 没有"只解一行"的办法（滤波要按行递推），所以就是整幅解一次 ——
    1440×1080 的 16 位帧实测约 6 ms，几百点的扫描因此是可接受的。
    """
    import numpy as np
    from PIL import Image

    try:
        arr = np.asarray(Image.open(path))
    except Exception:            # noqa: BLE001 —— 文件被删/被占用/不是图片：这一点就没有值，不猜
        return None
    return arr if arr.ndim == 2 else None


def _pixel_of(path: Path, x: int, y: int):
    """一张图上的那一个像素；这一张读不动或比 (x, y) 小 → None（尺寸不齐时按张判）。"""
    arr = _png_gray(path)
    if arr is None or y >= arr.shape[0] or x >= arr.shape[1]:
        return None
    return int(arr[y, x])


def png_pixel_series(items, x: int, y: int, workers: int = 8) -> dict:
    """一条扫描里、**每个扫描点上同一个像素**的值 —— 数据处理页那条曲线的取数。

    items 是 [(序号, 读出位置 µm, PNG 路径 | None), ...]，按扫描顺序给全。
    没图的点（没采到图 / CCD 后端是 null）给 None —— **不补值、不插值**，曲线在那里断开。
    位置原样带回（那是扫描记下来的读出位置，本函数只负责读像素）。

    几何（宽高 / 位深 / 满量程）以**第一张读得动的图**为准；点落在画面外直接报错 ——
    不然整条曲线会全是 None，看着像"这个像素没光"，其实是指错了地方。
    整幅解码是 CPU 活，几百张串行约 2.5 s、8 线程约 0.8 s，所以这里铺开读。
    """
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor

    out = {
        "x": int(x), "y": int(y),
        "width": None, "height": None, "bits": None, "full_scale": None,
        "idx": [int(it[0]) for it in items],
        "position_um": [it[1] for it in items],
        "value": [None] * len(items),
        "missing": 0,          # 没图 / 图读不出来的点数（曲线在这里断开）
    }
    todo = []
    for i, (_, _, path) in enumerate(items):
        if path is None:
            out["missing"] += 1
            continue
        if out["width"] is None:                 # 第一张读得动的图定几何，顺便把它自己那份值取了
            arr = _png_gray(path)
            if arr is None:
                out["missing"] += 1
                continue
            h, w = arr.shape
            if not (0 <= x < w and 0 <= y < h):
                raise ValueError(f"点 ({x}, {y}) 超出画面 {w}×{h}")
            bits = 16 if arr.dtype == np.uint16 else 8
            out.update(width=int(w), height=int(h), bits=bits,
                       full_scale=TL_SATURATION_ADU if bits == 16 else 255)
            out["value"][i] = int(arr[y, x])
            continue
        todo.append((i, path))

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            got = pool.map(lambda it: _pixel_of(it[1], x, y), todo)
            for (i, _), v in zip(todo, got):
                out["value"][i] = v
                if v is None:
                    out["missing"] += 1
    return out


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
