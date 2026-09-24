"""scanner 的到位校验：偏差超过半个步距就判该点无效 —— 不采图、不入库、中止扫描。

设备层的到达容差是**绝对**的（PI 有自己的到位信号，XMT 是 0.2 µm 的读数比较）。步距 ≤ 容差时，
设点丢帧（台子停在上一点，正好差一个步距）会落进容差被判成到位，于是照常采图入库 —— 静默错点。
scanner 知道步距，所以补一道**相对**校验：偏差 > 步距/2 就失败。丢帧恰好是整整一个步距，
永远大于半个步距，所以这条在步距多小的时候都成立；反过来它只收半个步距，不会拿正常定位误差误伤。

**做不做由设备声明（stage.step_check）**：
  - PI：做（上面这套理由成立）。
  - XMT：**不做**（用户 2026-09 定）：等满稳定延时就直接采图，偏差一概不拦、不中止、不标无效。
    代价写在 docs/xmt/设备认识账.xml E12 —— 丢帧静默，只能事后从每点记下的读数看出来。

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
from backend.ccd import Shot  # noqa: E402
from backend.models import ScanRequest  # noqa: E402
from backend.scanner import Scanner  # noqa: E402
from backend.stage_api import (  # noqa: E402
    SETTLE_SOFTWARE,
    Caps,
    StageStatus,
    StopResult,
)


class FakeStage:
    """只实现 scanner 用得到的那几个方法。"""

    caps = Caps(
        name="假设备", platform="测试", has_on_target=True, has_stop_command=True,
        release_mode="servo_off", has_setpoint_ack=True, has_velocity=True,
        unit="µm", default_settle_ms=300,
    )

    # 这台假设备声明"要做采图前的相对校验"（PI 那样）；XMT 声明 False，见下面那两条用例
    step_check = True

    def __init__(self, travel: tuple[float, float] = (0.0, 100.0), lost: set | None = None,
                 wait_ok: bool = True, events: list | None = None):
        self.events = events if events is not None else []   # 与 FakeCapture 共用：钉顺序用
        self._position = 0.0
        self._target = 0.0
        self.travel = travel
        self.lost = set(lost or ())    # 这些目标上的设点"丢了"：台子不动
        self.wait_ok = wait_ok         # False = 设备层自己就确认不了到位（XMT 读回超差）
        self.moves: list[float] = []
        self.stops = 0
        self.settle_s: list[float] = []      # 每次等待时调用方给的稳定延时

    def move(self, target: float) -> float:
        self.moves.append(target)
        self._target = target
        if target not in self.lost:
            self._position = target
        return target

    def wait_on_target(self, timeout: float, cancel=None, settle_s: float = 0.0) -> bool:
        self.settle_s.append(settle_s)
        return self.wait_ok

    def poll(self) -> StageStatus:
        self.events.append("poll")
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
    def __init__(self, events: list | None = None) -> None:
        self.events = events if events is not None else []
        self.taken: list[tuple[int, int, float]] = []
        self.begun = 0
        self.ended = 0
        self.triggered = 0

    def begin(self) -> None:
        self.begun += 1

    def end(self) -> None:
        self.ended += 1

    def trigger(self) -> None:
        self.triggered += 1
        self.events.append("trigger")

    def capture(self, scan_id: int, index: int, position: float) -> Shot:
        self.events.append("capture")
        self.taken.append((scan_id, index, position))
        # 曝光逐点变化：好验证"每点记的是**这一帧自己**的曝光"，不是扫描级别的常量
        return Shot(path=f"fake/{scan_id}/{index}.png", exposure_us=10_000 + index * 1_000)


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


def test_point_records_the_exposure_of_that_shot():
    """每点入库的曝光 = 这一帧采图时的曝光（相机读回值）。

    扫描参数里没有曝光这一项，值是设备级的；老数据/没采到图时留 NULL，界面写「—」。
    """
    with with_temp_store():
        stage, capture = FakeStage(), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=12.0, count=3, settle_ms=0), stage, capture)
        assert st["status"] == "done", st
        pts = store.get_points(int(st["scan_id"]))
        assert [p["exposure_us"] for p in pts] == [10_000, 11_000, 12_000], pts


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


def test_device_that_skips_the_step_check_keeps_capturing():
    """设备声明不做这道校验（XMT）时：丢帧的点**照采、照入库**，扫描一路 done。

    这是用户 2026-09 定的口径 —— 只要达到稳定延时就开始采集。代价是丢帧静默：
    这一条同时钉住了"我们确实放弃了当场发现丢帧的能力"，改了这里就得改认识账 E12。
    """
    with with_temp_store():
        class NoStepCheck(FakeStage):
            step_check = False

        stage, capture = NoStepCheck(lost={12.0}), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=14.0, count=5, settle_ms=0),
                      stage, capture)
        assert st["status"] == "done", st
        assert [c[1] for c in capture.taken] == [0, 1, 2, 3, 4], f"每点都要采图：{capture.taken}"
        pts = store.get_points(int(st["scan_id"]))
        assert len(pts) == 5, f"错位的点也入库（如实记读数）：{pts}"
        # 丢帧那一点的读数是**上一目标**（11.0），不是 12.0 —— 事后只有它能暴露这件事
        assert round(pts[2]["actual_um"], 4) == 11.0, pts[2]
        assert round(pts[2]["target_um"], 4) == 12.0, pts[2]


def test_trigger_comes_before_the_position_read():
    """每点：**先触发曝光 → 再读位置 → 取帧**。

    两件事一个走 USB、一个走串口，互不相干；串行做就是白等一次曝光（实测 200 ms/点）。
    并行之后那次读数还落进了这一帧的积分窗，位置与图对得更齐。顺序反了这个收益就没了 ——
    所以这里把顺序钉死，改顺序会红。
    """
    with with_temp_store():
        events: list = []
        stage = FakeStage(events=events)
        capture = FakeCapture(events=events)
        st = run_scan(ScanRequest(start_um=10.0, stop_um=10.4, count=3, settle_ms=0),
                      stage, capture)
        assert st["status"] == "done", st
        # 开头那次 poll 是 start() 查伺服/行程用的；之后每点就是 trigger → poll → capture
        assert events[0] == "poll" and events[1:] == ["trigger", "poll", "capture"] * 3, events
        assert capture.triggered == 3, capture.triggered


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


def test_device_says_not_arrived_fails_without_capture():
    """设备层没确认到位（XMT：等满延时后读回超差）→ 不采图、不入库、整条 failed。

    这是本次「不判稳」改动新引入的失败路径：fixture 里 wait_on_target 默认恒真，
    只有这条用例能钉住它 —— 顺带钉住文案（不能把 XMT 的读回超差说成「10 s 超时」）。
    """
    with with_temp_store():
        stage, capture = FakeStage(wait_ok=False), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=12.0, count=3, settle_ms=0),
                      stage, capture)
        assert st["status"] == "failed", st
        assert capture.taken == [], f"没确认到位不该采图：{capture.taken}"
        assert store.get_points(int(st["scan_id"])) == [], "没确认到位的点不该入库"
        assert "10 s" not in st["message"], f"XMT 上没有 10 s 这回事：{st['message']}"
        assert "未确认到位" in st["message"], st["message"]


def test_settle_delay_goes_to_the_device_layer():
    """界面上的稳定延时必须原样传给设备层 —— 等待现在只有这一处，漏传就是不等。"""
    with with_temp_store():
        stage, capture = FakeStage(), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=12.0, count=3, settle_ms=250),
                      stage, capture)
        assert st["status"] == "done", st
        # 预逼近 + 3 个点：每次都拿到同一个延时
        assert stage.settle_s == [0.25] * (len(capture.taken) + 1), stage.settle_s


def test_missing_settle_falls_back_to_the_device_default():
    """API 直调没给稳定延时 → 用这台设备的默认值（caps.default_settle_ms），不是写死的 100。"""
    with with_temp_store():
        stage, capture = FakeStage(), FakeCapture()
        st = run_scan(ScanRequest(start_um=10.0, stop_um=11.0, count=2), stage, capture)
        assert st["status"] == "done", st
        assert set(stage.settle_s) == {0.3}, stage.settle_s


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
