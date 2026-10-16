"""整幅图的相位差：离线自检，**不碰相机、不碰硬件**。

数据处理页「相位图」那张卡取的是 backend/phasemap.phase_map：把一条扫描的每一帧读一遍，
每个像素得到一个复数（相位 = arg，条纹强度 = |R|）。要守住的几条：

  1. **一遍扫完**：R 对样本是线性的，所以整幅图 = 每帧「权重 × 图」累加 ——
     像素个数不进代价。这条自检拿**逐像素直接解**（与 frontend/app.js 的
     pixelSpectrum / filteredSignal 同式）对着比，两边必须逐像素一致。
  2. **相位不锚定**：滤波曲线把首点锚成 0（为了看残差），相位图要量的**正是那个常数**。
     一根谱线时"两个像素每个时刻的相位差"是一条常数曲线，它的平均就是这里的数。
  3. **缺一个点整条不算**（没图 / 没记读数）：与频谱那张图同一条规矩。
  4. 波段 → 谱线（λ = c/ν、上限卡名义 Nyquist）、裁剪只改读文件那一层（rect 是原始坐标）。

直接跑：python backend/tests/test_phase_map.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ  # noqa: E402
from backend.phasemap import band_lines, phase_map, weight_vector  # noqa: E402
from backend.thorlabs_ccd import _save_png16  # noqa: E402


def _wrap(a):
    """折到 (−π, π]：界面上那两个相位差就是这么算的（差超过半个条纹就绕回来）。"""
    return (a + np.pi) % (2 * np.pi) - np.pi


def _truth(w, h):
    """每个像素一个**已知**相位（弧度）：横着走得多、竖着走得少。"""
    return np.array([[0.7 * x - 0.4 * y + 0.2 for x in range(w)] for y in range(h)])


def _scan(w, h, n, k_sig, step_um=0.0819, amp=40.0, base=500.0, scan=1, tmp=None):
    """造一条**单频**扫描：每个像素 x(t) = base + amp·cos(2π·k_sig·df·t + φ(x,y))。

    条纹频率正好落在谱的第 k_sig 格上（k·df 是格点），所以那一根解出来的幅度、相位都是干净的
    —— 核对时不用留混叠的余量。返回包里带上那一叠图本身（有些用例要按前端的式子自己再解一遍）。
    """
    um = [i * step_um for i in range(n)]
    t = np.array(um) * TIME_FS_PER_UM
    span = float(t.max() - t.min())
    df = 1.0 / span                       # 1/fs
    df_thz = 1e3 / span
    # **单位相位**（第 1 格的相位）：第 k 格的相位 = k·它 —— 前端的式子里就是这个约定，
    # 所以带出去的是它，别先把 k_sig 乘进去（乘两遍就是另一条序列了）。
    th = 2 * np.pi * df * (t - t.min())
    ph = _truth(w, h)
    images = []
    for i in range(n):
        img = np.empty((h, w), dtype=np.uint16)
        for y in range(h):
            for x in range(w):
                img[y, x] = int(round(base + amp * np.cos(k_sig * th[i] + ph[y, x])))
        images.append(img)
    paths = []
    for i, img in enumerate(images):
        p = tmp / f"scan{scan:04d}_{i:05d}.png"
        _save_png16(p, img)
        paths.append(p)
    items = [(i, float(um[i]), paths[i]) for i in range(n)]
    return {"items": items, "images": images, "ph": ph, "k_sig": k_sig,
            "df_thz": df_thz, "t": t, "th": th, "paths": paths}


def _band_of(k_sig, df_thz):
    """挑两个波长框，使留下的谱线**正好只有第 k_sig 根**。

    λ 与 ν 反向：短波那一头是高频，所以「波长最小」管的是 k 的上限（见 app.js 的 filtKeepRange）。
    λ 框比格宽一点点会多留一根 —— 这里取半格，两边都卡在第 k_sig 根上。
    """
    lmin = WAVELENGTH_UM_PER_THZ / ((k_sig + 0.5) * df_thz)     # 管 k 上限 → 波长最小
    lmax = WAVELENGTH_UM_PER_THZ / ((k_sig - 0.5) * df_thz)     # 管 k 下限 → 波长最大
    return lmin, lmax


def _series(images, y, x):
    """一个像素在每一帧上的值（**原始坐标**里的 (x, y)）。"""
    return np.array([float(np.asarray(im)[y, x]) for im in images])


def _ls_coeffs(series, th, k_lo, k_hi):
    """逐像素按**前端的式子**解出每根谱线的复数 c_k = A_k − iB_k（app.js 的 filteredSignal 同式）。

    series 是 (像素, 时间)；去均值在这一层做（前端也是先减均值再拟合 —— 不去的话
    cos/sin 在非均匀网格上不正交，直流量会按每个像素自己的亮度漏进系数）。
    """
    xm = series - series.mean(axis=1, keepdims=True)
    out = np.zeros((series.shape[0], k_hi - k_lo + 1), dtype=complex)
    for j, k in enumerate(range(k_lo, k_hi + 1)):
        cc, ss = np.cos(k * th), np.sin(k * th)
        scc, sss, scs = cc @ cc, ss @ ss, cc @ ss
        det = scc * sss - scs * scs
        yc, ys = xm @ cc, xm @ ss
        out[:, j] = (yc * sss - ys * scs) / det - 1j * (ys * scc - yc * scs) / det
    return out


def test_map_recovers_a_known_phase_tilt():
    """已知的相位倾斜必须原样回来：Δφ(像素) − Δφ(基准) = φ(像素) − φ(基准)（模 2π）。

    **只有差有意义**：绝对值跟公共载波、跟 t0 有关，界面上的数也是减过基准的。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(8, 6, 64, 10, tmp=Path(d))
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        got = phase_map(s["items"], lmin=lmin, lmax=lmax)

    assert (got["k_lo"], got["k_hi"]) == (10, 10), (got["k_lo"], got["k_hi"])
    assert (got["w"], got["h"], got["n"]) == (8, 6, 64), (got["w"], got["h"], got["n"])
    assert got["rect"] is None                      # 没裁过：整幅就是原始坐标
    ref = (2, 3)                                    # (行, 列) —— 数组是盘上那张图的顺序
    d = _wrap(got["phase"] - got["phase"][ref])
    want = _wrap(s["ph"] - s["ph"][ref])
    assert np.allclose(d, want, atol=0.02), np.abs(_wrap(d - want)).max()
    assert abs(float(d[ref])) < 1e-6, d[ref]         # 基准那一格恒为 0 色
    # 条纹强度 ∝ 每像素的条纹幅度：这一条里每个像素同幅度，所以该是一个常数
    assert np.allclose(got["amp"], got["amp"][0, 0], rtol=0.02), got["amp"]


