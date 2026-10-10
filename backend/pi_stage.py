"""E-709 控制器封装：全进程唯一的设备访问入口。

设计约束（见 AGENTS.md）：
1. 只有一个 owner 线程调用 PIPython/DLL；其他线程通过命令队列提交任务，
   避免并发访问 GCS DLL（它非线程安全）。
2. 急停走旁路：置标志 + 丢弃排队命令，owner 线程在飞行中的调用返回后立即执行 STP。
3. 限位从设备读取（TMN?/TMX?），不硬编码。
4. 等待到位由调用方轮询完成；owner 线程只执行短命令，保证随时能响应停止。
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from pipython import GCSDevice, GCSError
from pipython.pidevice.interfaces.pigateway import PIGateway
from pipython.pidevice.interfaces.piserial import PISerial

from .config import (
    AXIS,
    DEFAULT_SETTLE_MS,
    DEVICE_NAME,
    DEVICE_SERIAL,
    LINK,
    MAX_VELOCITY,
    SERIAL_BAUD,
    SERIAL_PORT,
    SLOW_QUERY_EVERY,
)
from .stage_api import (
    RELEASE_SERVO_OFF,
    SETTLE_DEVICE,
    Caps,
    StageAborted,  # noqa: F401  重新导出，老引用继续可用
    StageError,  # noqa: F401
    StageNotConnected,  # noqa: F401
    StageStatus,
    StopResult,
    sleep_cancelable,
)

log = logging.getLogger(__name__)

# GCS 错误码 10：控制器被命令停止。这是 STP 的正常回执，不是故障。
ERR_STOPPED_BY_COMMAND = 10

# 本设备能力声明（见 stage_api.Caps）
CAPS = Caps(
    name="PI E-709",
    platform="Windows + Linux",
    has_on_target=True,
    has_stop_command=True,
    release_mode=RELEASE_SERVO_OFF,
    has_setpoint_ack=True,
    has_velocity=True,
    unit="µm",
    default_settle_ms=DEFAULT_SETTLE_MS,
    readback_to_um=1.0,     # PI 的位置读数本来就是 µm，没有折算
)

# Linux 下 E-709 以 FTDI 虚拟串口出现（内核 ftdi_sio 直接驱动，不需要 PI 的 .so）。
# 按 by-id 路径找，不写死 ttyUSB0：换 USB 口或插别的串口设备时编号会变。
SERIAL_BY_ID = "/dev/serial/by-id"

# Windows 上 E-709 同样以 FTDI 虚拟串口（VCP）出现，**也能走这条纯 Python 命令层**，
# 不需要 PI Software Suite。按 VID:PID 认设备，不写死 COM 号（换 USB 口编号会变）。
PI_USB_VID = 0x1A72
PI_USB_PID = 0x100E


def resolve_link() -> str:
    """实际使用的连接方式：'usb'（GCS DLL）或 'serial'（FTDI 虚拟串口）。"""
    if LINK == "auto":
        return "usb" if sys.platform == "win32" else "serial"
    if LINK not in ("usb", "serial"):
        raise StageError(f"PI_LINK 取值非法：{LINK!r}（应为 auto/usb/serial）")
    return LINK


def find_serial_port() -> str:
    """找 PI 的串口设备路径；多个取第一个，一个都没有则报错。

    Linux 走 /dev/serial/by-id（内核 ftdi_sio 给的 FTDI 虚拟串口）；
    Windows 走 VID:PID —— FTDI 的 VCP 驱动同样给一个 COM 口，**不需要 PI Software Suite**，
    所以这里是"按设备认口"，换 USB 口、COM 号变了都不用改配置。
    """
    if sys.platform == "win32":
        from serial.tools import list_ports      # 用到才拉（与其它地方同一个规矩）

        ports = [p for p in list_ports.comports()
                 if (p.vid, p.pid) == (PI_USB_VID, PI_USB_PID)]
        if not ports:
            raise StageNotConnected(
                f"没有 VID:PID={PI_USB_VID:04X}:{PI_USB_PID:04X} 的串口（PI 控制器）。"
                "确认控制器已上电、USB 线插好；或用 PI_SERIAL_PORT 直接指定 COM 口。"
            )
        if len(ports) > 1:
            log.warning("发现多个 PI 串口，使用第一个：%s", [p.device for p in ports])
        return ports[0].device

    found = sorted(Path(SERIAL_BY_ID).glob("usb-PI_*"))
    if not found:
        raise StageNotConnected(
            f"{SERIAL_BY_ID}/usb-PI_* 下没有设备。确认控制器已上电、USB 线插好。"
        )
    if len(found) > 1:
        log.warning("发现多个 PI 串口，使用第一个：%s", [p.name for p in found])
    return str(found[0])


def stp(dev: GCSDevice) -> None:
    """发送 STP。控制器以错误码 10 回执，属正常，不当作异常抛给调用方。"""
    try:
        dev.STP()
    except GCSError as exc:
        if exc.val != ERR_STOPPED_BY_COMMAND:
            raise


@dataclass
class _Job:
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    exc: Optional[BaseException] = None
    cancelled: bool = False

    def wait(self, timeout: Optional[float] = None) -> Any:
        if not self.done.wait(timeout):
            raise StageError("设备命令超时（owner 线程未响应）")
        if self.exc is not None:
            raise self.exc
        return self.result


class Stage:
    """线程安全的 E-709 封装。所有公开方法都可以从任意线程调用。"""

    caps: Caps = CAPS
    # 采图前按步距再复核一次偏差（scanner 那道相对校验）：PI 的到达容差是绝对的，
    # 步距比它小时判据失去分辨力，这道补盲区。
    step_check = True

    def __init__(
        self,
        devname: str = DEVICE_NAME,
        serial: str = DEVICE_SERIAL,
        axis: str = AXIS,
    ) -> None:
        self._devname = devname
        self._serial = serial
        self._axis = axis
        self._dev: Optional[GCSDevice] = None
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._estop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._status = StageStatus(settle_source=SETTLE_DEVICE, axis=axis)
        self._status_lock = threading.Lock()
        self._tick = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 20.0) -> None:
        """启动 owner 线程并连接控制器（失败抛异常）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._owner_loop, name="pi-stage-owner", daemon=True
        )
        self._thread.start()
        try:
            self._call(self._open, timeout=timeout)
        except BaseException:
            self._running = False
            self._jobs.put(None)
            raise

    def shutdown(self) -> None:
        """断开设备并停 owner 线程。

        不动伺服：位移台保持原位（定位仪器的正常状态）。要卸力请显式 release()。
        """
        if not self._running:
            return
        try:
            self._call(self._close, timeout=5.0)
        except Exception:
            log.warning("关闭设备失败", exc_info=True)
        self._running = False
        self._jobs.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ------------------------------------------------------------------ owner 线程
    def _owner_loop(self) -> None:
        while self._running:
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            if self._estop_flag.is_set():
                self._do_estop()
            if job.cancelled:
                continue
            try:
                job.result = job.fn(*job.args, **job.kwargs)
            except GCSError as exc:
                job.exc = StageError(f"控制器报错：{exc}")
            except BaseException as exc:  # noqa: BLE001 - 必须传回调用方
                job.exc = exc
            finally:
                job.done.set()
        self._close_quiet()

    def _submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> _Job:
        if not self._running:
            raise StageNotConnected("设备层未启动")
        job = _Job(fn, args, kwargs)
        self._jobs.put(job)
        return job

    def _call(self, fn: Callable[..., Any], *args: Any, timeout: float = 10.0, **kwargs: Any) -> Any:
        return self._submit(fn, *args, **kwargs).wait(timeout)

    # ------------------------------------------------------------------ 急停旁路
    def estop(self) -> None:
        """急停：丢弃排队命令，并让 owner 线程立即执行 STP（保持伺服）。"""
        self._estop_flag.set()
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is None:
                self._jobs.put(None)
                break
            job.cancelled = True
            job.exc = StageAborted("急停：命令已被丢弃")
            job.done.set()
        # 空任务用于把可能阻塞在 get() 上的 owner 线程叫醒
        self._jobs.put(_Job(lambda: None))

    def _do_estop(self) -> None:
        """急停不允许抛异常：调用方在请求线程，不能被中断。"""
        self._estop_flag.clear()
        if self._dev is None:
            return
        try:
            stp(self._dev)
            log.warning("急停：已发送 STP")
        except Exception:
            log.error("急停 STP 失败", exc_info=True)

    # ------------------------------------------------------------------ 设备操作（owner 线程内执行）
    def _open(self) -> None:
        # 连接前先清空 PIPython 的连接状态回调。它是 PIGateway 上的**类级**列表，
        # 注册发生在 GCSDevice.__init__ 里、**早于**里面那次与设备通信的探测
        # （_downcast_gcsdevice_if_necessary → isgcs30_by_qcsv → float(read('CSV?'))）。
        # 所以构造中途抛异常时（串口被占、回包为空 → float('') 报 ValueError），
        # 调用方拿不到对象，回调却已注册：它强引用该对象使其永不回收，串口 fd
        # 一直开着，下次构造 gateway 时还会被调到已死的旧连接上（PortNotOpenError）。
        # 本进程同一时刻只应有一个设备对象，清空是安全的。
        PIGateway._connection_status_changed_callbacks.clear()
        if resolve_link() == "usb":
            dev = GCSDevice(self._devname)
            target = self._serial
            if not target:
                found = list(dev.EnumerateUSB())
                if not found:
                    raise StageNotConnected("未找到 PI 控制器（USB）")
                if len(found) > 1:
                    log.warning("发现多台 PI 设备，使用第一台：%s", found)
                target = found[0]
            dev.ConnectUSB(target)
        else:
            port = SERIAL_PORT or find_serial_port()
            try:
                # gateway=PISerial：只走 PIPython 的纯 Python 命令层，不加载 GCS DLL
                dev = GCSDevice(self._devname, gateway=PISerial(port, SERIAL_BAUD))
            except Exception as exc:
                raise StageNotConnected(
                    f"打开串口 {port} 失败：{exc}"
                    "（若为权限拒绝：sudo usermod -aG dialout $USER 后重新登录）"
                ) from exc
        self._dev = dev
        try:
            idn = dev.qIDN()
            serial = str(dev.qSSN()).strip()
            stage_type = str(dev.qCST()[self._axis]).strip()
            travel_min = float(dev.qTMN()[self._axis])
            travel_max = float(dev.qTMX()[self._axis])
            with self._status_lock:
                st = self._status
                st.serial = serial
                st.stage_type = stage_type
                st.travel_min = travel_min
                st.travel_max = travel_max
                st.axis = self._axis
                st.connected = True
            log.info("已连接：%s（位移台 %s，行程 %.3f–%.3f µm）",
                     idn, stage_type, travel_min, travel_max)
        except Exception:
            self._close_quiet()
            raise
        self._refresh_fast()
        self._refresh_slow()
        # 本应用只用闭环。控制器上电默认是开环，这里按当前位置补开伺服。
        # SVO 1 会把当前位置写进目标寄存器，故不产生位移。实测验证过（E-709，
        # 固件 5.001）：开环下用 SVR 把轴挪到 +1.24 µm（MOV? 仍为 0）后 SVO 1，
        # 轴停在原地，MOV? 随即变为 +1.2468。参见手册 PZ222E §3.7.1：
        #   "SVO 1 1 ... this also writes the current axis position to the
        #    target register, to avoid jumps of the mechanics."
        # 唯一例外：当前位置在标定行程 [TMN?, TMX?] 之外时目标被夹到行程端点，
        # 轴会走回范围内（曾见开环漂到 −4.04 µm 后开伺服回到 0）。
        # 不要"先 MOV 到当前位置再 SVO"：伺服关闭时 MOV 被拒绝（错误码 5），
        # 开环下只能用 SVA/SVR。
        if not self._status.servo:
            log.info("控制器处于开环，按当前位置开启闭环保持")
            self._set_servo(True)
        # 目标值与速度**只在连接时取一次真值**：周期慢查询不再问它们（见 _refresh_slow），
        # 这两个值此后只有本进程会改。这里读设备而不是拿位置顶替 —— 开环转闭环那一下
        # 目标可能被控制器夹到行程端点（见上面那条注释），位置不等于目标寄存器。
        target = float(dev.qMOV()[self._axis])
        velocity = float(dev.qVEL()[self._axis])
        with self._status_lock:
            self._status.target = target
            self._status.velocity = velocity

    def _close(self) -> None:
        if self._dev is not None:
            try:
                # 必须走 _cleanup() 而不是 close()：PIPython 的连接状态回调注册在
                # PIGateway 的**类级**列表里（pigateway.py: _connection_status_changed_
                # callbacks），close() 不注销它，而那个绑定方法持有本对象的强引用，
                # 使引用计数永不归零、__del__ 不触发。残留回调会在下次构造 gateway
                # 时被调用，打到已关闭的旧连接上，报 PortNotOpenError（实测复现）。
                # _cleanup() = 注销回调 + close()，正是 GCSDevice.__exit__ 走的路。
                self._dev._cleanup()
            finally:
                self._dev = None
        with self._status_lock:
            self._status.connected = False

    def _close_quiet(self) -> None:
        try:
            self._close()
        except Exception:
            log.warning("关闭设备时出错", exc_info=True)

    def _require(self) -> GCSDevice:
        if self._dev is None:
            raise StageNotConnected("控制器未连接")
        return self._dev

    def _refresh_fast(self) -> None:
        dev = self._require()
        position = float(dev.qPOS()[self._axis])
        on_target = bool(dev.qONT()[self._axis])
        with self._status_lock:
            self._status.position = position
            self._status.on_target = on_target
            self._status.updated_at = time.time()

    def _refresh_slow(self) -> None:
        """慢查询：只问**会自己变**的三样（伺服 / 过冲 / 错误码）。

        **不再问 qMOV? 与 qVEL?**（2026-10）：目标值由 _move 就地写、速度由
        _set_velocity 就地写，这两个值只有本进程能改，读回来是白花两趟（每趟还得
        陪一条 ERR?）。慢查询因此从 9 次往返降到 5 次。连接时那一次初值在 _open 里取。
        """
        dev = self._require()
        servo = bool(dev.qSVO()[self._axis])
        overflow = bool(dev.qOVF()[self._axis])
        error_code = int(dev.qERR())
        with self._status_lock:
            st = self._status
            st.servo = servo
            st.overflow = overflow
            st.error_code = error_code

    def _tick_once(self) -> None:
        self._refresh_fast()
        self._tick += 1
        if self._tick % SLOW_QUERY_EVERY == 0:
            self._refresh_slow()

    def _move(self, target: float) -> None:
        """下发目标。**写完不再立刻读回**（2026-10 起）。

        MOV 是异步的：紧跟的那次 qPOS? 读到的是"刚起步的位置"，对扫描毫无用处 ——
        扫描紧接着就要等到位信号、再读一次，**那一次才是入库的读数**。每点省 2 条查询。
        代价：手动移动后界面上的位置/到位晚一个遥测周期刷新（空闲 ≤100 ms）。
        _jog 自己先 _refresh_fast、_hold_here 与 _stop 各自也会刷新，不受影响。
        XMT 早就是这么做的（docs/xmt/设备认识账.xml 的 P1），这里同一个道理。
        """
        self._require().MOV(self._axis, float(target))
        with self._status_lock:
            self._status.target = float(target)
            # 下了新目标就还没到位：别让界面把上一状态的"是"当成现在的
            # （真值归控制器，下一个遥测周期就会刷出来）。
            self._status.on_target = False

    def _jog(self, delta: float) -> float:
        self._refresh_fast()
        target = self.clamp(self._status.position + delta)
        self._move(target)
        return target

    def _set_servo(self, on: bool) -> None:
        """开/关伺服。

        **开的那一下会把当前位置写进目标寄存器**（SVO 1 的语义，见 _open 里那条注释），
        所以目标值要跟着走：它不再由周期慢查询从设备读回（见 _refresh_slow），
        这里不补，界面上的「目标」就会停在开伺服之前那个值。
        """
        self._require().SVO(self._axis, 1 if on else 0)
        self._refresh_slow()
        if on:
            self._refresh_fast()
            with self._status_lock:
                self._status.target = self._status.position

    def _set_velocity(self, velocity: float) -> None:
        """写速度。**写完不再读回**：qVEL? 已从周期慢查询里去掉（见 _refresh_slow），
        这里就地记账；下发值本身已在公开的 set_velocity 里夹过范围。
        """
        self._require().VEL(self._axis, float(velocity))
        with self._status_lock:
            self._status.velocity = float(velocity)

    def _stop(self) -> None:
        stp(self._require())
        self._refresh_fast()

    # ------------------------------------------------------------------ 公开 API
    def status(self) -> StageStatus:
        """返回状态快照（不访问设备）。"""
        with self._status_lock:
            return replace(self._status)

    def poll(self) -> StageStatus:
        """让 owner 线程刷新一次状态，返回快照。"""
        self._call(self._tick_once)
        return self.status()

    def clamp(self, target: float) -> float:
        """把目标夹进设备行程范围。"""
        st = self.status()
        lo, hi = st.travel_min, st.travel_max
        return max(lo, min(hi, float(target)))

    def move(self, target: float) -> float:
        """移动到绝对位置（µm），返回实际下发的目标值（已夹限位）。"""
        value = self.clamp(target)
        self._call(self._move, value)
        return value

    def jog(self, delta: float) -> float:
        """相对移动（µm），从设备实时位置起算，返回实际下发的位置。"""
        return self._call(self._jog, float(delta))

    def set_servo(self, on: bool) -> None:
        self._call(self._set_servo, bool(on))

    def hold_here(self) -> float:
        """开伺服并原地保持，返回当前（即新的目标）位置。

        手册：SVO 改变伺服状态时会写目标寄存器，开伺服时目标取当前位置，
        所以不会跳回上次 MOV 的目标。伺服关着时 MOV 会被控制器拒绝（错误码 5）。
        """
        return self._call(self._hold_here)

    def _hold_here(self) -> float:
        self._set_servo(True)
        self._refresh_fast()
        return self._status.position

    def set_velocity(self, velocity: float) -> float:
        value = max(1.0, min(MAX_VELOCITY, float(velocity)))
        self._call(self._set_velocity, value)
        return value

    def stop_motion(self) -> StopResult:
        """停止运动，保持伺服（位姿保持）。"""
        self._call(self._stop)
        return StopResult.HARD

    def release(self) -> None:
        """关闭伺服（卸力）。台子会回弹到静止位，异常振动时使用。"""
        self._call(self._set_servo, False)

    def poll_on_target(self) -> bool:
        """只查一次到位信号（省一次往返），用于等待稳定。"""
        return bool(self._call(self._read_on_target))

    def _read_on_target(self) -> bool:
        on_target = bool(self._require().qONT()[self._axis])
        with self._status_lock:
            self._status.on_target = on_target
            self._status.updated_at = time.time()
        return on_target

    def wait_on_target(
        self,
        timeout: float,
        cancel: Optional[Callable[[], bool]] = None,
        settle_s: float = 0.0,
    ) -> bool:
        """在调用方线程轮询等待到位。owner 线程保持可响应。

        控制器给出 ONT? 之后还要再等 settle_s —— 这是 AGENTS 定的 PI 语义
        「到位信号 + 一段固定延时」，延时归设备层，所以放在这里而不是让上层再睡一次。
        cancel 返回 True 时立即放弃等待（中止扫描用），返回 False。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel():
                return False
            if self.poll_on_target():
                return sleep_cancelable(settle_s, cancel)
        return False
