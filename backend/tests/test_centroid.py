"""全局质心的离线自检：定义、边界、与暴力法一致、饱和计数。

**这个质心按用户定的口径：整幅图的强度加权重心，不扣背景、不设阈值、不开窗。**
不碰硬件、不碰数据库。直接跑：python backend/tests/test_centroid.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import TL_SATURATION_ADU
from backend.thorlabs_ccd import global_centroid


def test_centroid_of_a_symmetric_image_is_the_centre():
    img = np.ones((9, 11), dtype=np.uint16) * 100
    c = global_centroid(img)
    assert abs(c["cx"] - 5.0) < 1e-9, c["cx"]        # (0..10) 的均值 = 5
    assert abs(c["cy"] - 4.0) < 1e-9, c["cy"]        # (0..8) 的均值 = 4


def test_centroid_of_a_single_pixel_is_that_pixel():
    img = np.zeros((7, 7), dtype=np.uint16)
    img[2, 5] = 1000
    c = global_centroid(img)
    assert (c["cx"], c["cy"]) == (5.0, 2.0), c


def test_weights_are_intensity_not_area():
    """两团亮度 3:1 的图像，重心按强度加权 —— 不是两团的几何中点。"""
    img = np.zeros((1, 11), dtype=np.uint16)
    img[0, 1] = 300
    img[0, 9] = 100
    c = global_centroid(img)
    assert abs(c["cx"] - 3.0) < 1e-9, c["cx"]        # (1*300 + 9*100) / 400


def test_matches_the_brute_force_definition():
    rng = np.random.default_rng(3)
    img = rng.integers(0, TL_SATURATION_ADU + 1, size=(64, 96)).astype(np.uint16)
    c = global_centroid(img)
    coarse = img.astype(np.float64)
    yy, xx = np.mgrid[0:64, 0:96]
    assert abs(c["cx"] - (coarse * xx).sum() / coarse.sum()) < 1e-6
    assert abs(c["cy"] - (coarse * yy).sum() / coarse.sum()) < 1e-6
    assert c["sum"] == int(img.sum()), (c["sum"], int(img.sum()))
    assert c["peak"] == int(img.max())


def test_zero_image_has_no_centroid():
    """全零（只可能出现在坏帧/遮光）时不能返回 0/0，要说"没有"。"""
    c = global_centroid(np.zeros((4, 4), dtype=np.uint16))
    assert c["cx"] is None and c["cy"] is None and c["sum"] == 0, c


def test_saturated_count_uses_measured_full_scale():
    img = np.full((5, 5), TL_SATURATION_ADU - 1, dtype=np.uint16)
    img[0, 0] = TL_SATURATION_ADU
    img[0, 1] = 65535
    c = global_centroid(img)
    assert c["saturated"] == 2, c
    assert c["peak"] == 65535, c


def test_shape_is_reported_for_scaling():
    """前端拿 width/height 把像素坐标换成百分比 —— 这两个数必须在。"""
    c = global_centroid(np.ones((1080, 1440), dtype=np.uint16))
    assert (c["width"], c["height"]) == (1440, 1080), c


def test_rotation_moves_the_centroid_and_swaps_shape():
    """画面转过去之后，质心的"归一化位置"必须仍在画面内 —— 前端把十字线画到显示帧上就靠这个。

    注意后端**不是**这么算的（它先算质心、再转显示，读数保持传感器坐标）；这里只是把
    "转过之后的坐标系长什么样"钉住，用来对照前端的换算表。"""
    img = np.zeros((40, 60), dtype=np.uint16)      # 高 40、宽 60
    img[10, 30] = 1000                             # 质心正好在 (30, 10)
    for k in range(4):
        c = global_centroid(np.rot90(img, k))
        if k % 2:
            assert (c["width"], c["height"]) == (40, 60), (k, c["width"], c["height"])
        else:
            assert (c["width"], c["height"]) == (60, 40), (k, c["width"], c["height"])
        # 归一化位置必须落在画面内（十字线用百分比摆，越界就是这里错了）
        assert 0.0 <= c["cx"] / c["width"] <= 1.0, (k, c["cx"], c["width"])
        assert 0.0 <= c["cy"] / c["height"] <= 1.0, (k, c["cy"], c["height"])
    # 180° 是点对称，但**像素下标**的对称是 x + x' = W-1（不是 W）：
    # 40x60 里第 30 列转 180° 到第 29 列，拿归一化坐标相加会差 1/W —— 差的就是这一个像素。
    c0, c2 = global_centroid(img), global_centroid(np.rot90(img, 2))
    assert c0["cx"] + c2["cx"] == 59 and c0["cy"] + c2["cy"] == 39, (c0, c2)


def test_clockwise_rotation_maps_the_centroid_this_way():
    """预览朝向按**顺时针**转（界面上一次 90°），质心读数保持传感器坐标不动。

    前端要按这张表把十字线画到显示帧上，所以把映射钉在这里：
      90°： (x', y') = (H-1-y, x)，显示尺寸 (H, W)
      180°：(x', y') = (W-1-x, H-1-y)
      270°：(x', y') = (y, W-1-x)，显示尺寸 (H, W)
    验法是"两条路必须等价"：把图转过去再算质心 == 把质心按公式搬过去。
    """
    img = np.zeros((40, 60), dtype=np.uint16)
    img[10, 30] = 1000                                  # 传感器坐标 (30, 10)，W=60 H=40
    c = global_centroid(img)
    assert (c["width"], c["height"]) == (60, 40)

    def mapped(k):
        x, y, W, H = c["cx"], c["cy"], c["width"], c["height"]
        return {0: (x, y), 90: (H - 1 - y, x), 180: (W - 1 - x, H - 1 - y),
                270: (y, W - 1 - x)}[k]

    for deg, k in ((0, 0), (90, 1), (180, 2), (270, 3)):
        got = global_centroid(np.rot90(img, -k))         # 后端显示走的就是 -k（顺时针）
        want = mapped(deg)
        assert (got["cx"], got["cy"]) == want, (deg, (got["cx"], got["cy"]), want)
        if deg % 180:
            assert (got["width"], got["height"]) == (40, 60), (deg, got["width"], got["height"])


def test_rotation_only_accepts_quarter_turns():
    from backend.thorlabs_ccd import CameraError, _norm_rotation
    assert [_norm_rotation(d) for d in (0, 90, 180, 270, 360, -90)] == [0, 90, 180, 270, 0, 270]
    for bad in (45, 1, 91):
        try:
            _norm_rotation(bad)
        except CameraError:
            continue
        raise AssertionError(f"{bad} 应该被拒绝")


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
