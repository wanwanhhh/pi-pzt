"""芯明天 E53.D1S-H 串口协议：组帧、校验、数值编解码、增量解析。

资料与完整 119 条指令表见 docs/xmt/README.md 与 docs/xmt/protocol/。
三条硬约束决定了这里的形态：

1. 帧 = AA | 地址 | 包长 | B3 | B4 | 数据段 | BCC；包长 = 数据段长 + 6（含帧头与校验）。
2. 数值是「符号 + 幅值」而不是补码，分辨率固定 1e-4，幅值上限 32767.9999。
3. 串口是字节流：半包、粘包、垃圾字节都会出现，解析必须增量且能重同步。

本模块不碰串口，是纯函数加一个解析状态机，可离线单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

HEADER = 0xAA
BROADCAST = 0x00

# 帧长合法区间。最短 6 字节（无数据段的查询类），包长字段本身是 uchar。
MIN_FRAME = 6
MAX_FRAME = 255

# 数值编码：v = ((kk0 & 0x7F) << 8 | kk1) + ((kk2 << 8) | kk3) * 1e-4
VALUE_SCALE = 10000
VALUE_MAX = 32767.9999

# B3 指令码。只列本仓库用得到的，其余见指令总表。
CMD_SET_VOLTAGE = 0x00          # 单路电压（开环）
CMD_SET_POSITION = 0x01         # 单路位移（闭环设点）；注意：无应答
CMD_CLEAR_ALL = 0x04            # 多路清零（会把输出写到零，是一次真实运动）
CMD_READ_VOLTAGE = 0x05
CMD_READ_POSITION = 0x06
CMD_STREAM_VOLTAGE = 0x07       # 实时读单路电压，数据段 = 通道号 + 发送周期(ms)
CMD_STREAM_POSITION = 0x08      # 实时读单路位移，同上
CMD_STOP_STREAM = 0x0B
CMD_LOOP_MODE = 0x12            # 开闭环设置，数据段 = 通道号 + 'O'/'C'
CMD_READ_LOOP_MODE = 0x13
CMD_READ_POS_LIMIT_HIGH = 0x1B  # 27
CMD_READ_POS_LIMIT_LOW = 0x23   # 35
CMD_READ_UNIT = 0x35            # 53
CMD_READ_ADDRESS = 0x47         # 47 读地址；下发帧的地址字节必须写 0（广播）
CMD_HANDSHAKE = 0x4D            # 77，回 b"OK"
CMD_MODEL = 0x4E                # 78
CMD_POWER_INFO = 0x50           # 80 电源信息读取1，回 61 字节，[24]..[55] 是命令能力位图
CMD_STAGE_INFO = 0x52           # 82 台子信息读取，回 15 字节

# B3=53 回的单位码
UNITS = {0: "mrad", 1: "位移", 2: "角秒", 3: "µrad", 4: "mm", 5: "nm", 6: "周期"}


def bcc(data: bytes) -> int:
    """帧头到数据段末尾的逐字节 XOR。"""
    x = 0
    for b in data:
        x ^= b
    return x


def frame(b3: int, data: bytes = b"", addr: int = 1, b4: int = 0) -> bytes:
    """组一帧。上下行报文格式相同，所以回包也能用它拼（测试用）。"""
    body = bytes((HEADER, addr, len(data) + 6, b3, b4)) + data
    return body + bytes((bcc(body),))


def encode_value(v: float) -> bytes:
    """浮点 → 4 字节：符号 + 幅值，分辨率 1e-4。"""
    if not -VALUE_MAX <= v <= VALUE_MAX:
        raise ValueError(f"超出协议幅值上限 ±{VALUE_MAX}: {v}")
    units = round(abs(v) * VALUE_SCALE)
    whole, frac = divmod(units, VALUE_SCALE)
    kk0 = (whole >> 8) & 0x7F
    if v < 0:
        kk0 |= 0x80
    return bytes((kk0, whole & 0xFF, frac >> 8, frac & 0xFF))


def decode_value(b: bytes) -> float:
    """4 字节 → 浮点。"""
    kk0, kk1, kk2, kk3 = b[0], b[1], b[2], b[3]
    v = ((kk0 & 0x7F) << 8 | kk1) + ((kk2 << 8) | kk3) / VALUE_SCALE
    return -v if kk0 & 0x80 else v


# ---------------- 常用命令 ----------------

def read_channel(b3: int, ch: int = 0) -> bytes:
    """数据段只有一个通道号的读命令（5/6/19/27/35）。"""
    return frame(b3, bytes((ch,)))


def set_position(pos: float, ch: int = 0) -> bytes:
    """闭环设点。"""
    return frame(CMD_SET_POSITION, bytes((ch,)) + encode_value(pos))


def set_voltage(volts: float, ch: int = 0) -> bytes:
    """开环设电压。"""
    return frame(CMD_SET_VOLTAGE, bytes((ch,)) + encode_value(volts))


def set_loop_mode(mode: str, ch: int = 0) -> bytes:
    """mode: 'O' 开环 / 'C' 闭环。"""
    return frame(CMD_LOOP_MODE, bytes((ch,)) + mode.encode("ascii"))


def stream_position(period_ms: int, ch: int = 0) -> bytes:
    """启动周期推送位移。period_ms 手册给的范围是 1~255，真机下限待实测。"""
    return frame(CMD_STREAM_POSITION, bytes((ch, period_ms)))


# ---------------- 解析 ----------------

@dataclass(frozen=True)
class Frame:
    """一帧解析结果。raw 含帧头与校验字节，data 是纯数据段。"""

    addr: int
    b3: int
    b4: int
    data: bytes
    raw: bytes

    @property
    def value(self) -> Optional[float]:
        """数据段形如「通道号 + 4 字节数值」时解出数值，否则 None。"""
        return decode_value(self.data[1:5]) if len(self.data) >= 5 else None

    @property
    def channel(self) -> Optional[int]:
        """数据段首字节是通道号时给出，否则 None。"""
        return self.data[0] if self.data else None


class Parser:
    """增量解析串口字节流。

    半包、粘包、垃圾字节都要能扛：先对齐帧头，长度够了才取整帧，
    校验不过就丢一个字节重新找帧头 —— 包长字段本身也可能被噪声改坏，
    所以不能盲目相信它。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped = 0   # 丢掉的字节数（含校验失败的帧）
        self.bad_bcc = 0   # 校验失败的帧数

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf += chunk
        out: list[Frame] = []
        while (f := self._take()) is not None:
            out.append(f)
        return out

    def _take(self) -> Optional[Frame]:
        buf = self._buf
        while True:
            i = buf.find(HEADER)
            if i < 0:
                self.dropped += len(buf)
                buf.clear()
                return None
            if i:
                self.dropped += i
                del buf[:i]
            if len(buf) < 3:
                return None                    # 还不知道包长
            n = buf[2]
            if not MIN_FRAME <= n <= MAX_FRAME:
                self.dropped += 1
                del buf[0]
                continue
            if len(buf) < n:
                return None                    # 整帧还没到齐
            raw = bytes(buf[:n])
            if bcc(raw[:-1]) != raw[-1]:
                self.bad_bcc += 1
                self.dropped += 1
                del buf[0]
                continue
            del buf[:n]
            return Frame(addr=raw[1], b3=raw[3], b4=raw[4], data=raw[5:-1], raw=raw)