def test_map_equals_the_direct_per_pixel_ls():
    """**定义的自证**：一遍扫出来的数 = 逐像素按前端的式子直接解。

    前端那条路（app.js 的 pixelSpectrum / filteredSignal）解出的是同一对 A/B；相位图再拿它们
    与**全图共用的那条载波**（留下那一段的中点 k_c）相关：R = Σ_k W_k·c_k，
    W_k = Σ_t e^(i2π(k−k_c)·df·(t−t0))。**改前端的式子就要改这里** —— 这条用例就是那个钉子。
    """
    import tempfile

    rng = np.random.default_rng(7)
    n, w, h = 48, 5, 4
    um = list(np.cumsum(rng.uniform(0.03, 0.09, n)))     # 非均匀的读出位置（真实扫描就是这样）
    t = np.array(um) * TIME_FS_PER_UM
    span = float(t.max() - t.min())
    df, df_thz, t0 = 1.0 / span, 1e3 / span, float(t.min())
    th = 2 * np.pi * df * (t - t0)
    lines = {11: 30.0, 13: 12.0, 16: 6.0}                # 三根谱线，幅度各不相同
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        images, paths = [], []
        for i in range(n):
            img = np.empty((h, w), dtype=np.uint16)
            for y in range(h):
                for x in range(w):
                    v = 500.0
                    for k, a in lines.items():
                        v += a * np.cos(k * th[i] + 0.3 * x - 0.2 * y)
                    img[y, x] = int(round(v))
            images.append(img)
            p = tmp / f"scan0001_{i:05d}.png"
            _save_png16(p, img)
            paths.append(p)
        items = [(i, float(um[i]), paths[i]) for i in range(n)]
        # λ 框：管上限的写在 lmin（短波那头），管下限的写在 lmax
        lmin = WAVELENGTH_UM_PER_THZ / ((max(lines) + 0.5) * df_thz)
        lmax = WAVELENGTH_UM_PER_THZ / ((min(lines) - 0.5) * df_thz)
        got = phase_map(items, lmin=lmin, lmax=lmax)

        k_lo, k_hi = got["k_lo"], got["k_hi"]
        assert (k_lo, k_hi) == (min(lines), max(lines)), (k_lo, k_hi)
        series = np.array([np.asarray(im).ravel() for im in images], dtype=np.float64).T
        c = _ls_coeffs(series, th, k_lo, k_hi)
        k_c = (k_lo + k_hi) / 2.0
        W = np.array([np.exp(1j * (k - k_c) * th).sum() for k in range(k_lo, k_hi + 1)])
        want = c @ W

    gotp = got["phase"].reshape(-1)
    gota = got["amp"].reshape(-1)
    assert np.allclose(np.cos(gotp), np.cos(np.angle(want)), atol=1e-4), np.abs(_wrap(gotp - np.angle(want))).max()
    assert np.allclose(np.sin(gotp), np.sin(np.angle(want)), atol=1e-4)
    assert np.allclose(gota, np.abs(want), rtol=1e-4), np.abs(gota - np.abs(want)).max()


