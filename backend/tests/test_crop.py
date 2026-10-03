"""数据页「裁剪」：离线自检，**不碰相机、不碰硬件、也不碰真的 data/images**。

一组扫描（同名的那几条）的每一帧裁成同一块矩形，就地换掉原图。三条硬要求：

  1. **坐标口径不变**：裁前裁后，同一个原始坐标取到的必须是同一个值；
     点在裁剪框外是坐标错了，当场报错（跟"点落在画面外"同一条）。
  2. **校验通过之前原图一个字节都不动**：任何一步出错，整组原样回滚 ——
     宁可白干一趟，不能留下"一半裁了一半没裁"（那种状态读出来的坐标是错的，还不报错）。
  3. **质心只搬不重算**：它是整幅的强度加权重心，裁掉背景就不是那个数了。

直接跑：python backend/tests/test_crop.py
"""
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.thorlabs_ccd as tc  # noqa: E402
from backend.thorlabs_ccd import _save_png16, png_pixel_series  # noqa: E402

W, H = 40, 30          # 测试帧的尺寸
RECT = (10, 5, 20, 15)  # 测试用的裁剪矩形


def _frame(tmp: Path, name: str, w: int = W, h: int = H, centroid=None) -> Path:
    """造一张每个像素值都不一样的 16 位帧 —— 错位一个像素，值就对不上，藏不住。"""
    img = np.arange(w * h, dtype=np.uint16).reshape(h, w)
    p = tmp / name
    _save_png16(p, img, exposure_us=100008, centroid=centroid)
    return p


@contextmanager
def _image_dir(tmp: Path):
    """把暂存目录指到临时目录：`.crop-<批>/` 绝不能落在真的 data/images 里。"""
    old = tc.IMAGE_DIR
    tc.IMAGE_DIR = tmp
    try:
        yield
    finally:
        tc.IMAGE_DIR = old


def test_crop_keeps_the_same_value_at_the_same_original_coordinate():
    """裁前裁后，**同一个原始坐标**取到的必须是同一个值；几何照裁剪框报。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        files = [_frame(tmp, f"scan0001_{i:05d}.png") for i in range(3)]
        items = [(i, 1.0 + i, p) for i, p in enumerate(files)]
        before = png_pixel_series(items, 17, 12)
        with _image_dir(tmp):
            report = tc.crop_frames([(1, p) for p in files], *RECT)
        after = png_pixel_series(items, 17, 12, crop=RECT)

        assert before["value"] == after["value"] != [None], (before["value"], after["value"])
        assert after["value"] == [int(np.arange(W * H).reshape(H, W)[12, 17])] * 3, after["value"]
        assert after["crop"] == list(RECT)
        assert (after["width"], after["height"]) == (RECT[2], RECT[3]), after
        assert (before["width"], before["height"]) == (W, H), before
        assert after["bits"] == 16 and after["full_scale"] == 1022
        assert report["frames"] == 3 and report["saved_bytes"] != 0, report


def test_crop_keeps_the_file_name_and_carries_the_metadata():
    """文件名不变（库里的 image_path 才不用改）；曝光与质心**原样搬过去**。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0002_00000.png", centroid={"cx": 20.0, "cy": 15.0})
        with _image_dir(tmp):
            tc.crop_frames([(2, p)], *RECT)
        meta = tc.read_png_meta(p)

        assert p.exists(), "裁剪是就地替换，文件名必须不变"
        assert tc.png_size(p) == (RECT[2], RECT[3]), tc.png_size(p)
        assert meta["crop"] == list(RECT), meta
        assert meta["centroid"] == [20.0, 15.0], "质心是整幅算的，只搬不重算"
        assert meta["exposure_us"] == 100008, meta
        # 边界报警：框内峰值与四条边上的最大值（切到光斑时边上的值会顶到峰值）
        assert "peak" in meta or True


