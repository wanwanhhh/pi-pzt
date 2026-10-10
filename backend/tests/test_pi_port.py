"""PI 串口查找：离线自检，**不碰硬件、也不需要插着设备**。

find_serial_port() 两边都按设备找口：Linux 看 /dev/serial/by-id，Windows 看 VID:PID —— 换 USB 口、
COM 号变了都不用改配置。Windows 分支是 2026-10-09 加的（E-709 在 Windows 上也是 FTDI 虚拟串口，
不装 PI Software Suite 也能走 PISerial 这条纯 Python 命令层），所以把判据钉在这里。

直接跑：python backend/tests/test_pi_port.py
"""
from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import pi_stage                                     # noqa: E402

_Port = namedtuple("_Port", "device vid pid description")

_NOT_PI = _Port("COM1", 0x0403, 0x6001, "USB Serial Converter")
_PI = _Port("COM7", pi_stage.PI_USB_VID, pi_stage.PI_USB_PID,
            "PI E-709 Digital Piezo Controller (COM7)")


class _fake_windows:
    """把 sys.platform 与 pyserial 的口列表临时换成假的；出块必须还原。"""

    def __init__(self, ports):
        self._ports = list(ports)

    def __enter__(self):
        import serial.tools.list_ports as lp

        self._lp, self._comports, self._platform = lp, lp.comports, sys.platform
        lp.comports = lambda: list(self._ports)
        sys.platform = "win32"

    def __exit__(self, *exc):
        self._lp.comports = self._comports
        sys.platform = self._platform
        return False


def test_windows_picks_the_pi_port_by_vid_pid():
    """Windows：认 VID:PID，不认 COM 号 —— 列表里别的口不能误选。"""
    with _fake_windows([_NOT_PI, _PI]):
        assert pi_stage.find_serial_port() == "COM7"


def test_windows_without_the_controller_says_so():
    """Windows：一个 PI 口都没有时要报错说清楚，不许随便挑一个口用。"""
    with _fake_windows([_NOT_PI]):
        try:
            pi_stage.find_serial_port()
        except pi_stage.StageNotConnected as exc:
            assert "1A72:100E" in str(exc), exc
        else:
            raise AssertionError("没有 PI 串口时必须报错")


def test_platform_is_restored_after_the_fake():
    """临时改成 win32 之后必须还原（后面的用例要按真实平台走）。"""
    before = sys.platform
    with _fake_windows([_PI]):
        assert sys.platform == "win32"
    assert sys.platform == before, f"{before} → {sys.platform}"


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
