"""CCD / 外部设备联动接口。

扫描执行器在每点停稳后调用 capture()，返回的路径写回该点元数据。
现在没有相机，用 DummyCapture 生成灰度占位图（灰度值编码位置），
先把"到位 -> 采图 -> 落盘 -> 写库 -> 界面显示"整条链路打通；
接真相机时只换 capture 的实现，不动执行器和存储。
"""
from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path
from typing import Protocol

from .config import CCD_BACKEND, IMAGE_DIR

log = logging.getLogger(__name__)


class Capture(Protocol):
    def capture(self, scan_id: int, index: int, position_um: float) -> str | None:
        """在当前位置采一帧。返回相对 DATA_DIR 的图片路径，None 表示未采图。"""


class NullCapture:
    """无相机：不落盘，图像字段留空。"""

    def capture(self, scan_id: int, index: int, position_um: float) -> str | None:
        return None


def _png_gray(side: int, value: int) -> bytes:
    """生成一张 side×side 的 8 位灰度 PNG，不依赖第三方库。"""
    raw = b"".join(b"\x00" + bytes([value]) * side for _ in range(side))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class DummyCapture:
    """占位相机：灰度值随位置变化，便于肉眼确认每点采图与顺序。"""

    def __init__(self, side: int = 64) -> None:
        self._side = side

    def capture(self, scan_id: int, index: int, position_um: float) -> str | None:
        name = f"scan{scan_id:04d}_{index:05d}.png"
        value = int(max(0.0, min(100.0, position_um)) * 2.55)
        (IMAGE_DIR / name).write_bytes(_png_gray(self._side, value))
        return f"images/{name}"


def make_capture() -> Capture:
    if CCD_BACKEND == "null":
        log.info("CCD 后端：null（不采图）")
        return NullCapture()
    log.info("CCD 后端：dummy（生成占位灰度图）")
    return DummyCapture()