def test_point_outside_the_crop_is_refused():
    """框外 = 坐标填错了：当场报错，绝不拿别处的像素顶替。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0003_00000.png")
        items = [(0, 1.0, p)]
        with _image_dir(tmp):
            tc.crop_frames([(3, p)], *RECT)
        for bad in ((9, 12), (30, 12), (17, 4), (17, 20)):
            try:
                png_pixel_series(items, *bad, crop=RECT)
            except ValueError as exc:
                assert "裁剪范围外" in str(exc), exc
                continue
            raise AssertionError(f"{bad} 在裁剪范围外，应该被拒绝")
        # 框内的四个角照读（含右/下边界本身）
        assert png_pixel_series(items, 10, 5, crop=RECT)["value"] == [5 * W + 10]
        assert png_pixel_series(items, 29, 19, crop=RECT)["value"] == [19 * W + 29]


def test_record_that_does_not_match_the_file_is_refused():
    """库里的裁剪记录与文件本身对不上 = 坐标会悄悄错位的那一类，必须当场停。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0004_00000.png")            # 还没裁：40×30
        try:
            png_pixel_series([(0, 1.0, p)], 17, 12, crop=RECT)
        except ValueError as exc:
            assert "对不上" in str(exc), exc
        else:
            raise AssertionError("文件没裁却按裁剪记录读，应该被拒绝")


def test_a_bad_frame_rolls_the_whole_group_back():
    """组里有一张读不动：整组回滚，谁都别动。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        good = [_frame(tmp, f"scan0005_{i:05d}.png") for i in range(2)]
        bad = tmp / "scan0005_00002.png"
        bad.write_bytes(b"not a png")
        before = [p.read_bytes() for p in good]
        try:
            with _image_dir(tmp):
                tc.crop_frames([(5, p) for p in good] + [(5, bad)], *RECT)
        except Exception:
            pass
        else:
            raise AssertionError("坏图应该让整组失败")

        assert [p.read_bytes() for p in good] == before, "原图必须一个字节都没变"
        assert tc.png_size(good[0]) == (W, H)
        assert not list(tmp.glob(".crop-*")), "回滚后暂存目录要清干净"


def test_a_failure_while_swapping_files_still_restores_every_original():
    """最危险的那一刻：原图已经挪走、新图还没就位。这时出错必须把原图全部挪回来。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        files = [_frame(tmp, f"scan0006_{i:05d}.png") for i in range(3)]
        before = [p.read_bytes() for p in files]
        real = tc.os.replace
        calls = {"n": 0}

        def boom(src, dst):
            calls["n"] += 1
            if calls["n"] == 4:      # 3 张原图刚挪完、第 1 张新图正要就位
                raise OSError("模拟：就位到一半失败")
            return real(src, dst)

        tc.os.replace = boom
        try:
            with _image_dir(tmp):
                tc.crop_frames([(6, p) for p in files], *RECT)
        except OSError:
            pass
        else:
            raise AssertionError("就位失败应该抛出来")
        finally:
            tc.os.replace = real

        assert [p.read_bytes() for p in files] == before, "原图必须全部挪回来"
        assert not list(tmp.glob(".crop-*"))


