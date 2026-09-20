"""16 位灰度 PNG 写入：只用标准库，不依赖 Pillow。

放在这里是因为 tools/ 下有几个脚本都要用（联通性探针、相机上机自检）。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def save_png_16(path: Path, img) -> int:
    """把 uint16 的 numpy 二维数组写成 16 位灰度 PNG，返回文件字节数。"""
    h, w = img.shape
    raw = b"".join(b"\x00" + img[y].astype(">u2").tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)
    return len(data)
