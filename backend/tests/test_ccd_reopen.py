"""相机会话生命周期：离线自检，**不碰 SDK、不碰硬件**。

设计（见 backend/thorlabs_ccd.py 顶部三条规则）：
  会话 = 一次 open_camera 产生的一切 + 它用的那个 SDK 实例；
  会话只在「使用窗口」内存在（预览意图 ∪ 扫描持有 ∪ 一次动作）；
  任何异常（除输入校验）→ 丢弃会话，记 failure，不重试；状态是派生量。

直接跑：python backend/tests/test_ccd_reopen.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.thorlabs_ccd import (  # noqa: E402
    CameraError,
    CameraInputError,
    ThorlabsCamera,
    _Session,
)


class _Range:
    def __init__(self, lo, hi):
        self.min, self.max = lo, hi


class _Cam:
    """假相机句柄：够 _apply / _arm / _pump 走一遍（ROI 用角点语义，读回的宽高比 ROI 小 1）。"""

    def __init__(self, serial="34331"):
        self.serial = serial
        self.model = "CS165MU"
        self.exposure_time_range_us = _Range(40, 1_000_000)
        self.disposed = 0
        self._roi = (0, 0, 1439, 1079)
        self.exposure_time_us = 8000
        self.gain = 0
        self.armed = 0

    @property
    def roi(self):
        return self._roi

    @roi.setter
    def roi(self, value):
        x, y, w, h = (int(v) for v in value)
        if min(x, y, w, h) < 0 or w <= 0 or h <= 0:
            raise CameraError("tl_camera_set_roi() returned non-zero error code: 1003")
        self._roi = (x, y, w - 1, h - 1)      # 相机读回的是角点

    @property
    def image_width_pixels(self):
        return self._roi[2] + 1

    @property
    def image_height_pixels(self):
        return self._roi[3] + 1

    def arm(self, _n):
        self.armed += 1

    def disarm(self):
        self.armed = 0

    def issue_software_trigger(self):
        pass

    def get_pending_frame_or_null(self):
        return None

    def dispose(self):
        self.disposed += 1


class _SDK:
    """假 SDK 实例：discover 按脚本返回；记下自己有没有被 dispose。"""

    made: list = []
    script: list = []
    calls = 0
    open_error: Exception | None = None

    def __init__(self):
        type(self).made.append(self)
        self.disposed = 0

    def discover_available_cameras(self):
        """按**调用次数**推进脚本：等设备时只重跑 discover，SDK 只建一次。"""
        if not type(self).script:
            return []
        i = min(type(self).calls, len(type(self).script) - 1)
        type(self).calls += 1
        return list(type(self).script[i])

    def open_camera(self, serial):
        if type(self).open_error is not None:
            raise type(self).open_error
        return _Cam(serial)

    def dispose(self):
        self.disposed += 1


def _fake_sdk(script: list, open_error: Exception | None = None):
    import backend.thorlabs_ccd as tc

    old_cls, old_dll = tc._sdk_class, tc._require_dll_dir
    _SDK.made, _SDK.script, _SDK.open_error, _SDK.calls = [], script, open_error, 0
    tc._sdk_class = lambda: _SDK                 # type: ignore[assignment]
    tc._require_dll_dir = lambda: "fake"         # type: ignore[assignment]
    return lambda: (setattr(tc, "_sdk_class", old_cls),
                    setattr(tc, "_require_dll_dir", old_dll))


def _cam() -> ThorlabsCamera:
    return ThorlabsCamera()


def _session(sdk=None, cam=None) -> _Session:
    return _Session(sdk or _SDK(), cam or _Cam(), "34331", ((0, 0, 1440, 1080), (0, 0, 1440, 1080)))


# ---------------- 状态是派生量 ----------------

def test_state_is_derived_not_accumulated():
    cam = _cam()
    assert cam.state() == "idle", cam.state()          # 没会话、没失败
    cam._opening = True
    assert cam.state() == "opening"
    cam._opening = False
    cam._sess = _session()
    assert cam.state() == "preview"                    # 有会话、没被扫描持有
    cam._hold = 1
    assert cam.state() == "held"
    cam._hold = 0
    cam._sess = None
    cam._failure = "掉线了"
    assert cam.state() == "failed"
    cam._failure = ""
    assert cam.state() == "idle"


def test_no_session_means_no_sdk_access_at_all():
    """没会话时：没有帧可发、状态是 idle —— 结构上不可能碰 SDK。"""
    cam = _cam()
    assert cam.latest_jpeg() is None
    st = cam.status()
    assert st["state"] == "idle" and st["open"] is False
    assert st["centroid"] is None and st["frames"] == 0
    assert st["preview_roi"] == [0, 0, 1440, 1080]     # 显示配置值，不是"上次会话的残留"
    cam._discard("空会话")                              # 空会话上丢弃：no-op，不抛
    assert cam.state() == "idle"


# ---------------- 出错就丢会话 ----------------

def test_input_errors_keep_the_session():
    """填错一个曝光值不能把好好的会话扔掉（所以输入错误有独立类型）。"""
    cam = _cam()
    s = cam._sess = _session()
    cam._after_job_error("exposure", CameraInputError("曝光 99999999 µs 超出相机范围"))
    assert cam._sess is s, "输入错误不该丢会话"
    assert cam.state() == "preview"


def test_any_other_error_discards_handle_and_sdk():
    """DLL 报错 / 垃圾值引出的异常 → 句柄和 SDK 一起丢，并记下失败原因。"""
    cam = _cam()
    sess = cam._sess = _session()
    cam._after_job_error("preview", OSError("exception: access violation reading 0x0"))
    assert cam._sess is None
    assert cam.state() == "failed"
    assert "access violation" in cam.status()["failure"]
    assert sess.cam.disposed == 1, "句柄要 dispose"
    assert sess.sdk.disposed == 1, "**SDK 实例也要 dispose**（实测：留着它再 open 会崩进程）"


def test_normal_stop_is_not_a_failure():
    cam = _cam()
    cam._sess = _session()
    cam._discard("预览已停", failed=False)
    assert cam.state() == "idle" and cam.status()["failure"] == ""


# ---------------- 新鲜度看曝光，不看写死的秒数 ----------------

def test_frame_budget_follows_exposure():
    cam = _cam()
    cam._exposure_us = 200_000                        # 200 ms
    assert cam._frame_budget() > 1.3
    cam._exposure_us = 20_000_000                     # 20 s 长曝光：不能按 1 s 判掉线
    assert cam._frame_budget() > 40


def test_latest_jpeg_needs_a_live_session():
    cam = _cam()
    cam._sess = _session()
    cam._preview_wanted = True
    cam._sess.jpeg = b"jpeg"
    cam._sess.last_frame_at = time.monotonic()
    assert cam.latest_jpeg() == b"jpeg"

    cam._sess.last_frame_at = time.monotonic() - 99    # 帧陈旧
    assert cam.latest_jpeg() is None
    cam._sess.last_frame_at = time.monotonic()
    cam._hold = 1                                      # 扫描持有：不给预览帧
    assert cam.latest_jpeg() is None
    cam._hold = 0
    cam._preview_wanted = False                        # 人不要预览：不给
    assert cam.latest_jpeg() is None


# ---------------- 建会话：SDK 只建一次、等设备 -----------------------------------------------------------------

def test_open_session_builds_the_sdk_once_and_waits_for_the_device():
    """等设备出现时**只重跑 discover**，不重建 SDK（重建第二个会抛 already in use）。"""
    restore = _fake_sdk([[], [], ["34331"]])
    try:
        cam = _cam()
        t0 = time.monotonic()
        sess = cam._ensure_session(5.0)
        assert sess.serial == "34331"
        assert len(_SDK.made) == 1, f"SDK 建了 {len(_SDK.made)} 次，必须只建一次"
        assert time.monotonic() - t0 >= 0.4, "应该真的等过一轮（discover 为空 → sleep 0.5）"
    finally:
        restore()


def test_open_session_gives_up_and_disposes_the_sdk():
    restore = _fake_sdk([[]])
    try:
        cam = _cam()
        try:
            cam._ensure_session(0.4)
        except CameraError as exc:
            assert "没发现相机" in str(exc), exc
        else:
            raise AssertionError("没有设备时必须失败")
        assert len(_SDK.made) == 1 and _SDK.made[0].disposed == 1, "建不起来就别把 SDK 留着"
        assert cam.state() == "idle", "建会话失败不算 failed（还没用过）"
    finally:
        restore()


def test_open_session_reports_in_use_honestly():
    """SDK 被别人占着（或本进程闩锁没清）：如实报原因，别吞掉。"""
    restore = _fake_sdk([["34331"]], open_error=CameraError("TLCameraSDK is already in use"))
    try:
        cam = _cam()
        try:
            cam._ensure_session(0.0)
        except CameraError as exc:
            assert "already in use" in str(exc), exc
        else:
            raise AssertionError("开不起来必须抛")
        assert _SDK.made[0].disposed == 1
    finally:
        restore()


# ---------------- 重开 / 扫描持有 ----------------

def test_reopen_discards_both_then_builds_a_new_session():
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        old = cam._sess = _session()
        cam._preview_wanted = True
        cam._run(("reopen", 0.0))
        assert old.cam.disposed == 1 and old.sdk.disposed == 1, "旧会话（句柄 + SDK）必须一起丢"
        assert cam._sess is not None and cam._sess is not old
        assert cam.state() == "preview", "预览要着就该接着预览"
    finally:
        restore()


def test_preview_off_ends_the_window():
    cam = _cam()
    sess = cam._sess = _session()
    cam._preview_wanted = True
    cam._run(("preview", False, 0.0))
    assert cam._sess is None and sess.cam.disposed == 1
    assert cam.status()["failure"] == "", "用户自己停的不算失败"


def test_scan_holds_the_session_until_it_says_end():
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._run(("begin_scan",))
        assert cam._hold == 1 and cam.state() == "held" and cam._sess is not None
        held = cam._sess
        cam._preview_wanted = False
        cam._run(("end_scan",))
        assert cam._hold == 0
        assert cam._sess is None and held.cam.disposed == 1, "没人要预览就收工"
        assert cam.status()["failure"] == ""
    finally:
        restore()


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