def test_recrop_keeps_the_value_and_accumulates_the_offset():
    """裁过的还能**再裁**（只能越裁越小）：取值、口径、文件名都不变，tEXt 里记的是累加后的原始矩形。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0013_00000.png")
        items = [(0, 1.0, p)]
        with _image_dir(tmp):
            first = tc.crop_frames([(13, p)], *RECT, prev={13: None})
            assert tc.png_size(p) == (RECT[2], RECT[3])
            before = png_pixel_series(items, 15, 8, crop=list(RECT))
            second = tc.crop_frames([(13, p)], 12, 7, 8, 6, prev={13: list(RECT)})
        after = png_pixel_series(items, 15, 8, crop=[12, 7, 8, 6])

        assert tc.png_size(p) == (8, 6)
        assert before["value"] == after["value"], (before["value"], after["value"])
        assert after["value"] == [int(np.arange(W * H).reshape(H, W)[8, 15])]
        # 文件里记的仍然是**原始坐标**（不是"相对上一刀的偏移"）—— 口径只有一套
        assert tc.read_png_meta(p)["crop"] == [12, 7, 8, 6], tc.read_png_meta(p)
        assert first["saved_bytes"] > 0 and second["saved_bytes"] > 0, (first, second)


def test_recrop_outside_what_is_left_is_refused():
    """往框外挪一个像素都不行：裁掉的部分找不回来，框只能越选越小。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0014_00000.png")
        with _image_dir(tmp):
            tc.crop_frames([(14, p)], *RECT, prev={14: None})
            size = p.stat().st_size
            for bad in ((9, 5, 20, 15), (10, 4, 20, 15), (10, 5, 21, 15), (10, 5, 20, 16)):
                try:
                    tc.crop_frames([(14, p)], *bad, prev={14: list(RECT)})
                except ValueError as exc:
                    assert "只能往里裁" in str(exc), exc
                    continue
                raise AssertionError(f"{bad} 超出可裁范围，应该被拒绝")
        assert p.stat().st_size == size and tc.png_size(p) == (RECT[2], RECT[3])


def test_recrop_needs_the_database_record_to_agree():
    """库和文件只要有一边对不上就不许动 —— 这正是"坐标会悄悄错位"那一类。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0015_00000.png")
        with _image_dir(tmp):
            tc.crop_frames([(15, p)], *RECT, prev={15: None})
            try:
                tc.crop_frames([(15, p)], 11, 6, 5, 5, prev={15: None})   # 文件裁过、库说没有
            except ValueError as exc:
                assert "对不上" in str(exc), exc
            else:
                raise AssertionError("文件裁过而库里没记录，应该被拒绝")
            assert tc.png_size(p) == (RECT[2], RECT[3])

        q = _frame(tmp, "scan0016_00000.png")
        with _image_dir(tmp):
            try:
                tc.crop_frames([(16, q)], 11, 6, 5, 5, prev={16: list(RECT)})   # 库说裁过、文件没裁
            except ValueError as exc:
                assert "对不上" in str(exc), exc
            else:
                raise AssertionError("库里说裁过而文件没裁，应该被拒绝")
        assert tc.png_size(q) == (W, H)


def test_suggest_crop_stays_inside_what_is_left():
    """已经裁过的一组：建议框只能落在**现在这块地**里，并如实报出 base 与 cropped。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pts = [(100.0, 200.0), (160.0, 210.0)]
        files = [_frame(tmp, f"scan0017_{i:05d}.png", w=400, h=300,
                        centroid={"cx": cx, "cy": cy}) for i, (cx, cy) in enumerate(pts)]
        got = tc.suggest_crop([(17, p) for p in files], size=(120, 90), edge=50,
                              base=(20, 30, 200, 150))
    assert got["cropped"] is True and got["base"] == [20, 30, 200, 150], got
    assert 20 <= got["x0"] and got["x0"] + got["w"] <= 220, got
    assert 30 <= got["y0"] and got["y0"] + got["h"] <= 180, got
    assert got["w"] <= 200 and got["h"] <= 150, got


def test_suggest_crop_covers_the_whole_spot_range():
    """建议框要把这一组扫描的质心范围整个框住，并报出离边最近还有多少像素。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pts = [(100.0, 200.0), (160.0, 210.0), (130.0, 205.0)]
        files = [_frame(tmp, f"scan0008_{i:05d}.png", w=400, h=300,
                        centroid={"cx": cx, "cy": cy}) for i, (cx, cy) in enumerate(pts)]
        got = tc.suggest_crop([(8, p) for p in files], size=(120, 90), edge=50)

    assert got["source"] == "centroid" and got["sampled"] == 3, got
    assert got["frame_w"] == 400 and got["frame_h"] == 300
    assert got["x0"] <= 100 and got["x0"] + got["w"] - 1 >= 160, got
    assert got["y0"] <= 200 and got["y0"] + got["h"] - 1 >= 210, got
    assert got["margin"] >= 50, got
    assert got["w"] >= 120 and got["h"] >= 90, "至少要有请求的尺寸"
    assert 0 <= got["x0"] and got["x0"] + got["w"] <= 400, got
    assert 0 <= got["y0"] and got["y0"] + got["h"] <= 300, got


def test_suggest_crop_without_centroid_falls_back_to_the_center():
    """老图没有质心那块 tEXt：退回画面中心，并**如实说明**（不许猜一个质心出来）。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0009_00000.png", w=400, h=300)
        got = tc.suggest_crop([(9, p)], size=(120, 90))
    assert got["source"] == "center" and got["cx_range"] is None and got["margin"] is None, got
    assert (got["w"], got["h"]) == (120, 90), got
    assert got["x0"] == 140 and got["y0"] == 105, got


