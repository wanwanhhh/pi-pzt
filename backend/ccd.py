"""CCD / 外部设备联动接口。

每点的顺序是 **trigger() → capture()**：先让相机开始曝光，再去读位置、拿这一帧。
两件事一个走相机、一个走串口，串行做就是白等一次曝光（实测 200 ms/点）；
并行之后那次读数还落在这一帧的积分窗里，位置与图对得更齐。
**图片路径 + 这一帧当时用的曝光**一起写回该点元数据（曝光是相机读回值，不是扫描参数
—— 扫描参数里根本没有曝光这一项）。
（XMT 那边没有「停稳」这一步，也不做任何到位判据：等满稳定延时就直接采图，
见 stage_api.wait_on_target 与 docs/xmt/设备认识账.xml E12）。
现在没有相机，用 DummyCapture 生成灰度占位图（灰度值编码位置），
先把"到位 -> 采图 -> 落盘 -> 写库 -> 界面显示"整条链路打通；
接真相机时只换 capture 的实现，不动执行器和存储。
"""
from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from typing import Protocol

from .config import CCD_BACKEND, IMAGE_DIR

log = logging.getLogger(__name__)

# 真相机（索雷博 CS165MU）的对象。预览接口要用它，所以在这里建一次、全进程共用。
# 开关相机本身由它自己的 owner 线程管（见 thorlabs_ccd.py 顶部四条硬约束）。
_thorlabs = None


@dataclass(frozen=True)
class Shot:
    """一点采下来的东西。

    path        相对 DATA_DIR 的图片路径；None = 这一帧没采到（没落盘）。
    exposure_us **这一帧采图时相机上的曝光**（µs，相机读回值）。None = 无从记录：
                占位相机没有曝光概念、或加这一列之前入库的老数据。
                不许拿"当前曝光"去倒填 —— 那是猜，不是记录。
    """

    path: str | None = None
    exposure_us: int | None = None


class Capture(Protocol):
    def begin(self) -> None:
        """一次扫描开始：相机归扫描用（预览让位、会话保持到扫描结束）。"""

    def end(self) -> None:
        """扫描结束（含失败/中止）：交还相机。**不许抛异常**。"""

    def trigger(self) -> None:
        """开始曝光（每点一次）。**不阻塞等帧** —— 调用方紧接着去读位置。"""

    def capture(self, scan_id: int, index: int, position_um: float) -> Shot:
        """取**已经触发**的那一帧。返回图片路径与这一帧的曝光（见 Shot）。"""


class NullCapture:
    """无相机：不落盘，图像字段留空。"""

    def begin(self) -> None:
        pass

    def end(self) -> None:
        pass

    def trigger(self) -> None:
        pass

    def capture(self, scan_id: int, index: int, position_um: float) -> Shot:
        return Shot()


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

    def begin(self) -> None:
        pass

    def end(self) -> None:
        pass

    def trigger(self) -> None:
        pass                       # 占位相机没有曝光这回事，触发是空动作

    def capture(self, scan_id: int, index: int, position_um: float) -> Shot:
        name = f"scan{scan_id:04d}_{index:05d}.png"
        value = int(max(0.0, min(100.0, position_um)) * 2.55)
        (IMAGE_DIR / name).write_bytes(_png_gray(self._side, value))
        return Shot(path=f"images/{name}")     # 占位相机没有曝光可言，如实留空


class ThorlabsCapture:
    """索雷博 CS165MU：每点采一帧原生全幅、存 16 位 PNG。

    **不裁剪**：保存一律用原生 1440x1080，预览的小 ROI 只是预览用。
    实际动作全在 backend/thorlabs_ccd.py 的 owner 线程里，这里只做转发。
    """

    def __init__(self) -> None:
        self.camera = thorlabs_camera()

    def begin(self) -> None:
        """扫描接手相机：设备层持有会话、预览让位（状态变 held）。"""
        self.camera.begin_scan()

    def end(self) -> None:
        """扫描交还相机。**不许抛**：结束路径上抛异常会盖掉真正的失败原因。"""
        try:
            self.camera.end_scan()
        except Exception as exc:                  # noqa: BLE001
            log.warning("扫描结束时交还相机失败：%s", exc)

    def trigger(self) -> None:
        self.camera.trigger()

    def capture(self, scan_id: int, index: int, position_um: float) -> Shot:
        path, exposure_us = self.camera.capture(scan_id, index, position_um)
        return Shot(path=path, exposure_us=exposure_us)


def thorlabs_camera():
    """取（必要时创建）全进程唯一的相机对象。不打开相机，只建对象。"""
    global _thorlabs
    if _thorlabs is None:
        from .thorlabs_ccd import ThorlabsCamera   # 延迟导入：非 thorlabs 后端不拉 PIL/SDK

        _thorlabs = ThorlabsCamera()
    return _thorlabs


def make_capture() -> Capture:
    if CCD_BACKEND == "null":
        log.info("CCD 后端：null（不采图）")
        return NullCapture()
    if CCD_BACKEND == "thorlabs":
        log.info("CCD 后端：thorlabs CS165MU（原生全幅，16 位 PNG）")
        return ThorlabsCapture()
    log.info("CCD 后端：dummy（生成占位灰度图）")
    return DummyCapture()
