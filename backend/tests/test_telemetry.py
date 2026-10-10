"""遥测线程：扫描期间不再自己轮询设备（2026-10）。

这条改动以前**没有任何测试拦得住**，而它换掉的是曲线样本的来源 —— 写错了不会报错，
只会让曲线悄悄变空，或者变陈（把扫描器上一刻的读数当成这一刻的）。所以钉三件事：

  1. 扫描 running 时遥测**一次都不碰设备**（poll 计数必须是 0）；
  2. 每出现一个新点记**一个**样本，重复问同一个 index 不再记（否则一条读数会被摊成
     好几格，看着像采样率变高了）；
  3. 空闲 / 暂停时照旧自己轮询 —— 暂停不是 running，那两格界面不能僵。

Telemetry 住在 server.py 里，而 server.py **导入时就会 store.init()**，所以这里先把
DB_PATH 指到临时库再导入（模块级只导入一次，后面几个用例共用）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


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


@pytest.fixture(scope="module")
def telemetry_cls(tmp_path_factory):
    from backend import store

    store.DB_PATH = tmp_path_factory.mktemp("db") / "t.db"   # 导入 server 时会 init，别碰真库
    from backend.server import Telemetry

    return Telemetry


def _tick(tel) -> None:
    """让 _loop 正好跑一轮：把队列等待换成"立刻结束"。"""
    tel._stop.clear()
    tel._stop.wait = lambda timeout: tel._stop.set()
    tel._loop()


def test_scan_running_never_polls_the_device(telemetry_cls):
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = telemetry_cls(stage, scanner, hz=50.0, hz_scan=2.0)
    for i in range(1, 6):
        scanner.index = i
        _tick(tel)
    assert stage.polls == 0, "扫描期间遥测不该碰设备（多一个轮询者就是两份读数互相拖）"
    samples = tel.trace(0.0, time.time() + 1.0)
    assert len(samples) == 5, samples
    assert [s[1] for s in samples] == [0.1, 0.2, 0.3, 0.4, 0.5]
    assert [s[2] for s in samples] == [0.1, 0.2, 0.3, 0.4, 0.5], "目标值跟着点走"


def test_same_point_is_not_recorded_twice(telemetry_cls):
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = telemetry_cls(stage, scanner, hz=50.0, hz_scan=2.0)
    scanner.index = 1
    for _ in range(3):
        _tick(tel)
    assert len(tel.trace(0.0, time.time() + 1.0)) == 1


def test_point_without_a_reading_is_skipped(telemetry_cls):
    """index 动了但 actual_um 还没记上（首点读之前）：不记样本，不编一个位置。"""
    stage, scanner = _FakeStage(), _FakeScanner("running")
    tel = telemetry_cls(stage, scanner, hz=50.0, hz_scan=2.0)
    scanner.index = 0                      # actual_um is None
    _tick(tel)
    assert tel.trace(0.0, time.time() + 1.0) == []


@pytest.mark.parametrize("status", ["idle", "paused"])
def test_idle_and_paused_still_poll(telemetry_cls, status):
    stage, scanner = _FakeStage(), _FakeScanner(status)
    tel = telemetry_cls(stage, scanner, hz=50.0, hz_scan=2.0)
    scanner.index = 1
    _tick(tel)
    assert stage.polls == 1, f"{status} 时遥测必须自己轮询（否则界面会僵住）"
    samples = tel.trace(0.0, time.time() + 1.0)
    assert len(samples) == 1 and samples[0][1] == 10.0, samples
