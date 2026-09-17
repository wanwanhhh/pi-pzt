"""芯明天 E53.D1S-H 设备层：自写协议 + pyserial 直连 USB CDC 虚拟串口（仅 Windows）。

协议与实测结论见 docs/xmt/README.md。三处与 PI 的语义差写在 CAPS 里（见 stage_api）：

- **单位陷阱**：设点 `1` 与行程 `27/35` 是 µm，读回 `6` 是 4/3 µm ——
  读回乘 XMT_READBACK_TO_UM 才进上层，设点永远是 µm。**绝不让读回值原样流进设点**：
  实测那样写回去，台子从 108.66 µm 跑到 144.95 µm（跑偏 4/3 倍）。
- **没有到位信号**：停稳只能软件判（SettleJudge：不重叠窗口取均值 + 采样间隔抖动）。
- **没有停止 / 关伺服指令**：stop_motion 只能软停（当前真实位置写回成新目标），
  release 只能切开环 + 输出写零（卸力，**不保证停在原位**）。

连接时从设备读回 19（开闭环）/ 53（单位码）/ 27 35（行程，µm），有一条不合就拒绝连接 ——
**行程是软限位的唯一权威来源**，写死一个数比不连更危险。
"""
from __future__ import annotations

import logging
import queue
import random
import statistics
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

import serial
from serial.tools import list_ports

from . import xmt_protocol as xp
from .config import (
    SLOW_QUERY_EVERY,
    XMT_ADDRESS,
    XMT_ARRIVAL_TOL_UM,
    XMT_BAUD,
    XMT_CMD_TIMEOUT,
    XMT_MAX_TRAVEL_UM,
    XMT_POLL_JITTER_MS,
    XMT_POLL_MS,
    XMT_PORT,
    XMT_READ_TIMEOUT,
    XMT_READBACK_TO_UM,
    XMT_SETTLE_EPS_UM,
    XMT_SETTLE_WINDOW,
    XMT_SETTLE_WINDOWS,
    XMT_USB_PID,
    XMT_USB_VID,
)
from .stage_api import (
    Caps,
    RELEASE_OPEN_LOOP_ZERO,
    SETTLE_SOFTWARE,
    StageAborted,
    StageError,
    StageNotConnected,
    StageStatus,
    StopResult,
)

log = logging.getLogger(__name__)

CAPS = Caps(
    name="芯明天 E53.D1S-H",
    platform="仅 Windows",
    has_on_target=False,
    has_stop_command=False,
    release_mode=RELEASE_OPEN_LOOP_ZERO,
    has_setpoint_ack=False,
    has_velocity=False,   # 协议表里没有速度指令，set_velocity 明确拒绝
    unit="µm",
)

# 协议 §2.5 要求主机帧间隔最少 50 ms。实测不强制（背靠背连发两条都被处理），
# 但生产端不靠"下位机容忍"过日子，照手册留间隔。
FRAME_GAP = 0.05
# 设备**掉电保持上次波特率**（指令 63 设的），所以连不上时要把档位扫一遍。
BAUD_CANDIDATES = (XMT_BAUD, 9600, 19200, 38400, 57600, 76800, 128000, 230400, 256000)
LOOP_CLOSED = b"C"
LOOP_OPEN = b"O"
UNIT_DISPLACEMENT = 1


def find_port() -> Optional[str]:
    """按 VID:PID 找控制器的串口。"""
    for p in list_ports.comports():
        if (p.vid, p.pid) == (XMT_USB_VID, XMT_USB_PID):
            return p.device
    return None


class _Link:
    """帧收发。跑在 owner 线程里：静默、带超时、按手册留帧间隔。

    tools/xmt_check.py 里那份 Link 是**上机脚本**用的（带打印、带只读白名单），
    服务对象不同，故意不共用：这里要静默、不能有交互输出，也不认白名单。
    """

    def __init__(
        self, port: str, baud: int = XMT_BAUD, addr: int = XMT_ADDRESS, ser: Any = None
    ) -> None:
        self.port = port
        # ser 只在离线测试里注入（假串口）；正常路径自己开真串口
        self.ser = ser if ser is not None else serial.Serial(port, baud, timeout=0)
        self.parser = xp.Parser()
        self.addr = addr
        self.baud = baud
        self._next_send = 0.0

    def close(self) -> None:
        self.ser.close()

    def set_baud(self, baud: int) -> None:
        self.ser.baudrate = baud
        self.baud = baud
        self.drain()

    def drain(self) -> None:
        """丢掉残留，让每轮从干净状态开始。"""
        self.ser.reset_input_buffer()
        self.parser = xp.Parser()

    def _gap(self) -> None:
        wait = self._next_send - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_send = time.monotonic() + FRAME_GAP

    def send(self, raw: bytes) -> None:
        """发一帧。写命令（0/1/18）没有应答，丢了是静默的 —— 靠读回校验兜。"""
        self._gap()
        self.ser.write(raw)

    def ask(self, raw: bytes, want_b3: int, timeout: float = XMT_READ_TIMEOUT) -> Optional[xp.Frame]:
        """发一帧并等它的回包；超时返回 None。"""
        self.drain()
        self.send(raw)
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            n = self.ser.in_waiting
            if n:
                for f in self.parser.feed(self.ser.read(n)):
                    if f.b3 == want_b3:
                        return f
            else:
                time.sleep(min(left, 0.0005))


