"""线包络（x-t / y-t）：离线自检，**不碰相机、不碰硬件**。

数据页「线包络」那张卡取的是 backend/line_envelope.line_envelope：沿一条线（固定一行或
固定一列）把每一帧读一遍，逐位置取带通信号的包络 |z|，再交出自己的直流 d。要守住的几条：

  1. **定义的自证**：env 必须逐点等于「把这段波长加回时域、取解析信号的模」——
     也就是与 frontend/app.js 的 filteredSignal 同式的直接算法（系数走 lstsq，不走
     被测代码那条 2×2 正规方程：两条不同的代数路线，才能钉住式子本身）。
  2. **dc 是这条线每个位置所有帧的均值** —— 与强度对齐的 d 同一个数。
  3. **位置是原始坐标**：裁过的扫描也一样（读文件那一层减一次偏移），出了画面就说清能填什么。
  4. **全通照算**（这里不需要载波）；缺一个点整条不算。

直接跑：python backend/tests/test_line_envelope.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ  # noqa: E402
from backend.line_envelope import line_envelope  # noqa: E402
from backend.thorlabs_ccd import _save_png16  # noqa: E402


def _band_of(k_lo, k_hi, df_thz):
    """波长框：λ 与 ν 反向，所以「波长最小」管 k 的上限、「波长最大」管下限（半格余量）。"""
    lmin = WAVELENGTH_UM_PER_THZ / ((k_hi + 0.5) * df_thz)
    lmax = WAVELENGTH_UM_PER_THZ / ((k_lo - 0.5) * df_thz)
    return lmin, lmax


def _scan(w, h, n, lines, tmp, seed=3, crop=None):
    """造一条**多根谱线**的扫描：每个像素 x(t) = base(x,y) + Σ_k a_k·cos(k·θ(t) + φ_k(x,y))。

    crop=(x0,y0,w,h) 时只把那一块写成 PNG，并让文件自带同一份 CropRect（与裁剪后的盘上
    文件一模一样的形状），库里那份由调用方传给 line_envelope。
    """
    rng = np.random.default_rng(seed)
    um = list(np.cumsum(rng.uniform(0.03, 0.09, n)))
    t = np.array(um) * TIME_FS_PER_UM
    span = float(t.max() - t.min())
    df = 1.0 / span
    th = 2 * np.pi * df * (t - t.min())
    full_w, full_h = w, h
    base = 300.0 + 400.0 * rng.random((full_h, full_w))
    ph = {k: rng.uniform(0, 2 * np.pi, (full_h, full_w)) for k in lines}
    images = []
    for i in range(n):
        img = np.empty((full_h, full_w), dtype=np.float64)
        for y in range(full_h):
            for x in range(full_w):
                v = base[y, x]
                for k, a in lines.items():
                    v += a * np.cos(k * th[i] + ph[k][y, x])
                img[y, x] = v
        images.append(img)
    x0, y0, cw, ch = crop if crop else (0, 0, full_w, full_h)
    paths, kept = [], []
    for i, img in enumerate(images):
        part = np.round(img[y0:y0 + ch, x0:x0 + cw]).astype(np.uint16)
        p = tmp / f"scan0001_{i:05d}.png"
        _save_png16(p, part, extra_text=[(b"CropRect", f"{x0},{y0},{cw},{ch}".encode())]
                    if crop else ())
        paths.append(p)
        kept.append(part)
    items = [(i, float(um[i]), paths[i]) for i in range(n)]
    return {"items": items, "images": images, "crop": crop, "th": th,
            "span": span, "df_thz": 1e3 / span, "t": t,
            "kept": np.stack(kept)}


def _direct_envelope(series, th, k_lo, k_hi):
    """**直接算法**（与 app.js 的 filteredSignal 同式）：逐位置解 A/B，再逐点拼 z、取模。

    series 是 (帧数, 位置数)。系数走 lstsq（QR/SVD）—— 被测代码走 2×2 正规方程，
    两条不同路线，同一处代数错才不会一起错。
    """
    series = np.asarray(series, dtype=np.float64)
    xm = series - series.mean(axis=0)
    zr = np.zeros_like(series)
    zi = np.zeros_like(series)
    for k in range(k_lo, k_hi + 1):
        c = np.cos(k * th)[:, None]
        s = np.sin(k * th)[:, None]
        basis = np.concatenate([c, s], axis=1)          # (帧数, 2)
        coef, *_ = np.linalg.lstsq(basis, xm, rcond=None)
        a, b = coef[0][None, :], coef[1][None, :]
        zr += a * c + b * s
        zi += a * s - b * c
    return np.hypot(zr, zi)


def _line(scan, axis, index, crop=None):
    """把一条线取出来（盘上那张图的坐标）：axis='y' 取一行、axis='x' 取一列。"""
    k = scan["kept"]
    return k[:, index, :] if axis == "y" else k[:, :, index]


def test_envelope_equals_the_direct_reconstruction():
    """**定义的自证**：一行（x-t）与一列（y-t）都要与直接算法逐点相等。"""
    import tempfile

    lines = {9: 30.0, 11: 12.0, 13: 6.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 5, 40, lines, Path(d))
        lmin, lmax = _band_of(min(lines), max(lines), s["df_thz"])
        want_row = _direct_envelope(_line(s, "y", 2), s["th"], min(lines), max(lines))
        want_col = _direct_envelope(_line(s, "x", 4), s["th"], min(lines), max(lines))
        row = line_envelope(s["items"], "y", 2, lmin=lmin, lmax=lmax)
        col = line_envelope(s["items"], "x", 4, lmin=lmin, lmax=lmax)
        assert row["env"].shape == (40, 6) and col["env"].shape == (40, 5)
        assert np.allclose(row["env"], want_row, atol=1e-9), "x-t 的包络与直接算法不一致"
        assert np.allclose(col["env"], want_col, atol=1e-9), "y-t 的包络与直接算法不一致"
        assert row["p"] == 6 and col["p"] == 5 and row["n"] == 40
        assert row["band"] == [lmin, lmax]
        assert row["from"] == 0 and row["at"] == 2 and row["axis"] == "y"


def test_dc_is_the_line_mean():
    """dc 就是这条线每个位置所有帧的均值（与强度对齐的 d 同一个数）。"""
    import tempfile

    lines = {9: 30.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(5, 4, 24, lines, Path(d))
        lmin, lmax = _band_of(9, 9, s["df_thz"])
        got = line_envelope(s["items"], "y", 1, lmin=lmin, lmax=lmax)
        want = _line(s, "y", 1).astype(np.float64).mean(axis=0)
        assert np.allclose(got["dc"], want), "直流不是这条线的均值"
        # 每帧的读出位置原样带出去（界面 hover 要用）
        assert np.allclose(got["pos_um"], [p for (_, p, _) in s["items"]])


def test_cropped_line_uses_original_coordinates():
    """裁过的扫描：位置仍按**原始坐标**说，偏移只在读文件那一层减一次。"""
    import tempfile

    lines = {9: 30.0, 12: 8.0}
    rect = (2, 1, 4, 3)                      # 原始 6×5 里的一块：x 2~5、y 1~3
    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 5, 30, lines, Path(d), crop=rect)
        lmin, lmax = _band_of(9, 12, s["df_thz"])
        got = line_envelope(s["items"], "y", 2, crop=rect, lmin=lmin, lmax=lmax)
        want = _direct_envelope(_line(s, "y", 1), s["th"], 9, 12)   # 原始 y = 2 → 本地 1
        assert got["from"] == 2 and got["p"] == 4 and got["rect"] == [2, 1, 4, 3]
        assert np.allclose(got["env"], want), "裁过的 x-t 与直接算法（本地那一行）不一致"
        got_col = line_envelope(s["items"], "x", 3, crop=rect, lmin=lmin, lmax=lmax)
        want_col = _direct_envelope(_line(s, "x", 1), s["th"], 9, 12)  # 原始 x = 3 → 本地 1
        assert got_col["from"] == 1 and got_col["p"] == 3
        assert np.allclose(got_col["env"], want_col), "裁过的 y-t 与直接算法不一致"
        # 出了画面就拒：原始 x 只能填 2 ~ 5
        try:
            line_envelope(s["items"], "x", 9, crop=rect, lmin=lmin, lmax=lmax)
            raise AssertionError("裁剪框外的位置应当报错")
        except ValueError as exc:
            assert "x 可填 2 ~ 5" in str(exc), str(exc)


def test_index_outside_the_frame_says_what_to_fill():
    """位置出了画面 = 填错了：说清能填哪一段，不返回一张错位的图。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 5, 20, {9: 30.0}, Path(d))
        try:
            line_envelope(s["items"], "y", 5)          # 行只有 0~4
            raise AssertionError("行号超出画面应当报错")
        except ValueError as exc:
            assert "y 可填 0 ~ 4" in str(exc), str(exc)
        try:
            line_envelope(s["items"], "x", 6)          # 列只有 0~5
            raise AssertionError("列号超出画面应当报错")
        except ValueError as exc:
            assert "x 可填 0 ~ 5" in str(exc), str(exc)
        try:
            line_envelope(s["items"], "y", -1)
            raise AssertionError("负数位置应当报错")
        except ValueError as exc:
            assert "不能是负数" in str(exc), str(exc)
        try:
            line_envelope(s["items"], "z", 1)
            raise AssertionError("轴名只有 x / y")
        except ValueError as exc:
            assert "x（固定一列" in str(exc), str(exc)


