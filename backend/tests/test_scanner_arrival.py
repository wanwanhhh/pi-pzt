"""scanner 的到位校验：偏差超过半个步距就判该点无效 —— 不采图、不入库、中止扫描。

设备层的到达容差是**绝对**的（XMT 是 0.2 µm）。步距 ≤ 容差时，设点丢帧（台子停在上一点，
正好差一个步距）会落进容差被判成到位，于是照常采图入库 —— 静默错点。scanner 知道步距，
所以补一道**相对**校验：偏差 > 步距/2 就失败。丢帧恰好是整整一个步距，永远大于半个步距，
所以这条在步距多小的时候都成立；反过来它只收半个步距，不会拿正常定位误差误伤。

起点终点相同的扫描（步距 0）不做这条校验：没有"上一点"可比。

直接跑：python backend/tests/test_scanner_arrival.py
"""
from __future__ import annotations

import logging
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import store  # noqa: E402
from backend.models import ScanRequest  # noqa: E402
from backend.scanner import Scanner  # noqa: E402
from backend.stage_api import SETTLE_SOFTWARE, StageStatus, StopResult  # noqa: E402


class FakeStage:
    """只实现 scanner 用得到的那几个方法。"""

    def __init__(self, travel: tuple[float, float] = (0.0, 100.0), lost: set | None = None):
        self._position = 0.0
        self._target = 0.0
        self.travel = travel
        self.lost = set(lost or ())    # 这些目标上的设点"丢了"：台子不动
        self.moves: list[float] = []
        self.stops = 0

    def move(self, target: float) -> float:
        self.moves.append(target)
        self._target = target
        if target not in self.lost:
            self._position = target
        return target

    def wait_on_target(self, timeout: float, cancel=None) -> bool:
        return True

    def poll(self) -> StageStatus:
        return self.status()

    def status(self) -> StageStatus:
        return StageStatus(
            settle_source=SETTLE_SOFTWARE, connected=True, servo=True,
            position=self._position, target=self._target, on_target=True,
            travel_min=self.travel[0], travel_max=self.travel[1],
        )

    def stop_motion(self) -> StopResult:
        self.stops += 1
        return StopResult.SOFT


class FakeCapture:
    def __init__(self) -> None:
        self.taken: list[tuple[int, int, float]] = []

    def capture(self, scan_id: int, index: int, position: float) -> str:
        self.taken.append((scan_id, index, position))
        return f"fake/{scan_id}/{index}.png"


def run_scan(req: ScanRequest, stage: FakeStage, capture: FakeCapture, timeout: float = 20.0):
    scanner = Scanner(stage, capture)
    scanner.start(req)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = scanner.state()
        if st["status"] not in ("running", "paused"):
            return st
        time.sleep(0.02)
    raise AssertionError("扫描没在超时内结束")


def with_temp_store():
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store.DB_PATH = str(pathlib.Path(tmp.name) / "t.db")
    store.init()
    return tmp


def test_normal_scan_records_every_point():
    with with_temp_store():
        stage, capture = FakeStage(), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=14.0, count=5, settle_ms=0), stage, capture)
        assert st["status"] == "done", st
        assert len(capture.taken) == 5, capture.taken
        pts = store.get_points(int(st["scan_id"]))
        assert len(pts) == 5
        assert [round(p["target_um"], 4) for p in pts] == [10.0, 11.0, 12.0, 13.0, 14.0]


def test_a_lost_setpoint_fails_the_scan_and_is_not_captured():
    """丢帧 = 台子停在上一点，正好差一个步距 —— 必须判失败，而且不采图不入库。"""
    with with_temp_store():
        stage, capture = FakeStage(lost={12.0}), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=14.0, count=5, settle_ms=0), stage, capture)
        assert st["status"] == "failed", st
        assert "偏差" in st["message"], st["message"]
        assert [c[1] for c in capture.taken] == [0, 1], f"第 2 点不该采图：{capture.taken}"
        pts = store.get_points(int(st["scan_id"]))
        assert [p["idx"] for p in pts] == [0, 1], f"失败的点不该入库：{pts}"


def test_deviation_inside_half_a_step_is_accepted():
    """台子偏一点但没到半个步距 —— 正常的定位误差，不能误伤。"""
    with with_temp_store():
        class SlightlyOff(FakeStage):
            def move(self, target: float) -> float:
                self.moves.append(target)
                self._target = target
                self._position = target + 0.3        # 步距 1 µm，偏 0.3 在半个步距内
                return target

        stage = SlightlyOff()
        capture = FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=14.0, count=5, settle_ms=0), stage, capture)
        assert st["status"] == "done", st
        assert len(capture.taken) == 5


def test_zero_step_scan_skips_the_check():
    """起终点相同（步距 0）时没有"上一点"可比，不能因此判失败。"""
    with with_temp_store():
        stage, capture = FakeStage(), FakeCapture()
        stage._position = 10.02          # 有点偏差，但不该被这条校验拦住
        st = run_scan(ScanRequest(start_um=10.0, stop_um=10.0, count=3, settle_ms=0), stage, capture)
        assert st["status"] == "done", st
        assert len(capture.taken) == 3


def main() -> int:
    logging.disable(logging.CRITICAL)
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
