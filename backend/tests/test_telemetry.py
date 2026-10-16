"""遥测线程：扫描期间不再自己轮询设备（2026-10）。

这条改动以前**没有任何测试拦得住**，而它换掉的是曲线样本的来源 —— 写错了不会报错，
只会让曲线悄悄变空，或者变陈（把扫描器上一刻的读数当成这一刻的）。所以钉三件事：

  1. 扫描 running 时遥测**一次都不碰设备**（poll 计数必须是 0）；
  2. 每出现一个新点记**一个**样本，重复问同一个 index 不再记（否则一条读数会被摊成
     好几格，看着像采样率变高了）；
  3. 空闲 / 暂停时照旧自己轮询 —— 暂停不是 running，那两格界面不能僵。

与别的离线单测一样**不依赖测试框架**（README 里那条"逐个文件直接跑"的命令得真的跑到用例）：
直接跑：python backend/tests/test_telemetry.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TMP = None      # 临时库（server 导入时就会 init，别碰真库）
_TEL = None      # Telemetry 类，几条用例共用（模块只会导入一次）


class _Snap:
    """StageStatus 的最小替身：Telemetry 只用它的 as_dict()。"""

    def __init__(self, position: float) -> None:
        self._d = {
            "connected": True, "position": position, "target": 10.0, "velocity": 100.0,
            "servo": True, "on_target": True, "overflow": False, "error_code": 0,
            "travel_min": 0.0, "travel_max": 100.0, "axis": "X", "serial": "fake",
            "stage_type": "fake", "updated_at": 0.0, "settle_source": "device",
        }

    def as_dict(self) -> dict:
        return dict(self._d)


class _FakeStage:
    def __init__(self) -> None:
        self.polls = 0

    def poll(self):
        self.polls += 1
        return _Snap(10.0)

    def status(self):
        return _Snap(10.0)


class _FakeScanner:
    def __init__(self, status: str = "running") -> None:
        self.status = status
        self.index = 0

    def state(self) -> dict:
        return {
            "scan_id": 7, "status": self.status, "index": self.index, "count": 100,
            "target_um": round(self.index * 0.1, 4),
            "actual_um": round(self.index * 0.1, 4) if self.index else None,
            "message": "",
        }


def _telemetry_cls():
    """先把库指到临时目录再导入 server —— server 导入时就会 store.init()。"""
    global _TMP, _TEL
    if _TEL is None:
        from backend import store

        _TMP = tempfile.TemporaryDirectory()
        store.DB_PATH = Path(_TMP.name) / "t.db"
        from backend.server import Telemetry

        _TEL = Telemetry
    return _TEL


def _tick(tel) -> None:
    """让 _loop 正好跑一轮：把队列等待换成"立刻结束"。"""
    tel._stop.clear()
    tel._stop.wait = lambda timeout: tel._stop.set()
    tel._loop()


def test_scan_running_never_polls_the_device():
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = _telemetry_cls()(stage, scanner, hz=50.0, hz_scan=2.0)
    for i in range(1, 6):
        scanner.index = i
        _tick(tel)
    assert stage.polls == 0, "扫描期间遥测不该碰设备（多一个轮询者就是两份读数互相拖）"
    samples = tel.trace(0.0, time.time() + 1.0)
    assert len(samples) == 5, samples
    assert [s[1] for s in samples] == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert [s[2] for s in samples] == [0.1, 0.2, 0.3, 0.4, 0.5], "目标值跟着点走"


def test_same_point_is_not_recorded_twice():
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = _telemetry_cls()(stage, scanner, hz=50.0, hz_scan=2.0)
    scanner.index = 1
    for _ in range(3):
        _tick(tel)
    assert len(tel.trace(0.0, time.time() + 1.0)) == 1


def test_point_without_a_reading_is_skipped():
    """index 动了但 actual_um 还没记上（首点读之前）：不记样本，不编一个位置。"""
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = _telemetry_cls()(stage, scanner, hz=50.0, hz_scan=2.0)
    scanner.index = 0                      # actual_um is None
    _tick(tel)
    assert tel.trace(0.0, time.time() + 1.0) == []


def test_idle_and_paused_still_poll():
    for status in ("idle", "paused"):
        stage, scanner = _FakeStage(), _FakeScanner(status)
        tel = _telemetry_cls()(stage, scanner, hz=50.0, hz_scan=2.0)
        scanner.index = 1
        _tick(tel)
        assert stage.polls == 1, f"{status} 时遥测必须自己轮询（否则界面会僵住）"
        samples = tel.trace(0.0, time.time() + 1.0)
        assert len(samples) == 1 and samples[0][1] == 10.0, (status, samples)


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