def test_all_pass_is_allowed():
    """两个波长框都空 = 全通，照算（这里不需要载波，与相位图相反）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(5, 4, 24, {9: 30.0}, Path(d))
        got = line_envelope(s["items"], "y", 1)
        assert got["band"] == [None, None] and got["k_lo"] == 1
        assert np.all(np.isfinite(got["env"])) and got["env"].max() > 0
        # 全通与某一段波段给出的不是同一张图（确实是按波段滤波）
        lmin, lmax = _band_of(9, 9, s["df_thz"])
        band = line_envelope(s["items"], "y", 1, lmin=lmin, lmax=lmax)
        assert not np.allclose(got["env"], band["env"])


def test_unfiltered_is_the_rectified_deviation():
    """不滤波：去掉带通，逐帧画 |I − d| —— 三步与分母一字不改，波段不参与。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(5, 4, 24, {9: 30.0}, Path(d))
        got = line_envelope(s["items"], "y", 1, unfiltered=True)
        line = _line(s, "y", 1).astype(np.float64)
        want = np.abs(line - line.mean(axis=0))
        assert np.allclose(got["env"], want), "不滤波的图不是 |I − d|"
        assert np.allclose(got["dc"], line.mean(axis=0))
        assert got["unfiltered"] is True and got["band"] == [None, None]
        assert "k_lo" not in got, "不滤波不分波段，不该有谱线元数据"
        # 与滤波口径不是同一张图（真的把带通去掉了）
        lmin, lmax = _band_of(9, 9, s["df_thz"])
        filt = line_envelope(s["items"], "y", 1, lmin=lmin, lmax=lmax)
        assert not np.allclose(got["env"], filt["env"])
        try:
            line_envelope(s["items"], "y", 1, lmin=lmin, lmax=lmax, unfiltered=True)
            raise AssertionError("不滤波还带波长应当报错")
        except ValueError as exc:
            assert "不滤波就不分波段" in str(exc), str(exc)


def test_missing_point_is_refused():
    """缺一个点整条不算 —— 缺一个点就是另一条采样序列（与频谱、相位图同一条规矩）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(4, 3, 12, {9: 30.0}, Path(d))
        items = list(s["items"])
        items[5] = (items[5][0], items[5][1], None)
        try:
            line_envelope(items, "y", 1)
            raise AssertionError("缺一个点应当整条拒掉")
        except ValueError as exc:
            assert "没采到图" in str(exc) and "第 6 个点" in str(exc), str(exc)


if __name__ == "__main__":
    test_envelope_equals_the_direct_reconstruction()
    test_dc_is_the_line_mean()
    test_cropped_line_uses_original_coordinates()
    test_index_outside_the_frame_says_what_to_fill()
    test_all_pass_is_allowed()
    test_unfiltered_is_the_rectified_deviation()
    test_missing_point_is_refused()
    print("线包络自检通过")