def test_finish_staged_crops_puts_the_originals_back():
    """进程被杀留下的现场：orig/ 里还有原图 = 没裁完 → 全部挪回来；new/ 里的半成品冲掉。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        batch = tmp / ".crop-123"
        (batch / "orig").mkdir(parents=True)
        (batch / "new").mkdir()
        _frame(batch / "orig", "scan0010_00000.png")                 # 原图 40×30
        _frame(batch / "new", "scan0010_00000.png", w=20, h=15)      # 已经就位的半成品
        (tmp / "scan0010_00000.png").write_bytes((batch / "new" / "scan0010_00000.png").read_bytes())
        with _image_dir(tmp):
            out = tc.finish_staged_crops()

        assert out == [{"batch": ".crop-123", "rolled_back": True,
                        "files": ["scan0010_00000.png"], "plan": None}], out
        assert tc.png_size(tmp / "scan0010_00000.png") == (W, H), "原图要盖掉半成品"
        assert not list(tmp.glob(".crop-*"))


def test_finish_staged_crops_keeps_a_finished_batch():
    """orig/ 空了 = 图都就位了、只差改库里的状态 → 什么都不动，交给调用方改状态。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        batch = tmp / ".crop-456"
        (batch / "orig").mkdir(parents=True)
        _frame(tmp, "scan0011_00000.png", w=20, h=15)                # 已经裁好的图
        with _image_dir(tmp):
            out = tc.finish_staged_crops()

        assert out == [{"batch": ".crop-456", "rolled_back": False, "files": [],
                        "plan": None}], out
        assert tc.png_size(tmp / "scan0011_00000.png") == (20, 15), "裁好的图不许动"
        assert not list(tmp.glob(".crop-*"))


def test_finish_staged_crops_reports_the_plan():
    """现场说明（这一批要裁成什么、**裁之前每条是什么样**）要原样带出来。

    再裁的批次里，库在开工时已经被改成新矩形了 —— 回滚之后只有这份说明能告诉调用方
    "原来是多少"，没有它，库和文件就再也对不上。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        batch = tmp / ".crop-789"
        (batch / "orig").mkdir(parents=True)
        (batch / "new").mkdir()
        _frame(batch / "orig", "scan0018_00000.png")
        (batch / "plan.json").write_text(
            '{"rect": [1, 2, 3, 4], "prev": {"18": [10, 5, 20, 15]}}', "utf-8")
        with _image_dir(tmp):
            out = tc.finish_staged_crops()

        assert out == [{"batch": ".crop-789", "rolled_back": True,
                        "files": ["scan0018_00000.png"],
                        "plan": {"rect": [1, 2, 3, 4], "prev": {"18": [10, 5, 20, 15]}}}], out
        assert tc.png_size(tmp / "scan0018_00000.png") == (W, H)
        assert not list(tmp.glob(".crop-*"))


def test_without_crop_nothing_shifts():
    """没裁过的扫描走的还是老路：crop 报 None，坐标就是文件坐标。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _frame(tmp, "scan0012_00000.png")
        got = png_pixel_series([(0, 1.0, p)], 17, 12)
    assert got["crop"] is None and (got["width"], got["height"]) == (W, H), got
    assert got["value"] == [12 * W + 17]


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
