"""相机会话生命周期：离线自检，**不碰 SDK、不碰硬件**。

设计（见 backend/thorlabs_ccd.py 顶部五条硬约束）：
  会话 = 一次 open_camera 产生的一切 + 它用的那个 SDK 实例；
  会话只在「使用窗口」内存在（预览意图 ∪ 扫描持有 ∪ 一次动作）；
  任何异常（除输入校验）→ 丢弃会话，记 failure，不重试；状态是派生量；
  **一个会话只 arm 一次**：设置只在会话出生时写；预览与手动保存共用连续流，扫描另用单帧会话
  （触发模式在会话出生时定，不能在 arm 之后改）。

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

    # 触发模式写不进去的假相机（钉住"读回不一致就不拿它去扫描"）
    ignore_trigger_mode_write = False

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
        self.trigger_calls = 0
        self.image_poll_timeout_ms = 2000
        self.frame_time_us = 40_718       # 曝光 8 ms 时的实测值（认识账 E12）
        self._frames_per_trigger = 0
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
    def frames_per_trigger_zero_for_unlimited(self):
        return self._frames_per_trigger

    @frames_per_trigger_zero_for_unlimited.setter
    def frames_per_trigger_zero_for_unlimited(self, value):
        if not type(self).ignore_trigger_mode_write:
            self._frames_per_trigger = int(value)

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
        self.trigger_calls += 1

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


def _session(sdk=None, cam=None, single_frame: bool = False) -> _Session:
    return _Session(sdk or _SDK(), cam or _Cam(), "34331", (0, 0, 1440, 1080),
                    single_frame=single_frame)


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


# ---------------- 两种会话：一个会话只 arm 一次 ----------------
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


def test_begin_scan_switches_to_a_single_frame_session():
    """扫描接手：预览那条连续流换成**单帧触发**会话（模式是会话级设置，不能原地改）。"""
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._run(("preview", True, 0.0))
        old = cam._sess
        assert old is not None and old.single_frame is False

        cam._run(("begin_scan",))
        sess = cam._sess
        assert cam.state() == "held"
        assert sess is not old, "预览会话不能拿来扫描（触发模式不同）"
        assert old.cam.disposed == 1 and old.sdk.disposed == 1, "旧会话（句柄 + SDK）要一起丢"
        assert sess.single_frame is True
        assert sess.cam.frames_per_trigger_zero_for_unlimited == 1, "扫描必须是一次触发一帧"
        assert sess.cam.arm_calls == 1, "一个会话只 arm 一次"
        assert sess.cam.trigger_calls == 0, "arm 后不预触发：第一帧要等扫描到位后才开始"
        cam._run(("end_scan",))
    finally:
        restore()


def test_frame_freshness_counts_only_while_frames_are_expected():
    """扫描期间没人取帧，那段时间不算"预览没出新帧" —— 交还后不许立刻把会话判掉线。

    现在交还时会重建预览会话（单帧 → 连续），旧的计时天然被切断；这条同时钉住：
    新会话不会被旧的"没出新帧"带下水，而真掉线仍然照判。
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

        cam._run(("end_scan",))                    # 交还预览：丢掉单帧会话、按连续模式重开
        live = cam._sess
        assert live is not None and live is not sess
        cam._maybe_pump(0.0)
        assert cam._sess is live, "刚交还就拿扫描期间没取帧去判掉线了"

        # 但真掉线还是要判：期望起点也推回 60 s 前，同一拍就该丢掉会话
        live.frames = 5
        live.last_frame_at = time.monotonic() - 60
        live.frames_expected_since = time.monotonic() - 60
        cam._maybe_pump(0.0)
        assert cam._sess is None and "没出新帧" in cam.status()["failure"]
    finally:
        restore()


def test_end_scan_rebuilds_a_continuous_preview_session():
    """扫描交还、预览还要着：丢掉单帧会话，按连续模式重开 —— 预览那条流不能是单帧的。"""
    restore = _fake_sdk([["34331"]])
    try:
        cam = _cam()
        cam._preview_wanted = True
        cam._run(("begin_scan",))
        scan_sess = cam._sess
        assert cam.state() == "held" and scan_sess.single_frame is True

        cam._run(("end_scan",))
        assert cam._hold == 0
        assert scan_sess.cam.disposed == 1 and scan_sess.sdk.disposed == 1
        sess = cam._sess
        assert sess is not None and sess is not scan_sess
        assert sess.single_frame is False, "预览要连续视频"
        assert sess.cam.arm_calls == 1
        assert sess.cam.trigger_calls >= 1, "连续流靠第一发触发起来"
        assert cam.state() == "preview"
    finally:
        restore()


# ---------------- 单帧触发：扫描严格性的根基 ----------------

def test_single_frame_session_never_auto_triggers():
    """单帧会话取帧时缺帧**不补触发**（补一发就多一帧，整条对应关系会错位）；连续会话照补。"""
    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    sess.armed = True
    assert cam._next_frame(sess) is None
    assert sess.cam.trigger_calls == 0

    cont = cam._sess = _session()
    cont.armed = True
    assert cam._next_frame(cont) is None
    assert cont.cam.trigger_calls == 1


