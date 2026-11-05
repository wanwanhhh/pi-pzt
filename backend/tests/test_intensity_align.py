"""整幅图的强度对齐：离线自检，**不碰相机、不碰硬件**。

数据页「强度对齐」那张卡取的是 backend/intensity_align.align_maps：一遍读盘得到两张与 n 无关的图
（带内交流能量 Σb²、直流 d），n 只决定最后那个逐像素因子。要守住的几条：

  1. **定义的自证**：energy 必须逐像素等于「把这段波长加回时域、再逐点平方求和」——
     也就是与 frontend/app.js 的 filteredSignal 同式的直接算法。这条钉子钉住 G 与去均值。
  2. **G 不是对角阵**：采样非均匀时丢掉交叉项就变了数（钉住"不能只取对角"这条决定）。
  3. **dc 是这条扫描该像素的均值**（每帧读一遍就顺手加出来的那张图）。
  4. **全通也能算**（与相位图的重要区别：这里不需要载波）。
  5. **缺一个点整条不算**（与频谱、相位图同一条规矩）；裁剪只改读文件那一层。
  6. **累加内存上限在分配之前判**：没裁剪时幅面由第一帧说了算 —— 判定必须放在那之后、
     np.zeros 之前。放到「读完再判」就是先分配 2K 张图、再拒（全通全幅会先要 23 GiB）。

**第 1 条钉的是「这条式子该给什么数」，不是「现状给什么数」**：直接算法走 lstsq（QR/SVD），
被测的 `_prep` 走 2×2 正规方程 —— 两条不同代数路线。两边都写成正规方程时，
同一处代数错（scs 符号写反）会一起错、互相印证，测试反而全绿（实测过）。

直接跑：python backend/tests/test_intensity_align.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ  # noqa: E402
from backend.intensity_align import _prep, align_maps  # noqa: E402
from backend.thorlabs_ccd import _save_png16  # noqa: E402


def _band_of(k_lo, k_hi, df_thz):
    """波长框：λ 与 ν 反向，所以「波长最小」管 k 的上限、「波长最大」管下限（半格余量）。"""
    lmin = WAVELENGTH_UM_PER_THZ / ((k_hi + 0.5) * df_thz)
    lmax = WAVELENGTH_UM_PER_THZ / ((k_lo - 0.5) * df_thz)
    return lmin, lmax


def _scan(w, h, n, lines, tmp, seed=3):
    """造一条**多根谱线**的扫描：每个像素 x(t) = base(x,y) + Σ_k a_k·cos(k·θ(t) + φ_k(x,y))。

    base 逐像素不同（亮暗不均）—— 这张图要做的正是把它除掉。
    """
    rng = np.random.default_rng(seed)
    um = list(np.cumsum(rng.uniform(0.03, 0.09, n)))
    t = np.array(um) * TIME_FS_PER_UM
    span = float(t.max() - t.min())
    df = 1.0 / span
    th = 2 * np.pi * df * (t - t.min())
    base = 300.0 + 400.0 * rng.random((h, w))
    ph = {k: rng.uniform(0, 2 * np.pi, (h, w)) for k in lines}
    images = []
    for i in range(n):
        img = np.empty((h, w), dtype=np.uint16)
        for y in range(h):
            for x in range(w):
                v = base[y, x]
                for k, a in lines.items():
                    v += a * np.cos(k * th[i] + ph[k][y, x])
                img[y, x] = int(round(min(65535, max(0, v))))
        images.append(img)
    paths = []
    for i, img in enumerate(images):
        p = tmp / f"scan0001_{i:05d}.png"
        _save_png16(p, img)
        paths.append(p)
    items = [(i, float(um[i]), paths[i]) for i in range(n)]
    return {"items": items, "images": images, "base": base, "ph": ph, "th": th,
            "df": df, "span": span, "df_thz": 1e3 / span, "t": t}


def _direct_energy(images, th, k_lo, k_hi):
    """**直接算法**（与 app.js 的 filteredSignal 同式）：逐像素解 A/B，再逐点加回、平方求和。

    **系数走 lstsq（QR/SVD），不走正规方程** —— 被测代码（`_prep`）用的是 2×2 的代数式
    （`(yc·sss − ys·scs)/det`）。两条路线算法不同，所以这一条钉的是「这条式子该给什么数」，
    不是「现状给什么数」；如果两边都写成正规方程，同一处代数错（比如 scs 符号写反）
    会一起错、互相印证，测试反而全绿。
    """
    series = np.array([np.asarray(im).ravel() for im in images], dtype=np.float64)
    xm = series - series.mean(axis=0)          # 去均值（前端也是先减均值再拟合）
    n = series.shape[0]
    b = np.zeros_like(series)
    for k in range(k_lo, k_hi + 1):
        basis = np.stack([np.cos(k * th), np.sin(k * th)], axis=1)
        coef, *_ = np.linalg.lstsq(basis, xm, rcond=None)   # (2, npix)
        b += coef[0][None, :] * basis[:, 0][:, None] + coef[1][None, :] * basis[:, 1][:, None]
    assert b.shape == (n, series.shape[1])
    return (b * b).sum(axis=0), series.mean(axis=0)


def test_energy_equals_the_direct_reconstruction():
    """**定义的自证**：一遍扫出来的 energy = 逐像素把这段波长加回时域、逐点平方求和。

    顺带钉住 dc：它就是这条扫描该像素的均值（这里再独立算一遍）。
    """
    import tempfile

    lines = {11: 30.0, 13: 12.0, 16: 6.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(5, 4, 40, lines, Path(d))
        lmin, lmax = _band_of(min(lines), max(lines), s["df_thz"])
        got = align_maps(s["items"], lmin=lmin, lmax=lmax)
        want, dc = _direct_energy(s["images"], s["th"], min(lines), max(lines))
        mean = np.array([np.asarray(im).ravel() for im in s["images"]],
                        dtype=np.float64).mean(axis=0)

    assert (got["k_lo"], got["k_hi"]) == (min(lines), max(lines)), (got["k_lo"], got["k_hi"])
    assert (got["w"], got["h"], got["n"]) == (5, 4, 40), (got["w"], got["h"], got["n"])
    assert got["rect"] is None
    e = got["energy"].reshape(-1)
    # 容差 1e-12：实测最大相对偏差 2.4e-15（两条不同代数路线：lstsq 的 QR/SVD vs 2×2 正规方程），
    # 容的是 BLAS 求和顺序，不是「差一点点也算对」。不给 atol —— energy 量级 1e4，atol 永远被盖住。
    assert np.allclose(e, want, rtol=1e-12), np.abs(e - want).max() / want.max()
    assert np.allclose(got["dc"].reshape(-1), mean, rtol=1e-12), \
        np.abs(got["dc"].reshape(-1) - mean).max()


def test_cross_terms_matter_on_uneven_sampling():
    """G **不是对角阵**：采样非均匀时，只取对角（当成各根正交）算出来的数就变了。

    钉住的是「交叉项不能丢」这条决定 —— 丢了不会报错，只会悄悄给另一个数。
    """
    import tempfile

    lines = {9: 25.0, 12: 18.0, 15: 8.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(3, 3, 36, lines, Path(d), seed=11)
        lmin, lmax = _band_of(min(lines), max(lines), s["df_thz"])
        got = align_maps(s["items"], lmin=lmin, lmax=lmax)
        th = s["th"]
        k_lo, k_hi = got["k_lo"], got["k_hi"]
        phi = np.stack([np.cos(k * th) for k in range(k_lo, k_hi + 1)] +
                       [np.sin(k * th) for k in range(k_lo, k_hi + 1)], axis=1)   # (N, 2K)
        g = phi.T @ phi
        g_diag = np.diag(np.diag(g))
        # 同一批系数、两种 G：完整 vs 只取对角。系数就是模块解出来的那一批
        # （**逐根 2×2 解**，与前端同式 —— 不是把 2K 个基函数放在一起做联合最小二乘，那是另一批系数）
        series = np.array([np.asarray(im).ravel() for im in s["images"]], dtype=np.float64)
        t_fs = [p * TIME_FS_PER_UM for (_, p, _) in s["items"]]
        _ks, rows_a, rows_b, _phi = _prep(t_fs, k_lo, k_hi, s["span"])
        coeff = np.concatenate([series.T @ rows_a.T, series.T @ rows_b.T], axis=1)
        full = ((coeff @ g) * coeff).sum(axis=1)
        diag = ((coeff @ g_diag) * coeff).sum(axis=1)

    e = got["energy"].reshape(-1)
    assert np.allclose(e, full, rtol=1e-12), np.abs(e - full).max() / full.max()
    assert np.abs(diag - full).max() / full.max() > 1e-3, "交叉项在这里本该有可见的贡献"


def test_all_pass_works_without_a_carrier():
    """**全通也能算**：两个波长框都空 = 所有谱线的交流能量 —— 这里不需要载波（相位图才需要）。"""
    import tempfile

    lines = {7: 20.0, 10: 9.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(4, 3, 32, lines, Path(d))
        got = align_maps(s["items"])
        want, _ = _direct_energy(s["images"], s["th"], 1, (32 - 1) // 2)

    assert (got["band"], got["k_lo"], got["k_hi"]) == ([None, None], 1, (32 - 1) // 2), \
        (got["band"], got["k_lo"], got["k_hi"])
    assert np.allclose(got["energy"].reshape(-1), want, rtol=1e-12)


def test_missing_point_refuses_the_whole_scan():
    """缺一个点（没图 / 没记读数）整条不算 —— 说清是第几个点；与频谱、相位图同一条规矩。"""
    import tempfile

    lines = {6: 15.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(3, 2, 8, lines, Path(d))
        lmin, lmax = _band_of(6, 6, s["df_thz"])
        no_img = list(s["items"])
        no_img[5] = (5, s["items"][5][1], None)
        try:
            align_maps(no_img, lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "没采到图" in str(exc) and "第 6 个点" in str(exc), exc
        else:
            raise AssertionError("缺图的点没有报错")
        no_um = list(s["items"])
        no_um[2] = (2, None, s["items"][2][2])
        try:
            align_maps(no_um, lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "没记下读出位置" in str(exc) and "第 3 个点" in str(exc), exc
        else:
            raise AssertionError("没记读数的点没有报错")
        try:
            align_maps(s["items"][:2], lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "至少要 3 个" in str(exc), exc
        else:
            raise AssertionError("点太少没有报错")


def test_crop_only_shifts_the_reading():
    """裁过的扫描：数组是**盘上那张图**的形状，rect 是**原始坐标** —— 逐像素等于原图那块子图。

    **必须真裁**：盘上的图要是 8×6、另存一份 5×4 的裁剪块，两次读的才是不同字节。
    从前这里是拿一张 5×4 的原图、crop 也写 5×4 —— `read_frames` 只比对形状不切片，
    两次跑的是同一批字节、同一次计算，那两条 allclose 永远不会失败。
    """
    import tempfile

    lines = {8: 22.0, 12: 7.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(8, 6, 24, lines, Path(d))
        lmin, lmax = _band_of(8, 12, s["df_thz"])
        full = align_maps(s["items"], lmin=lmin, lmax=lmax)
        # 另存一份真裁过的图（原始坐标里 (2,1) 起、5×4 那一块），再按裁剪记录读
        cutdir = Path(d) / "cut"
        cutdir.mkdir()
        cut_items = []
        for i, im in enumerate(s["images"]):
            sub = np.ascontiguousarray(im[1:5, 2:7])
            p = cutdir / f"scan0001_{i:05d}.png"
            _save_png16(p, sub)
            cut_items.append((i, float(s["items"][i][1]), p))
        cut = align_maps(cut_items, crop=(2, 1, 5, 4), lmin=lmin, lmax=lmax)
        try:
            align_maps(s["items"], crop=(2, 1, 9, 9), lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "库和文件对不上" in str(exc), exc
        else:
            raise AssertionError("裁剪尺寸对不上没有报错")

    assert cut["rect"] == [2, 1, 5, 4] and (cut["w"], cut["h"]) == (5, 4), cut["rect"]
    assert full["rect"] is None and (full["w"], full["h"]) == (8, 6)
    # 逐像素等于原图那块子图（裁剪只改读文件那一层：同一叠帧、同一套式子、换了个窗口）
    assert np.allclose(cut["energy"], full["energy"][1:5, 2:7], rtol=1e-12)
    assert np.allclose(cut["dc"], full["dc"][1:5, 2:7], rtol=1e-12)


def test_memory_limit_refuses_before_allocating():
    """内存上限**在分配之前**判：拒的时候一帧都不该读过（更不该分配那 2K 张图）。

    没裁剪时幅面由第一帧说了算，所以判定只能放在 _accumulate 拿到第一帧之后、
    np.zeros 之前。放到「读完再判」就变成先分配、再拒 —— 全通全幅（K=999、1440×1080）
    要 23 GiB，那是 OOM 而不是一句人话。
    """
    import tempfile

    from backend import intensity_align

    lines = {8: 22.0, 12: 7.0}
    with tempfile.TemporaryDirectory() as d:
        s = _scan(5, 4, 24, lines, Path(d))
        lmin, lmax = _band_of(8, 12, s["df_thz"])
        seen = []
        old = intensity_align.ALIGN_MAX_BYTES
        intensity_align.ALIGN_MAX_BYTES = 1        # 1 字节：5 根谱线怎么算都超
        try:
            try:
                align_maps(s["items"], lmin=lmin, lmax=lmax,
                           progress=lambda done, total: seen.append(done))
            except ValueError as exc:
                assert "超过上限" in str(exc) and "把波段收窄" in str(exc), exc
            else:
                raise AssertionError("超过上限没有拒")
        finally:
            intensity_align.ALIGN_MAX_BYTES = old
    # 关键：拒的时候**最多只读过 1 帧**（幅面由它决定，这一帧躲不掉），
    # 而不是把整条 24 帧读完再拒。从前这里是 24 帧全读完、2K 张图也分配好了才抛。
    assert seen == [1], f"拒之前读了 {seen} 帧（判定在分配/读盘之后）"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