class SettleJudge:
    """软件停稳判据：判的是**台子不动了**，不是"到位精度"。

    - 取 window 个读数算一个均值，窗口与窗口之间比：均值差 ≤ eps 才算这一窗没动；
      连着 need 个这样的窗口 → 停稳。
    - 窗口**不重叠**：滑动窗口（每次只挪一个样本）会把运动摊薄，匀速走时相邻均值
      只差 v/window，等于把判据放宽了 window 倍。
    - 采样间隔带抖动（见 wait_on_target）：固定网格会与闭环振荡拍频，拍频极低时
      连续几次读数看起来纹丝不动。**鲁棒性来自抖动与窗口长度，不是采样率。**
    - 同时还要求落在目标附近：**设点无应答，丢帧是静默的**，不校验就会把
      "没动的台子"当成到位采图。

    已知限制（接受，不当 bug 查）：台子蠕变时读数变化极慢，会落进 eps 被判成停稳。
    """

    def __init__(self, window: int, need: int, eps_um: float, tol_um: float) -> None:
        self.window = max(2, int(window))
        self.need = max(2, int(need))   # need=1 时第一窗只立基线，等于没有运动检测
        self.eps_um = float(eps_um)
        self.tol_um = float(tol_um)
        self.reset(0.0)

    def reset(self, target_um: float) -> None:
        self._target = float(target_um)
        self._buf: list[float] = []
        self._prev: Optional[float] = None
        self._streak = 0
        self._settled = False

    def feed(self, position_um: float) -> bool:
        """喂一个读数（µm），返回当前是否算停稳。窗口没满时保持上次结论。"""
        self._buf.append(float(position_um))
        if len(self._buf) < self.window:
            return self._settled
        mean = statistics.fmean(self._buf)
        self._buf.clear()
        moved = self._prev is not None and abs(mean - self._prev) > self.eps_um
        self._streak = 0 if moved else self._streak + 1
        self._prev = mean
        self._settled = self._streak >= self.need and abs(mean - self._target) <= self.tol_um
        return self._settled


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


