"""预览页「80% 能量圆」的离线自检：原始数据、不扣背景、整幅质心、窗口、80% 累积。

不碰硬件、不碰数据库。直接跑：python backend/tests/test_energy_circle.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.thorlabs_ccd import (
    CameraError,
    ThorlabsCamera,
    energy_circle,
    global_centroid,
)


def _gauss(h=240, w=240, cx=120.0, cy=120.0, wx=10.0, amp=100.0, bg=0.0):
    yy, xx = np.mgrid[0:h, 0:w]
    img = bg + amp * np.exp(-2.0 * (((xx - cx) / wx) ** 2 + ((yy - cy) / wx) ** 2))
    return np.clip(np.rint(img), 0, 65535).astype(np.uint16)


def _measure(img):
    return energy_circle(img, global_centroid(img))


def test_80_percent_radius_of_a_gaussian_is_close_to_the_analytic_value():
    """高斯下 80% 能量半径 = sqrt(-0.5 ln 0.2)·w ≈ 0.8967w；原始帧不扣背景。"""
    img = _gauss(wx=10.0, amp=100.0, bg=0.0)
    r = _measure(img)
    assert r["ok"] is True, r
    want = 0.8967 * 10.0
    assert abs(r["r80"] - want) < 0.15, (r["r80"], want)
    assert abs(r["d80"] - 2.0 * r["r80"]) < 1e-12


def test_total_is_the_raw_window_sum_not_background_subtracted():
    """窗口内总能量 = 原始像素和；没有扣背景这一步。"""
    img = _gauss(wx=20.0, amp=1000.0, bg=2.0)
    r = _measure(img)
    assert r["ok"] is True, r
    x0, y0, ww, hh = r["window"]
    assert r["total"] == int(img[y0:y0 + hh, x0:x0 + ww].astype(np.int64).sum()), r["total"]


def test_weak_background_still_gives_a_radius_but_is_slightly_biased():
    """背景弱时仍能出数；这里只钉它不报错、量级对，不承诺和扣背景一样准。"""
    img = _gauss(wx=20.0, amp=1000.0, bg=2.0)
    r = _measure(img)
    assert r["ok"] is True, r
    assert 0.85 * 20.0 < r["r80"] < 1.10 * 20.0, r["r80"]


def test_strong_background_is_refused_with_the_background_reason():
    """10% 峰值线落到背景里：80% 会圈住背景，必须拒绝，不能返回一个假直径。"""
    img = _gauss(wx=10.0, amp=100.0, bg=100.0)
    r = _measure(img)
    assert r["ok"] is False, r
    assert "背景" in r["reason"], r


def test_background_area_share_is_refused_even_if_each_pixel_looks_small():
    """每像素 2 ADU 看着很弱，但窗口面积乘上去能占小光斑总能量的三成 —— 这种也要拒。"""
    img = _gauss(wx=6.0, amp=300.0, bg=2.0)
    r = _measure(img)
    assert r["ok"] is False, r
    assert "背景占" in r["reason"], r


def test_moderate_background_is_drawn_but_marked_as_a_warning():
    """背景占 5%~20%：照画，但把占比报给界面标『仅供参考』；不假装是干净背景。"""
    img = _gauss(wx=20.0, amp=1000.0, bg=4.5)
    r = _measure(img)
    assert r["ok"] is True, r
    assert 0.05 < r["bg_fraction"] < 0.20, r
    assert r["bg_warning"] is True, r


def test_spot_near_the_frame_edge_is_refused():
    img = _gauss(cx=20.0, cy=20.0, wx=10.0, amp=100.0, bg=0.0)
    r = _measure(img)
    assert r["ok"] is False, r
    assert "贴边" in r["reason"], r


def test_zero_frame_has_no_energy_circle():
    r = _measure(np.zeros((240, 240), dtype=np.uint16))
    assert r["ok"] is False, r
    assert "没有信号" in r["reason"], r


def test_missing_centroid_is_refused():
    r = energy_circle(np.ones((240, 240), dtype=np.uint16), None)
    assert r["ok"] is False and "质心" in r["reason"], r


def test_saturation_is_reported_but_still_measured():
    """饱和会削顶、总能量少一块，所以只报计数给界面标“仅供参考”，不冒充没饱和。"""
    img = np.minimum(_gauss(wx=10.0, amp=2000.0, bg=0.0), 1022).astype(np.uint16)
    r = _measure(img)
    assert r["ok"] is True, r
    assert r["saturated"] > 0, r
    assert r["r80"] > 0


def test_input_frame_is_not_mutated():
    img = _gauss(wx=10.0, amp=100.0, bg=0.0)
    before = img.copy()
    _measure(img)
    assert np.array_equal(img, before)


def test_camera_method_needs_a_preview_frame():
    cam = ThorlabsCamera()
    try:
        cam.measure_energy_circle()
    except CameraError as exc:
        assert "先开预览" in str(exc), exc
    else:
        raise AssertionError("没有帧时应该明确报错")


def test_camera_method_uses_the_live_frame_and_stored_centroid():
    img = _gauss(wx=10.0, amp=100.0, bg=0.0)
    cam = ThorlabsCamera()
    cam._sess = types.SimpleNamespace(live=img, centroid=global_centroid(img))
    r = cam.measure_energy_circle()
    assert r["ok"] is True, r
    assert abs(r["cx"] - 120.0) < 1e-9 and abs(r["cy"] - 120.0) < 1e-9, r


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
