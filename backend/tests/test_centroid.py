"""预览质心与轮廓图（剖面）的离线自检：定义、边界、与暴力法一致、饱和计数、切片。

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


def test_saved_png_carries_centroid_and_exposure():
    """存盘时把**曝光与质心**写进文件自己身上（PNG 的 tEXt 块），读回来必须对得上 ——
    图库那一行就是从这里来的，不查库。"""
    import tempfile
    from pathlib import Path

    from backend.thorlabs_ccd import _save_png16, read_png_meta

    img = np.zeros((20, 30), dtype=np.uint16)
    img[5, 7] = 900
    cen = global_centroid(img)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.png"
        _save_png16(p, img, 12345, cen)
        meta = read_png_meta(p)
    assert meta["exposure_us"] == 12345, meta
    assert meta["centroid"] == [7.0, 5.0], meta


def test_png_without_text_blocks_says_none():
    """老图（加 tEXt 之前存的）没有这些块 → 界面显示"未记录"。
    **不许猜一个值出来**：猜出来的质心比没有更糟。"""
    import tempfile
    from pathlib import Path

    from backend.thorlabs_ccd import _save_png16, read_png_meta

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "old.png"
        _save_png16(p, np.ones((4, 4), dtype=np.uint16))
        meta = read_png_meta(p)
    assert meta == {"exposure_us": None, "centroid": None}, meta


def test_thumb_mapping_depends_on_bit_depth():
    """缩略图降到 8 位的口径按位深决定：16 位图右移 2 位（满量程 1022），8 位图原样。

    图库的原始帧是 16 位，扫描占位图（假相机）是 8 位 —— 一刀切右移会把后者压暗 4 倍。
    """
    import io as _io
    import tempfile
    from pathlib import Path

    from PIL import Image

    from backend.config import IMAGE_DIR
    from backend.thorlabs_ccd import _save_png16, thumb_jpeg

    caches = []
    try:
        with tempfile.TemporaryDirectory() as d:
            p8 = Path(d) / "eight.png"
            Image.fromarray(np.full((8, 8), 200, dtype=np.uint8), mode="L").save(p8)
            p16 = Path(d) / "sixteen.png"
            _save_png16(p16, np.full((8, 8), 800, dtype=np.uint16))   # 800 >> 2 == 200
            # 缓存文件名里有 mtime：**趁文件还在**把路径记下来，出了临时目录就 stat 不到了
            caches = [IMAGE_DIR.parent / "thumbs" / f"{p.stem}_{int(p.stat().st_mtime)}_260.jpg"
                      for p in (p8, p16)]
            m8 = float(np.asarray(Image.open(_io.BytesIO(thumb_jpeg(p8)))).mean())
            m16 = float(np.asarray(Image.open(_io.BytesIO(thumb_jpeg(p16)))).mean())
        assert abs(m8 - 200) < 3, m8       # 8 位原样（JPEG 允许几个数的偏差）
        assert abs(m16 - 200) < 3, m16     # 16 位的 800 → 200
    finally:
        for cache in caches:               # 缩略图缓存落在 data/thumbs/，测试用完自己清掉
            cache.unlink(missing_ok=True)


def test_thumb_cache_key_covers_size_and_mtime():
    """缓存键少了尺寸或 mtime 都是**静默**错误：前者让大图小图互相顶掉，后者让重存后还看旧图。"""
    import os
    import tempfile
    from pathlib import Path

    from backend.thorlabs_ccd import _thumb_cache_name

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.png"
        p.write_bytes(b"x")
        keys = {_thumb_cache_name(p, s) for s in (260, 520, 1440)}
        assert len(keys) == 3, keys                       # 尺寸必须进键
        before = _thumb_cache_name(p, 260)
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        assert _thumb_cache_name(p, 260) != before, "mtime 变了键要跟着变"


def test_profile_cuts_the_whole_row_and_column():
    """轮廓图 = 过点的**整行**与**整列**，原值返回（16 位、不处理）。
    朝向为 0 时就是传感器的行/列 —— 转置了的话这条会红。"""
    from backend.thorlabs_ccd import ThorlabsCamera

    cam = ThorlabsCamera()
    cam._rotation = 0
    f = np.zeros((4, 6), dtype=np.uint16)
    f[:, 3] = [1, 2, 3, 4]                    # 第 3 列
    f[2, :] = [10, 20, 30, 40, 50, 60]        # 第 2 行；交叉点 (3,2) 最后写，所以两边都该是 40
    cam._live = f
    p = cam.profile(3, 2)
    assert p["horizontal"] == [10, 20, 30, 40, 50, 60], p["horizontal"]
    assert p["vertical"] == [1, 2, 40, 4], p["vertical"]      # 交叉点两条剖面里必须是同一个数
    assert (p["width"], p["height"], p["bits"], p["full_scale"]) == (6, 4, 16, 1022), p
    assert p["rotation"] == 0


def test_profile_follows_rotation():
    """转了 90° 之后"水平"要指**看到的**水平：显示水平 = 传感器的某一列（自下往上），
    显示垂直 = 传感器的某一行（自左往右）。"""
    from backend.thorlabs_ccd import ThorlabsCamera

    cam = ThorlabsCamera()
    f = np.arange(4 * 6, dtype=np.uint16).reshape(4, 6)      # frame[y, x] = y*6 + x
    cam._live = f
    cam._rotation = 90
    p = cam.profile(0, 0)                                    # 显示坐标系左上角
    assert (p["width"], p["height"]) == (4, 6), (p["width"], p["height"])   # 宽高互换
    # 顺时针 90°：显示 (x', y') = (H-1-y, x)。显示第 0 行来自传感器第 0 列，自下往上
    assert p["horizontal"] == [int(f[3, 0]), int(f[2, 0]), int(f[1, 0]), int(f[0, 0])], p["horizontal"]
    assert p["vertical"] == [int(v) for v in f[3, :]], p["vertical"]


def test_profile_refuses_bad_points_and_no_frame():
    from backend.thorlabs_ccd import CameraError, ThorlabsCamera

    cam = ThorlabsCamera()
    try:
        cam.profile(0, 0)
    except CameraError as exc:
        assert "先开预览" in str(exc), exc
    else:
        raise AssertionError("没有帧时应该明确报错，而不是返回空剖面")
    cam._live = np.zeros((4, 6), dtype=np.uint16)
    for bad in ((6, 0), (0, 4), (99, 99)):
        try:
            cam.profile(*bad)
        except CameraError:
            continue
        raise AssertionError(f"{bad} 越界应该被拒")


def test_png_profile_reads_the_saved_file():
    """存下来的 PNG 也能切：坐标就是文件坐标（文件是传感器朝向，从不旋转）。"""
    import tempfile
    from pathlib import Path

    from backend.thorlabs_ccd import _save_png16, png_profile

    f = np.zeros((5, 7), dtype=np.uint16)
    f[:, 2] = [11, 12, 13, 14, 15]
    f[1, :] = [1, 2, 3, 4, 5, 6, 7]        # 交叉点最后写：水平里是 3，垂直里也是 3
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.png"
        _save_png16(p, f, 1000)
        got = png_profile(p, 2, 1)
    assert got["horizontal"] == [1, 2, 3, 4, 5, 6, 7], got["horizontal"]
    assert got["vertical"] == [11, 3, 13, 14, 15], got["vertical"]
    assert (got["width"], got["height"], got["bits"], got["full_scale"]) == (7, 5, 16, 1022), got


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
