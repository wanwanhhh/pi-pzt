"""**一遍读盘**（backend/frames.py）：数据处理那一层读帧的唯一入口。三条不变式各钉一条。

相位图与强度对齐都从 frames.read_frames 读同一条扫描的 PNG，所以"这一帧能不能用"那三条
在这里钉一次：

  1. 读不出来（被删 / 被占用 / 不是图片）→ 说清是第几个点、哪条路径；
  2. 不是二维灰度图 → 说清；
  3. **库和文件对不上** → 当场拒。这一条最要紧：它不会抛异常，只会让整幅图整体错一块。
     「对不上」有两半，**两半都要核**：
     - 尺寸（库写 w×h，文件解出来不是这个大小）；
     - 矩形（文件自己 tEXt 里的 CropRect 与库里那一份不一致）—— 尺寸一样、原点挪两格，
       尺寸那一半一点都看不出来（实测：文件写 2,1,6,6、库写 3,1,6,6，energy 差 0.0，
       整幅图却整体错位）。

**文件里没有 CropRect 而库里有 crop，也算对不上**：历史上每一次裁剪都写这块 tEXt
（`crop_frames` 从有裁剪那一天起就写 `CropRect`，见 git log -S 'CropRect'），所以
"文件没写"只可能是这一份记录不是它自己的 —— 不当老图放过。

**读裁剪记录只读块头**：整幅图那条路每帧都要核一遍，2000 帧按整幅读就是几 GB。
`read_png_meta` 逐块走到 IEND，IDAT 整块 seek 过去、一个字节都不读（这条也钉在下面）。

直接跑：python backend/tests/test_frames.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import frames  # noqa: E402
from backend.thorlabs_ccd import _save_png16, read_png_meta  # noqa: E402


def _frame(tmp: Path, name: str, w: int = 6, h: int = 6, crop=None, seed: int = 0) -> Path:
    """造一张 16 位帧，值逐像素不同（错位一个像素就看得出来）；crop 给了就写进 tEXt。"""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 60000, (h, w), dtype=np.uint16)
    p = tmp / name
    text = [] if crop is None else [(b"CropRect", ("%d,%d,%d,%d" % tuple(crop)).encode())]
    _save_png16(p, img, extra_text=text)
    return p


def _items(paths):
    return [(i, 1.0 + i, p) for i, p in enumerate(paths)]


def _drain(items, crop):
    """把整条读完（read_frames 是生成器：不变式在迭代里才触发）。"""
    return [(i, sh) for i, sh, _ in frames.read_frames(items, crop=crop)]


def test_matching_crop_records_pass():
    """**两份一致就放行**：文件写着 2,1,6,6、库里也记着 2,1,6,6 —— 照读，形状照文件。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0001_{i:05d}.png", crop=(2, 1, 6, 6), seed=i)
                 for i in range(3)]
        got = _drain(_items(paths), (2, 1, 6, 6))
    assert [sh for _, sh in got] == [(6, 6)] * 3, got


def test_a_moved_origin_is_refused():
    """**原点挪了两格、尺寸一模一样**：这一格不核矩形就完全看不出来（整幅图整体错位）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0002_{i:05d}.png", crop=(2, 1, 6, 6), seed=i)
                 for i in range(3)]
        try:
            _drain(_items(paths), (3, 1, 6, 6))
        except ValueError as exc:
            assert "库和文件对不上" in str(exc) and "第 1 个点" in str(exc), exc
        else:
            raise AssertionError("文件写 2,1,6,6、库写 3,1,6,6，居然照算了")


def test_a_file_without_the_crop_record_is_refused_when_the_db_has_one():
    """库里有 crop、文件里没有 CropRect：也算对不上（历史上每一次裁剪都写这块 tEXt）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0003_{i:05d}.png", seed=i) for i in range(3)]
        assert read_png_meta(paths[0])["crop"] is None, "这条用例要的正是「文件没写」"
        try:
            _drain(_items(paths), (2, 1, 6, 6))
        except ValueError as exc:
            assert "库和文件对不上" in str(exc), exc
        else:
            raise AssertionError("库里说裁过、文件里没有记录，居然照算了")