def test_one_line_means_the_pointwise_average_is_the_same_number():
    """**"能不能算成逐点相位差的平均"**：一根谱线时，两个像素每个时刻的相位差是一条常数曲线 ——
    它的平均（算术平均 = 向量平均 = 这里的数）三者是同一个数。

    反过来也说明了为什么不能拿〈滤波曲线〉那条相位曲线去平均：那条把首点锚成 0、
    还减掉了每像素自己的线性相位 —— 被锚掉的正是这个常数，两条相减会恒等于 0。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 4, 40, 9, tmp=Path(d))
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        got = phase_map(s["items"], lmin=lmin, lmax=lmax)

        # 同一叠图，按前端的式子解出那一根谱线 → 重建解析信号 z(t) = c·e^(iθ) → 逐点做相位差
        series = np.array([np.asarray(im).ravel() for im in s["images"]], dtype=np.float64).T
        c = _ls_coeffs(series, s["th"], got["k_lo"], got["k_hi"])
        z = c[:, :1] * np.exp(1j * s["k_sig"] * s["th"])[None, :]
        ref = 2 * 6 + 3                                   # 基准像素 (x=3, y=2)
        pointwise = _wrap(np.angle(z) - np.angle(z[ref]))
        mean_of_pointwise = pointwise.mean(axis=1)

    dmap = _wrap(got["phase"].reshape(-1) - got["phase"].reshape(-1)[ref])
    assert pointwise.std(axis=1).max() < 1e-9, pointwise.std(axis=1).max()   # 逐点是常数（一根谱线）
    assert np.allclose(_wrap(mean_of_pointwise - mean_of_pointwise[ref]), dmap, atol=1e-6), \
        np.abs(_wrap(mean_of_pointwise - mean_of_pointwise[ref] - dmap)).max()


def test_band_lines_follows_lambda_equals_c_over_nu():
    """波段 → 谱线：λ 与 ν 反向（短波那头是高频）、上限卡名义 Nyquist、一根不剩要报错。"""
    n, span = 101, 600.0
    k_max = (n - 1) // 2
    df_thz = 1e3 / span
    k_lo, k_hi, km, df = band_lines(None, None, n, span)
    assert (k_lo, k_hi, km) == (1, k_max, k_max) and abs(df - df_thz) < 1e-12, (k_lo, k_hi, km)
    k_lo, k_hi, _, _ = band_lines(WAVELENGTH_UM_PER_THZ / (10 * df_thz), None, n, span)
    assert (k_lo, k_hi) == (1, 10), (k_lo, k_hi)          # 只填「波长最小」= 只管频率上限
    k_lo, k_hi, _, _ = band_lines(None, WAVELENGTH_UM_PER_THZ / (4 * df_thz), n, span)
    assert (k_lo, k_hi) == (4, k_max), (k_lo, k_hi)       # 只填「波长最大」= 只管频率下限
    k_lo, k_hi, _, _ = band_lines(1e-6, None, n, span)    # 比 Nyquist 还短：上限仍是名义 Nyquist
    assert (k_lo, k_hi) == (1, k_max), (k_lo, k_hi)
    try:
        band_lines(WAVELENGTH_UM_PER_THZ / (5 * df_thz), WAVELENGTH_UM_PER_THZ / (9 * df_thz),
                   n, span)
    except ValueError as exc:
        assert "一根谱线都没有" in str(exc), exc
    else:
        raise AssertionError("空波段没有报错")
    try:
        band_lines(None, None, 10, 0.0)
    except ValueError as exc:
        assert "跨度 0" in str(exc), exc
    else:
        raise AssertionError("跨度 0 没有报错")


def test_missing_point_refuses_the_whole_scan():
    """缺一个点（没图 / 没记读数）整条不算 —— 说清是第几个点；与频谱那张图同一条规矩。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(4, 3, 8, 3, tmp=Path(d))
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        no_img = list(s["items"])
        no_img[5] = (5, s["items"][5][1], None)
        try:
            phase_map(no_img, lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "没采到图" in str(exc) and "第 6 个点" in str(exc), exc
        else:
            raise AssertionError("缺图的点没有报错")
        no_um = list(s["items"])
        no_um[2] = (2, None, s["items"][2][2])
        try:
            phase_map(no_um, lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "没记下读出位置" in str(exc) and "第 3 个点" in str(exc), exc
        else:
            raise AssertionError("没记读数的点没有报错")
        try:
            phase_map(s["items"][:2], lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "至少要 3 个" in str(exc), exc
        else:
            raise AssertionError("点太少没有报错")


def test_crop_only_shifts_the_reading():
    """裁过的扫描：数组是**盘上那张图**的形状，rect 是**原始坐标** —— 偏移只在读文件那一层减一次。

    同一叠图，裁着读和不裁着读必须给出逐像素一样的数（差的只是 rect 那个原点）。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = _scan(5, 4, 32, 6, tmp=tmp)
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        full = phase_map(s["items"], lmin=lmin, lmax=lmax)
        cut = phase_map(s["items"], crop=(7, 9, 5, 4), lmin=lmin, lmax=lmax)
        try:
            phase_map(s["items"], crop=(7, 9, 9, 9), lmin=lmin, lmax=lmax)
        except ValueError as exc:
            assert "库和文件对不上" in str(exc), exc
        else:
            raise AssertionError("裁剪尺寸对不上没有报错")
    assert cut["rect"] == [7, 9, 5, 4] and (cut["w"], cut["h"]) == (5, 4), cut["rect"]
    assert full["rect"] is None and (full["w"], full["h"]) == (5, 4)
    assert np.allclose(cut["phase"], full["phase"], atol=1e-6)
    assert np.allclose(cut["amp"], full["amp"], rtol=1e-6)


def test_progress_and_workers_do_not_change_the_numbers():
    """进度按帧报；铺开读（线程池）与串行读给同一份数（累加顺序变了，值不许变）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 5, 24, 5, tmp=Path(d))
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        seen = []
        many = phase_map(s["items"], lmin=lmin, lmax=lmax, workers=4,
                         progress=lambda done, total: seen.append((done, total)))
        one = phase_map(s["items"], lmin=lmin, lmax=lmax, workers=1)
    assert seen[-1] == (24, 24) and len(seen) == 24, seen
    assert np.allclose(many["phase"], one["phase"], atol=1e-5)
    assert np.allclose(many["amp"], one["amp"], rtol=1e-6)


def test_weight_vector_is_really_one_pass():
    """一遍扫完的根据：R = Σ_t u(t)·x(t) —— u 是**一场扫描一条**，与像素无关。

    同一条 u 乘给不同的 x，必须等于逐像素解出来的数（线性性不是巧合，是这套做法的前提：
    像素个数因此不进代价，代价只在解 PNG 上）。
    """
    rng = np.random.default_rng(3)
    n = 200
    t = np.cumsum(rng.uniform(0.3, 0.5, n))
    span = float(t.max() - t.min())
    t0 = float(t.min())
    df = 1.0 / span
    th = 2 * np.pi * df * (t - t0)
    k_lo, k_hi, k_c = 20, 24, 22.0
    u, df2 = weight_vector(list(t), k_lo, k_hi, k_c, span)
    assert abs(df2 - df) < 1e-12, (df2, df)
    W = np.array([np.exp(1j * (k - k_c) * th).sum() for k in range(k_lo, k_hi + 1)])
    for _ in range(5):
        x = rng.normal(0, 40, n) + 500
        xm = x - x.mean()
        want = 0j
        for j, k in enumerate(range(k_lo, k_hi + 1)):
            cc, ss = np.cos(k * th), np.sin(k * th)
            scc, sss, scs = cc @ cc, ss @ ss, cc @ ss
            det = scc * sss - scs * scs
            yc, ys = xm @ cc, xm @ ss
            want += W[j] * ((yc * sss - ys * scs) / det - 1j * (ys * scc - yc * scs) / det)
        assert abs((u * x).sum() - want) < 1e-6 * max(1.0, abs(want)), (u * x).sum() - want


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