class XmtStage:
    """线程安全的 E53.D1S-H 封装。所有公开方法都可以从任意线程调用。

    设备访问全在 owner 线程里（专用线程 + queue.Queue，见 AGENTS.md）：
    pyserial 的读写与等待都发生在那一根线程上，调用方只取结果。
    """

    caps: Caps = CAPS

    def __init__(
        self,
        port: str = XMT_PORT,
        addr: int = XMT_ADDRESS,
        link: Any = None,
    ) -> None:
        self._port = port
        self._addr = int(addr)
        self._link: Any = link        # 测试注入；正常为 None，连接时自己开
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._estop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._status = StageStatus(settle_source=SETTLE_SOFTWARE)
        self._status_lock = threading.Lock()
        self._judge = SettleJudge(
            XMT_SETTLE_WINDOW, XMT_SETTLE_WINDOWS, XMT_SETTLE_EPS_UM, XMT_ARRIVAL_TOL_UM
        )
        self._tick = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 20.0) -> None:
        """启动 owner 线程并连接设备（失败抛异常）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._owner_loop, name="xmt-stage-owner", daemon=True
        )
        self._thread.start()
        try:
            self._call(self._open, timeout=timeout)
        except BaseException:
            self._running = False
            self._jobs.put(None)
            raise

    def shutdown(self) -> None:
        """断开设备并停 owner 线程。不动输出：台子保持原位，要卸力请 release()。"""
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
            except serial.SerialException as exc:
                job.exc = StageError(f"串口出错：{exc}")
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

    def _call(
        self, fn: Callable[..., Any], *args: Any, timeout: float = XMT_CMD_TIMEOUT, **kwargs: Any
    ) -> Any:
        return self._submit(fn, *args, **kwargs).wait(timeout)

    # ------------------------------------------------------------------ 急停旁路
    def estop(self) -> None:
        """急停：丢弃排队命令，并让 owner 线程立刻软停。

        设备**没有**停止指令，能做的最多是把当前真实位置写成新目标 ——
        已经在途的那段行程拦不住，这一点由 stop_motion() 返回 SOFT 告诉上层。
        """
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
        if self._link is None:
            return
        try:
            self._soft_stop()
            log.warning("急停：已把当前位置写回成新目标（软停，在途行程拦不住）")
        except Exception:
            log.error("急停软停失败", exc_info=True)

    # ------------------------------------------------------------------ 连接
    def _open(self) -> None:
        if self._link is None:
            port = self._port or find_port()
            if not port:
                raise StageNotConnected(
                    f"没找到 VID:PID={XMT_USB_VID:04X}:{XMT_USB_PID:04X} 的串口。"
                    "确认控制器已上电、USB 线插好，或用 PI_XMT_PORT 指定端口。"
                )
            self._link = _Link(port, XMT_BAUD, self._addr)
        try:
            if not self._handshake(self._link):
                raise StageNotConnected(
                    "77 握手没有回 'OK'（九档波特率 × 三个地址都试过了）。"
                    "确认没有别的主机占着串口；设备掉电会保持上次波特率。"
                )
            self._identify(self._link)
        except BaseException:
            self._close_quiet()
            raise

    def _handshake(self, link: Any) -> bool:
        for baud in dict.fromkeys(BAUD_CANDIDATES):
            try:
                link.set_baud(baud)
            except Exception:
                continue
            for addr in dict.fromkeys((self._addr, 1, xp.BROADCAST)):
                link.addr = addr
                f = link.ask(xp.frame(xp.CMD_HANDSHAKE, addr=addr), xp.CMD_HANDSHAKE)
                if f is not None and f.data == b"OK":
                    self._addr = addr
                    log.info("握手成功：%s @ %d 8N1，地址 %d", link.port, baud, addr)
                    if addr == xp.BROADCAST:
                        # 单播没应答、只有广播认了：能用，但别装作知道设备地址。
                        log.warning("只有广播地址（0）应答，本次会话全程用广播"
                                    "（单设备没问题；多设备同总线时不行）")
                    return True
        return False

    def _identify(self, link: Any) -> None:
        """连接时把三件事读回来。任何一条不合就拒绝连接，不猜、不写死。"""
        mode = self._read_loop_mode()
        if mode not in (LOOP_CLOSED, LOOP_OPEN):
            raise StageNotConnected(f"19 读开闭环回了看不懂的值 {mode!r}，拒绝连接")

        code = self._read_code(xp.CMD_READ_UNIT)
        if code is None:
            raise StageNotConnected("53 读单位没回包，拒绝连接")
        if code != UNIT_DISPLACEMENT:
            raise StageNotConnected(
                f"设备报的单位码是 {code}（{xp.UNITS.get(code, '未知')}），不是 1（位移）。"
                "本仓库只按位移（µm）用它，拒绝连接。"
            )

        lo = self._read_raw(xp.CMD_READ_POS_LIMIT_LOW)      # 行程本来就是 µm
        hi = self._read_raw(xp.CMD_READ_POS_LIMIT_HIGH)
        if not 0.0 <= lo < hi <= XMT_MAX_TRAVEL_UM:
            raise StageNotConnected(
                f"行程读数 {lo} ~ {hi} µm 不合理（没标定好？），拒绝连接 —— "
                "行程是软限位的唯一权威来源，不能拿一个可疑值上岗。"
            )

        model = self._read_code(xp.CMD_MODEL)

        with self._status_lock:
            st = self._status
            st.connected = True
            st.servo = mode == LOOP_CLOSED
            st.travel_min, st.travel_max = lo, hi
            st.serial = str(link.port)
            st.stage_type = (
                f"E53.D1S-H 型号码 0x{model:02X}" if model is not None else "E53.D1S-H"
            )
            st.axis = "X"
        if mode == LOOP_CLOSED:
            pos = self._position_um()
            self._judge.reset(pos)
            with self._status_lock:
                self._status.position = pos
                self._status.target = pos
                self._status.updated_at = time.time()
            log.info("已连接 %s：行程 %.4f ~ %.4f µm，位置 %.4f µm；"
                     "停稳判据 ε=%.2f µm / 窗 %d 样本 / 连续 %d 窗（旋钮动过就得重测）",
                     link.port, lo, hi, pos, self._judge.eps_um,
                     self._judge.window, self._judge.need)
        else:
            # 开环下读回不是位移，别拿它当位置用；要动先开伺服。
            log.warning("设备当前是开环（19=%r）：位移设点会被拒绝，先在界面上开启伺服",
                        mode.decode("ascii", "replace"))

    def _close(self) -> None:
        self._close_quiet()

    def _close_quiet(self) -> None:
        link, self._link = self._link, None
        with self._status_lock:
            self._status.connected = False
        if link is None:
            return
        try:
            link.send(self._frame(xp.CMD_STOP_STREAM))
        except Exception:
            pass
        try:
            link.close()
        except Exception:
            log.warning("关闭串口失败", exc_info=True)

    def _require(self) -> Any:
        if self._link is None:
            raise StageNotConnected("设备未连接")
        return self._link

    # ------------------------------------------------------------------ 设备操作（owner 线程内）
    def _frame(self, b3: int, data: bytes = b"") -> bytes:
        """组一帧，地址用握手协商出来的那个。

        xmt_protocol 的组帧 helper 默认 addr=1，**地址不对的帧会被设备静默忽略**，
        所以本模块一律走这里组帧，不直接调那几个 helper 的默认形式。
        """
        return xp.frame(b3, data, addr=self._addr)

    def _read_raw(self, b3: int) -> float:
        """读一条「通道号 + 4 字节数值」的回包，**原样返回，不做任何折算**。

        注意单位并不统一：`6`/`8`（位移）回的是 4/3 µm，`27`/`35`（行程）
        回的就是 µm。所以谁要用这个值，谁负责说清单位 —— 位移一律走 _position_um()。
        """
        f = self._require().ask(self._frame(b3, bytes((0,))), b3)
        if f is None or f.value is None:
            raise StageError(f"命令 {b3} 无回包")
        return float(f.value)

    def _read_code(self, b3: int) -> Optional[int]:
        """读**单字节**回包的命令（53 单位码 / 78 型号码）。

        这两条的回包数据段只有 1 个字节，不是 Frame.value 认的「通道号 + 4 字节」，
        所以不能用 _read_raw —— 实测就是这么踩到的：53 明明回了包，
        按 4 字节去解却解出 None，看起来像"没回包"。
        """
        f = self._require().ask(self._frame(b3), b3)
        return int(f.data[0]) if f is not None and f.data else None

    def _read_loop_mode(self) -> bytes:
        f = self._require().ask(
            self._frame(xp.CMD_READ_LOOP_MODE, bytes((0,))), xp.CMD_READ_LOOP_MODE
        )
        return f.data[1:2] if f is not None else b""

    def _position_um(self) -> float:
        """读回位移并折算成 µm。

        **读回是 4/3 µm**（×0.75 才是 µm）—— 手册全书没写这个系数，是实测出来的。
        这个折算后的值可以写回成设点（软停、原地保持都靠它），但**只有折算过的
        才能写**：原样写回去，台子会跑偏 4/3 倍（实测 108.66 → 144.95 µm）。
        """
        return self._read_raw(xp.CMD_READ_POSITION) * XMT_READBACK_TO_UM

    def _write_target(self, um: float) -> None:
        """下发位移设点。参数**必须是 µm**：用户目标，或 _position_um() 的结果。"""
        self._require().send(xp.set_position(float(um), addr=self._addr))

    def _refresh_fast(self) -> None:
        """读一次位移并喂给判稳器。

        开环（已释放）时**不发布位置**：那时读回的含义没有依据，宁可留着上一次
        闭环的值，也不编一个数出来。on_target 一律置 False。
        （从没连过闭环时它保持初值 0.0 —— 界面上是「0.0000 + 未保持」，不是真值。）
        """
        if not self._status.servo:
            with self._status_lock:
                self._status.on_target = False
            return
        pos = self._position_um()
        on_target = self._judge.feed(pos)
        with self._status_lock:
            self._status.position = pos
            self._status.on_target = on_target
            self._status.updated_at = time.time()

    def _refresh_slow(self) -> None:
        mode = self._read_loop_mode()
        with self._status_lock:
            # 本设备没有过冲/错误码这两路读数，保持默认值，不编。
            self._status.servo = mode == LOOP_CLOSED

    def _tick_once(self) -> None:
        self._refresh_fast()
        self._tick += 1
        if self._tick % SLOW_QUERY_EVERY == 0:
            self._refresh_slow()

    def _move(self, target: float) -> None:
        if not self._status.servo:
            raise StageError("设备在开环（已释放）状态，位移设点没有意义，先开启伺服")
        if abs(target - self._status.position) <= self._judge.tol_um:
            # 设点丢帧与正常到位在这段距离上长得一样：读回校验失去了分辨力。
            log.warning(
                "目标 %.4f µm 离当前位置 %.4f µm 不到到达容差 %.2f µm："
                "这一段距离上，设点丢帧与正常到位分不出来（扫描步距要远大于容差）",
                target, self._status.position, self._judge.tol_um,
            )
        self._write_target(target)
        self._judge.reset(target)
        with self._status_lock:
            self._status.target = float(target)
            self._status.on_target = False
        self._refresh_fast()

    def _jog(self, delta: float) -> float:
        self._refresh_fast()
        target = self.clamp(self._status.position + delta)
        self._move(target)
        return target

    def _soft_stop(self) -> None:
        """软停：把**折算后的**当前真实位置写回成新目标。

        设备没有停止指令，这是能做到的全部；每次都要现读一次位置，
        不能拿 _status.position 顶替（界面上的值可能已经隔了几百毫秒）。
        """
        if not self._status.servo:
            return
        pos = self._position_um()
        self._write_target(pos)
        self._judge.reset(pos)
        with self._status_lock:
            self._status.target = pos
            self._status.on_target = False

    def _release_device(self) -> None:
        """切开环 + 输出写零（卸力）。

        顺序不能反：闭环下写电压没有意义。**不保证停在原位** ——
        切开环的瞬间输出还停在原电压，写零之后压电才往回走。
        """
        link = self._require()
        link.send(xp.set_loop_mode("O", addr=self._addr))
        link.send(xp.set_voltage(0.0, addr=self._addr))
        with self._status_lock:
            self._status.servo = False
            self._status.on_target = False

    def _set_servo(self, on: bool) -> None:
        if not on:
            self._release_device()
            return
        self._require().send(xp.set_loop_mode("C", addr=self._addr))
        pos = self._position_um()      # 闭环下读回才是位移
        self._write_target(pos)        # 原地保持：别跳回上次的设点
        self._judge.reset(pos)
        with self._status_lock:
            self._status.servo = True
            self._status.target = pos
        self._refresh_fast()

    def _read_on_target(self) -> bool:
        self._refresh_fast()
        return self._status.on_target

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
        """把目标夹进设备行程范围（行程是连接时从 27/35 读回来的）。"""
        st = self.status()
        return max(st.travel_min, min(st.travel_max, float(target)))

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
        """闭环原地保持，返回当前位置。

        先切闭环再读回位移、把它写成设点 —— 顺序反过来会跳回上次的设点。
        """
        self._call(self._set_servo, True)
        return self.status().position

    def set_velocity(self, velocity: float) -> float:
        """本设备没有速度指令（协议表里没有对应的 B3）。

        明确拒绝，不假装接受：速度由闭环自身决定，界面上的速度框对它没有意义。
        """
        raise StageError("芯明天 E53.D1S-H 没有速度设定指令（速度由闭环自身决定）")

    def stop_motion(self) -> StopResult:
        """软停：不再下发新目标 + 当前真实位置写回成新目标。

        设备没有停止指令，**已经在途的那段行程拦不住** —— 所以返回 SOFT，
        界面据此说话，别写成"保持伺服"。
        开环（已释放）下本来就"不再下发新目标"，这里什么都不做，但仍然返回 SOFT：
        它表达的是"这台设备只能软停"，不是"这次调用做了什么"。
        """
        self._call(self._soft_stop)
        return StopResult.SOFT

    def release(self) -> None:
        """释放：切开环 + 输出写零（卸力）。不保证停在原位，异常振动时用。"""
        self._call(self._release_device)

    def poll_on_target(self) -> bool:
        """只查一次停稳结论（软件判据，一次读数）。"""
        return bool(self._call(self._read_on_target))

    def wait_on_target(
        self, timeout: float, cancel: Optional[Callable[[], bool]] = None
    ) -> bool:
        """在调用方线程轮询等待停稳。owner 线程保持可响应。

        cancel 返回 True 时立即放弃等待（中止扫描用），返回 False。

        采样间隔 = XMT_POLL_MS + 抖动：**抖动不是装饰**，固定网格会与闭环振荡
        拍频，拍频极低时连续几次读数看起来纹丝不动，判据就瞎了。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel():
                return False
            if self.poll_on_target():
                return True
            time.sleep(
                (XMT_POLL_MS + random.uniform(0.0, XMT_POLL_JITTER_MS)) / 1000.0
            )
        return False
