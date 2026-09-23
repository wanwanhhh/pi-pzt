"""设备层的公共面：能力声明、状态结构、上下层契约。

两台设备各有实现（pi_stage.py / xmt_stage.py），但 scanner 与 server 只认这一层。
**能力差异必须在这里显式声明，不许让上层猜。**

三处最容易出事的语义差：

- **停止**：PI 有专用停止指令，立即停住并保持伺服。XMT 没有停止指令，
  只能「不再下发新目标 + 把当前位置写回成新目标」——软停，慢一个帧间隔，可能有过冲。
- **到位**：PI 是控制器给的硬件信号（+ 一段固定延时）；XMT 没有到位信号，只能
  「等满固定的稳定延时 + 读一次回，看落没落在到达容差内」。两者都叫 on_target，
  保证级别不同，所以 `StageStatus.settle_source` 要写进每点元数据。
- **释放**：PI 是关伺服，台子回弹；XMT 只能切开环 + 输出写零（卸力），不保证停在原位。

能力标志**只用来决定界面文案与上层策略，绝不参与夹取与安全逻辑** ——
软限位和进硬件前的参数校验对两台设备一视同仁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .config import AXIS, FALLBACK_TRAVEL_MAX, FALLBACK_TRAVEL_MIN


# ---------------- 设备层异常（公共面的一部分） ----------------
# 必须在 stage_api 里定义：server 的 FastAPI handler 绑在 StageError 上做 409/503 映射，
# scanner 捕 StageAborted 判中止。实现各抛各的异常，映射就会失效、变成 500。

class StageError(RuntimeError):
    """设备层错误。"""


class StageAborted(StageError):
    """命令因急停或中止被丢弃。"""


class StageNotConnected(StageError):
    """设备未连接。"""


# 「释放」这台设备能做成什么样
RELEASE_SERVO_OFF = "servo_off"            # 真关伺服（PI）
RELEASE_OPEN_LOOP_ZERO = "open_loop_zero"  # 切开环 + 输出写零，不保证停在原位（XMT）


@dataclass(frozen=True)
class Caps:
    """一台设备的能力声明。"""

    name: str                 # "PI E-709" / "芯明天 E53.D1S-H"
    platform: str             # "Windows + Linux" / "仅 Windows"
    has_on_target: bool       # 控制器是否给出硬件到位信号
    has_stop_command: bool    # 是否有专用停止指令
    release_mode: str         # RELEASE_SERVO_OFF / RELEASE_OPEN_LOOP_ZERO
    has_setpoint_ack: bool    # 设点是否有应答（无应答就必须读回校验）
    has_velocity: bool        # 是否有速度设定指令（没有就必须明确拒绝，不能假装接受）
    unit: str                 # 设备原生单位；对外统一折算成 µm
    default_settle_ms: int    # 界面「稳定延时」的默认值（PI：到位后的延时；XMT：唯一的等待）


class StopResult(Enum):
    """stop_motion() 实际做到了什么。调用方据此决定要不要提醒用户。"""

    HARD = "hard"   # 立即停住并保持（PI）
    SOFT = "soft"   # 软停：不再发新目标 + 当前位置写回；慢一个帧间隔，可能过冲（XMT）
    NOOP = "noop"   # 什么都没做 —— 不该出现，出现就是 bug


# 「停稳」结论的来源，写进每点元数据：
# 同一列 on_target 背后可能是两种不同级别的保证，数据要能自证。
SETTLE_DEVICE = "device"      # 控制器自己的到位信号
SETTLE_SOFTWARE = "software"  # 后端按读数判出来的结论（XMT：等满稳定延时后读一次回、与目标比）
SETTLE_UNKNOWN = "unknown"    # 加这一列之前入库的旧行


def sleep_cancelable(seconds: float, cancel: Optional[Callable[[], bool]] = None) -> bool:
    """睡 seconds，期间每 50 ms 看一次 cancel。取消返回 False。

    稳定延时归设备层（wait_on_target 的 settle_s），两台设备都要睡同一段，
    所以放在公共面里，不各写一份。
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return True
        if cancel is not None and cancel():
            return False
        time.sleep(min(left, 0.05))


@dataclass
class StageStatus:
    # 必填、故意不给默认值：给默认就会指向不安全方向 ——
    # XMT 忘了设的话，整库每点都自称「硬件到位」。
    settle_source: str
    connected: bool = False
    position: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    servo: bool = False
    on_target: bool = False
    overflow: bool = False
    error_code: int = 0
    travel_min: float = FALLBACK_TRAVEL_MIN
    travel_max: float = FALLBACK_TRAVEL_MAX
    axis: str = AXIS
    serial: str = ""
    stage_type: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "position": round(self.position, 4),
            "target": round(self.target, 4),
            "velocity": self.velocity,
            "servo": self.servo,
            "on_target": self.on_target,
            "overflow": self.overflow,
            "error_code": self.error_code,
            "travel_min": self.travel_min,
            "travel_max": self.travel_max,
            "axis": self.axis,
            "serial": self.serial,
            "stage_type": self.stage_type,
            "updated_at": self.updated_at,
            "settle_source": self.settle_source,
        }


@runtime_checkable
class StageProto(Protocol):
    """scanner 与 server 只允许用这些方法；两台设备的实现都必须满足。

    这是「薄接口层」的全部：没有基类、没有注册表，只有形状约定 + 一个 `create_stage()`
    工厂。配置里选一台，同时只激活一台。

    注意：`caps` 是**数据成员**而不是方法，所以 `issubclass(Stage, StageProto)`
    会直接 TypeError（Non-method members）。运行时只用 `isinstance` 或
    `typing.get_protocol_members()`。
    """

    caps: Caps

    def start(self, timeout: float = 20.0) -> None: ...
    def shutdown(self) -> None: ...
    def estop(self) -> None: ...
    def status(self) -> StageStatus: ...
    def poll(self) -> StageStatus: ...
    def clamp(self, target: float) -> float: ...
    def move(self, target: float) -> float: ...
    def jog(self, delta: float) -> float: ...
    def set_servo(self, on: bool) -> None: ...
    def hold_here(self) -> float: ...
    def set_velocity(self, velocity: float) -> float: ...
    def stop_motion(self) -> StopResult: ...
    def release(self) -> None: ...
    def poll_on_target(self) -> bool: ...
    def wait_on_target(
        self,
        timeout: float,
        cancel: Optional[Callable[[], bool]] = None,
        settle_s: float = 0.0,
    ) -> bool: ...


def create_stage(**kwargs: Any) -> StageProto:
    """按配置选一台设备。同一时刻只激活一台。

    延迟导入：只有被选中的那台才会拉进它的依赖（PI 那条要 PIPython）。
    """
    from .config import DEVICE

    if DEVICE == "xmt":
        try:
            from .xmt_stage import XmtStage  # noqa: PLC0415
        except ImportError as exc:
            raise StageNotConnected(
                "PI_DEVICE=xmt，但 backend/xmt_stage.py 导入失败（多半是缺 pyserial）；"
                "装依赖，或把 PI_DEVICE 设回 pi"
            ) from exc
        return XmtStage(**kwargs)
    if DEVICE != "pi":
        raise ValueError(f"未知设备 {DEVICE!r}，只认 'pi' / 'xmt'")
    from .pi_stage import Stage  # noqa: PLC0415

    return Stage(**kwargs)