def test_a_wrong_size_is_refused_too():
    """尺寸那一半照旧：文件是 6×6、库写 5×4 → 拒（两条判据都在 check_frame 里）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0004_{i:05d}.png", crop=(2, 1, 6, 6), seed=i)
                 for i in range(3)]
        try:
            _drain(_items(paths), (2, 1, 5, 4))
        except ValueError as exc:
            assert "库和文件对不上" in str(exc), exc
        else:
            raise AssertionError("裁剪尺寸对不上居然照算了")


def test_a_file_that_says_it_was_cropped_is_refused_when_the_db_has_no_record():
    """反过来的那一半：文件自己写着裁过、库里没有 crop —— 也拒。

    照 crop=None 读，坐标就被当成原始坐标，而文件其实只是原始帧里的一块：整幅图/曲线
    整体错位，一样不报错。两份记录说的不是一件事，就停下来查。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0005_{i:05d}.png", crop=(2, 1, 6, 6), seed=i)
                 for i in range(3)]
        try:
            _drain(_items(paths), None)
        except ValueError as exc:
            assert "库和文件对不上" in str(exc), exc
        else:
            raise AssertionError("文件写着裁过、库里没记录，居然照算了")


def test_without_a_crop_record_nothing_is_compared():
    """没裁过的扫描：库里没有 crop，文件里也没有 —— 照读，不拿"没有"去比"没有"。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        paths = [_frame(tmp, f"scan0006_{i:05d}.png", seed=i) for i in range(3)]
        got = _drain(_items(paths), None)
    assert [sh for _, sh in got] == [(6, 6)] * 3, got


def test_reading_the_crop_record_does_not_read_the_pixels():
    """**只读块头**：裁剪记录那一条不解码、也不把整幅读进内存。

    整幅图那条路每帧都要核一遍（2000 帧、每帧 1~2 MB 就是几 GB）。这里把 `open` 换成
    一个"只许小块读"的守卫：谁想一次 `read()` 读整幅（或者干脆 `read_bytes`），当场红。
    tEXt 在 IDAT 之前，所以这条路实际只读几百字节。
    """
    import builtins
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        rng = np.random.default_rng(5)
        img = rng.integers(0, 60000, (540, 720), dtype=np.uint16)
        p = tmp / "scan0006_00000.png"
        _save_png16(p, img, extra_text=[(b"CropRect", b"2,1,6,6")])
        assert p.stat().st_size > 500_000, "随机噪声压不动：这就是一张真的整幅帧"

        real = builtins.open
        seen = {"bytes": 0}

        class _SmallReads:
            """只许小块读：一次 read(-1)（整幅）就是这条用例要抓的那种做法。"""

            def __init__(self, f):
                self._f = f

            def read(self, n=-1):
                if n < 0 or n > 65536:
                    raise AssertionError(f"一次读了 {n} 字节 —— 那是整幅，不是块头")
                b = self._f.read(n)
                seen["bytes"] += len(b)
                return b

            def seek(self, off, whence=0):
                return self._f.seek(off, whence)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._f.close()
                return False

        def guarded(file, *a, **kw):
            f = real(file, *a, **kw)
            return _SmallReads(f) if str(file) == str(p) else f

        builtins.open = guarded
        try:
            meta = read_png_meta(p)
        finally:
            builtins.open = real

    assert meta["crop"] == [2, 1, 6, 6], meta
    assert seen["bytes"] < 4096, seen["bytes"]     # 读的是块头，不是整幅那几 MB


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:                 # noqa: BLE001 —— 自检脚本：把失败打全
            failed += 1
            print(f"FAIL  {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
