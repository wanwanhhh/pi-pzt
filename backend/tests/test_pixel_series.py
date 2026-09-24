"""扫描点像素序列：离线自检，**不碰相机、不碰硬件**。

数据处理页那条曲线的取数口子（GET /api/scans/{id}/pixel）就是这里的 png_pixel_series：
从每个扫描点**已经落盘的 PNG** 里取同一个像素。三条规矩要守住：

  1. 位置用调用方给的**读出位置**，本函数只读像素；
  2. 没图 / 图读不动的点给 None —— **不补值、不插值**（曲线在那里断开）；
  3. 点落在画面外是**坐标填错了**，当场报错，不能返回一串 None 让人以为"这个像素没光"。

直接跑：python backend/tests/test_pixel_series.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.thorlabs_ccd import _save_png16, png_pixel_series  # noqa: E402


def _frames(tmp: Path, values):
    """造几张 5×7 的 16 位帧，每张在 (2, 1) 处写一个值；返回路径表。"""
    out = []
    for i, v in enumerate(values):
        img = np.zeros((5, 7), dtype=np.uint16)
        img[1, 2] = v
        p = tmp / f"scan0001_{i:05d}.png"
        _save_png16(p, img)
        out.append(p)
    return out


def test_series_reads_the_same_pixel_from_every_frame():
    """每个扫描点各取同一个像素；几何（宽高/位深/满量程）跟着文件如实报。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = _frames(tmp, [10, 20, 30])
        items = [(i, 5.0 + i, p) for i, p in enumerate(paths)]
        got = png_pixel_series(items, 2, 1)

    assert got["value"] == [10, 20, 30], got["value"]
    assert got["idx"] == [0, 1, 2] and got["position_um"] == [5.0, 6.0, 7.0], got
    assert (got["x"], got["y"]) == (2, 1)
    assert (got["width"], got["height"], got["bits"], got["full_scale"]) == (7, 5, 16, 1022), got
    assert got["missing"] == 0


def test_position_is_passed_through_untouched():
    """位置是**扫描记下来的读出位置**，本函数一个数都不许改、也不许拿序号顶替。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        paths = _frames(Path(d), [1, 2])
        got = png_pixel_series([(0, None, paths[0]), (1, 12.5, paths[1])], 2, 1)
    assert got["position_um"] == [None, 12.5], got["position_um"]   # 没记下读数的点照实是 None


def test_points_without_a_frame_stay_none():
    """没采到图的点：值就是 None，**不许拿邻点的值顶上**（曲线在那里断开）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        paths = _frames(Path(d), [10, 20])
        got = png_pixel_series(
            [(0, 1.0, paths[0]), (1, 2.0, None), (2, 3.0, paths[1])], 2, 1
        )
    assert got["value"] == [10, None, 20], got["value"]
    assert got["missing"] == 1


def test_unreadable_file_counts_as_missing():
    """图被手删了 / 不是图片：这一点没有值，也不该让整条曲线 500。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = _frames(tmp, [10, 20])
        bad = tmp / "坏了.png"
        bad.write_bytes(b"not a png")
        got = png_pixel_series(
            [(0, 1.0, paths[0]), (1, 2.0, tmp / "没有这个文件.png"), (2, 3.0, bad),
             (3, 4.0, paths[1])],
            2, 1,
        )
    assert got["value"] == [10, None, None, 20], got["value"]
    assert got["missing"] == 2
    # 几何仍以第一张读得动的图为准 —— 后面几张读不动不影响前面的结论
    assert (got["width"], got["height"]) == (7, 5), got


def test_point_outside_the_frame_is_refused():
    """点在图外 = 坐标填错了：当场报错，不能返回一串 None。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        paths = _frames(Path(d), [10])
        for bad in ((7, 1), (2, 5), (999, 999)):
            try:
                png_pixel_series([(0, 1.0, paths[0])], *bad)
            except ValueError as exc:
                assert "超出画面" in str(exc), exc
                continue
            raise AssertionError(f"{bad} 越界应该被拒绝")


def test_eight_bit_frames_are_read_too():
    """占位相机（假相机）存的是 8 位灰度图：照读，位深与满量程跟着文件变（255 而不是 1022）。"""
    import tempfile

    from PIL import Image

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ps = []
        for i, v in enumerate((7, 200)):
            p = tmp / f"dummy{i}.png"
            Image.fromarray(np.full((4, 6), v, dtype=np.uint8), mode="L").save(p)
            ps.append(p)
        got = png_pixel_series([(i, float(i), p) for i, p in enumerate(ps)], 3, 2)
    assert got["value"] == [7, 200], got["value"]
    assert (got["bits"], got["full_scale"]) == (8, 255), got


def test_scan_without_any_frame_says_so():
    """整条扫描都没有图（CCD 后端是 null，或者被删光了）：如实返回"没有几何、全是 None" ——
    界面据此说"这条扫描没有图"，而不是画一条空坐标系让人以为像素是黑的。"""
    got = png_pixel_series([(0, 1.0, None), (1, 2.0, None)], 2, 1)
    assert got["value"] == [None, None] and got["missing"] == 2, got
    assert got["width"] is None and got["bits"] is None, got


def test_many_frames_use_the_same_path_as_few():
    """铺开读（线程池）与串行读的结果必须一模一样 —— 顺序、缺值位置都不能错。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        vals = [i * 3 % 1000 for i in range(24)]
        paths = _frames(Path(d), vals)
        items = [(i, float(i), paths[i]) for i in range(len(vals))]
        one = png_pixel_series(items, 2, 1, workers=1)
        many = png_pixel_series(items, 2, 1, workers=8)
    assert one["value"] == vals, one["value"]
    assert many == one, (many["value"], one["value"])


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