def test_frame_wait_follows_frame_time_not_a_fixed_30_s():
    """等帧预算跟着相机自报的帧时间走，不写死 30 s。"""
    cam = _cam()
    sess = _session(single_frame=True)
    sess.frame_time_us = 40_718           # 曝光 12 ms 的实测值
    assert cam._frame_wait_s(sess) == 1.0, "短曝光也要给至少 1 s"
    sess.frame_time_us = 1_028_000        # 曝光 1 s 的帧时间
    assert 2.5 <= cam._frame_wait_s(sess) <= 2.7
    sess.frame_time_us = 0                # 读不到帧时间：按曝光算
    cam._exposure_us = 300_000
    assert 1.0 <= cam._frame_wait_s(sess) <= 1.2


def test_missing_frame_is_recovered_with_one_retrigger():
    """丢一发触发（等帧超时）时补发**一发**，这一帧照常采到，扫描不用整条失败。"""
    import numpy as np

    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    sess.armed = True
    sess.cam.trigger_calls = 1             # 扫描已经发过这一点的触发
    orig = sess.cam.issue_software_trigger

    def issue():
        orig()
        if sess.cam.trigger_calls == 2:    # 补发的那一发才出帧
            sess.cam.frames.append(_Frame(np.full((4, 6), 9, dtype=np.uint16)))

    sess.cam.issue_software_trigger = issue
    cam._frame_wait_s = lambda _sess: 0.02
    path, exposure = cam._grab_frame(0, 0, 0.0, save=False)
    assert sess.cam.trigger_calls == 2, "只许补发一发"
    assert exposure == 8000
    assert sess.cam.frames == [], "补发后拿到的这一帧不该留在队列里"


def test_missing_frame_after_retrigger_fails_with_the_real_reason():
    """补发一发后还是没帧：才判这一点没采到，并说清是补发过之后仍无帧。"""
    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    sess.armed = True
    sess.cam.trigger_calls = 1
    cam._frame_wait_s = lambda _sess: 0.01
    try:
        cam._grab_frame(0, 3, 1.234, save=False)
    except CameraError as exc:
        assert "补发一发触发后仍无帧" in str(exc), exc
        assert "1.2340" in str(exc), "错报要带目标位置"
    else:
        raise AssertionError("补发后仍无帧必须失败")
    assert sess.cam.trigger_calls == 2, "只补一次，不无限重试"


def test_extra_frame_after_retrigger_is_drained():
    """补发前那一发如果只是迟到，会多出一帧：必须排空，否则下一点拿到上一点的帧。"""
    import numpy as np

    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    sess.armed = True
    sess.cam.trigger_calls = 1
    orig = sess.cam.issue_software_trigger

    def issue():
        orig()
        if sess.cam.trigger_calls == 2:
            # 原来那发迟到 + 补发那发：两帧都在队列里
            sess.cam.frames.append(_Frame(np.full((4, 6), 1, dtype=np.uint16)))
            sess.cam.frames.append(_Frame(np.full((4, 6), 2, dtype=np.uint16)))

    sess.cam.issue_software_trigger = issue
    cam._frame_wait_s = lambda _sess: 0.02
    cam._grab_frame(0, 0, 0.0, save=False)
    assert sess.cam.trigger_calls == 2
    assert sess.cam.frames == [], "多出来的那一帧必须被排空"


def test_begin_scan_rolls_back_hold_when_the_session_cannot_be_built():
    """扫描接手建不起会话：_hold 要回到 0，别把"相机归扫描用"永远挂在身上。"""
    restore = _fake_sdk([[]])
    try:
        cam = _cam()
        try:
            cam._run(("begin_scan",))
        except CameraError:
            pass
        else:
            raise AssertionError("没设备时 begin_scan 必须失败")
        assert cam._hold == 0, cam._hold
        assert cam._sess is None
    finally:
        restore()


def test_reopen_and_actions_are_refused_while_scan_holds():
    """扫描持有期间重开/开预览/保存一律拒绝 —— 重开会把单帧会话换成连续流，严格性静默消失。"""
    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    cam._hold = 1
    for job in (("reopen", 0.0), ("preview", True, 0.0), ("save",)):
        try:
            cam._run(job)
        except CameraInputError as exc:
            assert "扫描进行中" in str(exc), (job, exc)
        else:
            raise AssertionError(f"扫描持有期间不许 {job[0]}")
    assert cam._sess is sess, "拒绝的是动作，不是会话"
    assert sess.cam.disposed == 0


def test_mode_mismatch_keeps_the_session():
    """别的动作来要"连续模式"、而当前是扫描的单帧会话：只拒绝，**不许把扫描会话丢掉**。"""
    cam = _cam()
    sess = cam._sess = _session(single_frame=True)
    try:
        cam._ensure_session(0.0)
    except CameraInputError as exc:
        assert "触发模式" in str(exc), exc
    else:
        raise AssertionError("模式不符时必须拒绝")
    assert cam._sess is sess, "模式不符是输入错误，不是会话坏了"


def test_trigger_mode_readback_failure_rejects_the_session():
    """触发模式写不进去就别拿这个会话去扫描 —— 严格性只靠这一个设置。"""
    restore = _fake_sdk([["34331"]])
    try:
        _Cam.ignore_trigger_mode_write = True
        cam = _cam()
        try:
            cam._ensure_session(0.0, single_frame=True)
        except CameraError as exc:
            assert "触发模式没设上" in str(exc), exc
        else:
            raise AssertionError("读回不一致时必须拒绝")
        assert cam._sess is None
        assert _SDK.made and _SDK.made[0].disposed == 1, "建到一半失败要把 SDK 一起收掉"
    finally:
        _Cam.ignore_trigger_mode_write = False
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
