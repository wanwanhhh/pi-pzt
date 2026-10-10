"""相机会话生命周期：离线自检，**不碰 SDK、不碰硬件**。

设计（见 backend/thorlabs_ccd.py 顶部五条硬约束）：
  会话 = 一次 open_camera 产生的一切 + 它用的那个 SDK 实例；
  会话只在「使用窗口」内存在（预览意图 ∪ 扫描持有 ∪ 一次动作）；
  任何异常（除输入校验）→ 丢弃会话，记 failure，不重试；状态是派生量；
  **一个会话只 arm 一次**：设置只在会话出生时写，此后预览 / 手动保存 / 扫描共用一条连续流。

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


class _Frame:
    """假的一帧：_grab_frame 只用它的 image_buffer（真帧是 numpy uint16）。"""

    def __init__(self, arr):
        self.image_buffer = arr


class _Cam:
    """假相机句柄：够 _apply / _arm / _pump / _grab_frame 走一遍（ROI 用角点语义，读回的宽高比 ROI 小 1）。

    **写设置与 arm/disarm 全部计数**：离线用例靠这些计数钉住"一个会话只 arm 一次、
    保存不再碰相机"这条不变量（上机实测 2026-10-08：流跑着的时候再 arm 一轮，出帧就停摆）。
    """

    def __init__(self, serial="34331"):
        self.serial = serial
        self.model = "CS165MU"
        self.exposure_time_range_us = _Range(40, 1_000_000)
        self.disposed = 0
        self._roi = (0, 0, 1439, 1079)
        self._exposure = 8000
        self._gain = 0
        self.armed = 0
        self.arm_calls = 0
        self.disarm_calls = 0
        self.roi_writes = 0
        self.expo_writes = 0
        self.gain_writes = 0
        self.frames: list = []            # 待取帧队列：get_pending_frame_or_null 从队首拿

    @property
    def roi(self):
        return self._roi

    @roi.setter
    def roi(self, value):
        x, y, w, h = (int(v) for v in value)
        if min(x, y, w, h) < 0 or w <= 0 or h <= 0:
            raise CameraError("tl_camera_set_roi() returned non-zero error code: 1003")
        self.roi_writes += 1
        self._roi = (x, y, w - 1, h - 1)      # 相机读回的是角点

    @property
    def exposure_time_us(self):
        return self._exposure

    @exposure_time_us.setter
    def exposure_time_us(self, value):
        self.expo_writes += 1
        self._exposure = int(value)

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, value):
        self.gain_writes += 1
        self._gain = int(value)

    @property
    def image_width_pixels(self):
        return self._roi[2] + 1

    @property
    def image_height_pixels(self):
        return self._roi[3] + 1

    def arm(self, _n):
        self.armed += 1
        self.arm_calls += 1

    def disarm(self):
        self.armed = 0
        self.disarm_calls += 1

    def issue_software_trigger(self):
        pass

    def get_pending_frame_or_null(self):
        return self.frames.pop(0) if self.frames else None

    def dispose(self):
        self.disposed += 1


class _SDK:
    """假 SDK 实例：discover 按脚本返回；记下自己有没有被 dispose。"""

    made: list = []
    script: list = []
    calls = 0
    open_error: Exception | None = None
    frames: list = []            # 新开出来的假相机自带的帧队列（元素是 ndarray）

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
        cam = _Cam(serial)
        for arr in type(self).frames:
            cam.frames.append(_Frame(arr))
        return cam

    def dispose(self):
        self.disposed += 1


def _fake_sdk(script: list, open_error: Exception | None = None, frames=()):
    import backend.thorlabs_ccd as tc

    old_cls, old_dll = tc._sdk_class, tc._require_dll_dir
    _SDK.made, _SDK.script, _SDK.open_error, _SDK.calls = [], script, open_error, 0
    _SDK.frames = list(frames)
    tc._sdk_class = lambda: _SDK                 # type: ignore[assignment]
    tc._require_dll_dir = lambda: "fake"         # type: ignore[assignment]
    return lambda: (setattr(tc, "_sdk_class", old_cls),
                    setattr(tc, "_require_dll_dir", old_dll))


def _cam() -> ThorlabsCamera:
    return ThorlabsCamera()


def _session(sdk=None, cam=None) -> _Session:
    return _Session(sdk or _SDK(), cam or _Cam(), "34331", (0, 0, 1440, 1080))


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
    assert st["roi"] == [0, 0, 1440, 1080]             # 显示配置值，不是"上次会话的残留"
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


# ---------------- 一条连续流：一个会话只 arm 一次 ----------------
# 上机实测（2026-10-08，data/_grab_probe3.py）：预览连续出帧时做**两轮** disarm→arm，第二轮之后
# 相机一帧都不再出（1.4 s 后按"没出新帧"判掉线）；只做一轮安然无恙，设置写与取帧都不是必要条件。
# 旧代码的手动保存正是"disarm → 改设置 → arm → 取帧 → 再 disarm → 改回来 → 再 arm"，
# 于是每保存一次预览就死一次。下面这几条把"此后谁也不许再动相机"钉住。


def test_preview_then_save_never_touches_the_camera_again():
    """预览中保存原生帧：不许再 arm、不许 disarm、不许重写设置，会话也不许丢。"""
    import numpy as np

    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._run(("preview", True, 0.0))
        sess = cam._sess
        assert sess is not None and cam.state() == "preview"
        assert sess.cam.arm_calls == 1 and sess.cam.disarm_calls == 0, "开预览该 arm 且只 arm 一次"
        before = (sess.cam.roi_writes, sess.cam.expo_writes, sess.cam.gain_writes)

        sess.cam.frames.append(_Frame(np.full((4, 6), 7, dtype=np.uint16)))
        out = cam._run(("save",))
        assert out["shot"] is not None, "保存要真拿到一帧"

        assert sess.cam.arm_calls == 1, "保存不许再 arm（实测第二轮 arm 之后不再出帧）"
        assert sess.cam.disarm_calls == 0, "保存不许 disarm"
        assert (sess.cam.roi_writes, sess.cam.expo_writes, sess.cam.gain_writes) == before, \
            "保存不许重写相机设置（ROI/曝光/增益都别动）"
        assert cam._sess is sess, "预览要着：会话必须留着，预览接着跑"
    finally:
        restore()


def test_save_without_preview_is_a_one_shot_session():
    """预览关着时保存：开一个一次性会话，采完把句柄和 SDK 一起收掉（会话只在窗口内存在）。"""
    import numpy as np

    restore = _fake_sdk([["34331"]], frames=[np.full((4, 6), 3, dtype=np.uint16)])
    try:
        cam = _cam()
        out = cam._run(("save",))
        assert out["shot"] is not None, "一次性会话里那一帧要能拿到"
        assert cam._sess is None, "没人要预览：采完就收工"
        assert cam.state() == "idle" and cam.status()["failure"] == ""
        assert _SDK.made and _SDK.made[0].disposed == 1, "SDK 也要跟着收掉（E9）"
    finally:
        restore()


def test_begin_scan_reuses_the_preview_stream():
    """扫描在预览跑着的时候接手：**不重配、不重新 arm**，会话接着用。"""
    import numpy as np

    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._run(("preview", True, 0.0))
        sess = cam._sess
        before = (sess.cam.roi_writes, sess.cam.expo_writes, sess.cam.gain_writes)
        sess.cam.frames.extend(_Frame(np.zeros((2, 2), dtype=np.uint16)) for _ in range(3))

        cam._run(("begin_scan",))
        assert cam._sess is sess and cam.state() == "held", "接着用预览那条流"
        assert sess.cam.arm_calls == 1, "接手不许再 arm"
        assert sess.cam.disarm_calls == 0
        assert (sess.cam.roi_writes, sess.cam.expo_writes, sess.cam.gain_writes) == before, \
            "接手不许重配相机"
        assert len(sess.cam.frames) == 3, "接手不去清帧队列：实测没人取帧时驱动自己丢帧，攒不下来"
        cam._run(("end_scan",))
    finally:
        restore()


def test_frame_freshness_counts_only_while_frames_are_expected():
    """扫描期间没人取帧，那段时间不算"预览没出新帧" —— 交还后不许立刻把会话判掉线。

    上机实测（2026-10-08）：扫描交还后第一次节拍就丢了会话，报"预览 2.1s 没出新帧" ——
    其实扫描期间相机归扫描、根本没在取帧。判据只在"应该出帧"的时间里计时才对。
    """
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._preview_wanted = True
        cam._run(("begin_scan",))
        sess = cam._sess
        # 模拟扫描跑了很久：这期间没有任何预览取帧
        sess.frames = 5
        sess.last_frame_at = time.monotonic() - 60
        sess.frames_expected_since = time.monotonic() - 60

        cam._run(("end_scan",))                    # 交还预览：基线刷新
        assert cam._sess is sess
        cam._maybe_pump(0.0)
        assert cam._sess is sess, "刚交还就拿扫描期间没取帧去判掉线了"

        # 但真掉线还是要判：期望起点也推回 60 s 前，同一拍就该丢掉会话
        sess.frames_expected_since = time.monotonic() - 60
        cam._maybe_pump(0.0)
        assert cam._sess is None and "没出新帧" in cam.status()["failure"]
    finally:
        restore()


def test_end_scan_keeps_the_stream_when_preview_is_wanted():
    """扫描交还相机、预览还要着：会话留着接着出帧 —— 不许 disarm、不许重新 arm。"""
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._preview_wanted = True
        cam._run(("begin_scan",))
        sess = cam._sess
        assert cam.state() == "held" and sess.cam.arm_calls == 1
        cam._run(("end_scan",))
        assert cam._hold == 0 and cam._sess is sess, "预览要着就别收会话"
        assert sess.cam.disarm_calls == 0 and sess.cam.arm_calls == 1, "一个会话只 arm 一次"
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
