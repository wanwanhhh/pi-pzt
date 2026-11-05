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
  5. **解不出的那根要丢掉**：判据是 2×2 正规方程的条件数（`bands.solvable`），不是行列式的符号 ——
     等间隔奇数点扫描的 Nyquist 那根 det > 0 却烂到系数放大 1e11，相位图两种等价算法差 3 rad。

直接跑：python backend/tests/test_phase_map.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.bands import SOLVABLE_KAPPA, solvable  # noqa: E402
from backend.config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ  # noqa: E402
from backend.phasemap import (  # noqa: E402
    _grid,
    band_lines,
    moment_weight_vector,
    phase_map,
    phase_map_at,
    weight_vector,
)
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


def test_unsolvable_line_is_dropped_by_the_condition_number():
    """**解不出的那根要丢**，判据是条件数 —— 不是 `det > 0`。

    等间隔奇数点扫描的 Nyquist 那根：`det ≈ 2.4e-19 > 0`（`det > 0` 放行），
    可两个特征值之比只有 6e-26 —— 系数被放大到 1e11，相位图两种等价算法差 3.1 rad。
    实测（本机 49 条扫描、5482 根）：真实读数噪声下最大 κ = 3.2e7，离这条线还有五个数量级；
    读数抖到 1e-6 µm（真实噪声的千分之一）时 Nyquist 那根才升到 1e11、1e-9 µm 时到 1e25。
    """
    # 刀尖：2001 点、步距严格相等（读数一点噪声都没有）
    n = 2001
    t = np.arange(n, dtype=np.float64) * TIME_FS_PER_UM
    span = float(t[-1] - t[0])
    th = 2 * np.pi * (1 / span) * (t - t.min())
    k_nyq = (n - 1) // 2
    c, s = np.cos(k_nyq * th), np.sin(k_nyq * th)
    scc, sss, scs = float(c @ c), float(s @ s), float(c @ s)
    det = scc * sss - scs * scs
    assert det > 0, "这条用例要的正是「det > 0 但解不出」那一格"
    assert not solvable(scc, sss, scs), (scc, sss, scs)
    # 同一串采样上低一档那根必须还解得出（判据不能把整条带都掐掉）
    c, s = np.cos((k_nyq - 1) * th), np.sin((k_nyq - 1) * th)
    assert solvable(float(c @ c), float(s @ s), float(c @ s))
    # 相位图真的把它丢掉了：留下的最后一根不是 Nyquist 那根
    _t, _t0, _df, _th, rows = _grid(t, 1, k_nyq, span)
    assert rows[-1][0] < k_nyq, rows[-1][0]
    assert len(rows) == k_nyq - 1, len(rows)

    # 真实读数噪声（40 nm）下同一根必须留着：判据不能把真实扫描的 Nyquist 一起掐掉
    rng = np.random.default_rng(11)
    tj = np.arange(n, dtype=np.float64) + rng.normal(0, 0.04, n)
    spanj = float(tj[-1] - tj[0])
    thj = 2 * np.pi * (1 / spanj) * (tj - tj.min())
    c, s = np.cos(k_nyq * thj), np.sin(k_nyq * thj)
    assert solvable(float(c @ c), float(s @ s), float(c @ s)), "40 nm 噪声下 Nyquist 那根本该留着"

    # 判据是**相对量**：同一串采样整体缩放（µm ↔ fs）不影响结论
    c, s = np.cos(k_nyq * th), np.sin(k_nyq * th)
    assert solvable(4 * float(c @ c), 4 * float(s @ s), 4 * float(c @ s)) == solvable(
        float(c @ c), float(s @ s), float(c @ s))
    assert SOLVABLE_KAPPA == 1e12, "这条线本身是决定（见 bands.py 那一段实测）"


def test_only_none_counts_as_all_pass():
    """**「全通」只有一个判据：没填（None）**。填了 0 / 负数 / nan 是填错了，当场拒。

    从前这里按 `> 0` 判，于是 `?lmin=0` 被静默当成「不限」—— 而 server 的
    「相位图全通必拒」判的是 `is None`，中间那格（0/负/nan）就是没人管的过道：
    一条 API 直连就能绕过守卫，让相位图按全通算。
    """
    n, span = 101, 600.0
    for bad in (0, -1.0, float("nan")):
        for args in ((bad, None), (None, bad)):
            try:
                band_lines(args[0], args[1], n, span)
            except ValueError as exc:
                assert "正数" in str(exc), exc
            else:
                raise AssertionError(f"{args} 被当成了全通（应当拒）")
    # 填得太小（商溢出成 inf）也是填错了，报的话要能看懂，不是 OverflowError
    try:
        band_lines(1e-310, None, n, span)
    except ValueError as exc:
        assert "太" in str(exc), exc
    else:
        raise AssertionError("1e-310 没有报错")
    # 只填一边时，报错话术不许漏出 None（用户没填那个框，看到 None 不知道该改哪儿）。
    # 只填 lmax → 下限落到 Nyquist 之外 → 一根不剩，此时那句话该说「波长 ≤ x µm」。
    try:
        band_lines(None, 3.0, n, span)
    except ValueError as exc:
        assert "None" not in str(exc) and "≤" in str(exc), exc
    else:
        raise AssertionError("lmax=3.0（下限在 Nyquist 之外）该是空波段")


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


def test_moment_map_equals_the_single_line_map_when_one_line():
    """**单根谱线时，某一时刻的相位图与整条那张逐像素相同** —— 这是这个功能的边界。

    一根谱线时每个像素就是 a·cos(ωt+φ)：t 只给全图共用一个常数相位 ωt_j，减掉基准就抵消了。
    所以单根波段上开「某一时刻」看不出差别（图上会明说），差别只在带内多根时才出现。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        s = _scan(6, 5, 48, 9, tmp=Path(d))
        lmin, lmax = _band_of(s["k_sig"], s["df_thz"])
        whole = phase_map(s["items"], lmin=lmin, lmax=lmax)
        for um in (0.0, s["items"][17][1], s["items"][-1][1]):
            snap = phase_map_at(s["items"], um, lmin=lmin, lmax=lmax)
            assert (snap["k_lo"], snap["k_hi"]) == (9, 9), (snap["k_lo"], snap["k_hi"])
            ref = (3, 4)
            dw = _wrap(whole["phase"] - whole["phase"][ref])
            ds = _wrap(snap["phase"] - snap["phase"][ref])
            assert np.abs(_wrap(ds - dw)).max() < 1e-6, np.abs(_wrap(ds - dw)).max()
            # 包络差一个 N：整条那张是 Σ_t 的累加，单拍那张不累加 —— 相位不受它影响，强度要看除以 N
            assert np.allclose(snap["amp"] * len(s["items"]), whole["amp"], rtol=1e-6)


def test_moment_map_equals_the_per_pixel_signal_at_that_frame():
    """**定义的自证**：某一时刻的图 = 前端 `filteredSignal` 在那一拍的 z，逐像素解出来对着比。

    z_i(t_j) = Σ_k c_k·e^(i·2πk·df·(t_j−t0))，c_k 由逐像素最小二乘给出 —— 这就是那一刻
    每个像素的带通信号复数；相位 = arg、包络 = |z|。带内三根（互相干涉）才看得出它随时刻走。
    """
    import tempfile

    rng = np.random.default_rng(11)
    n, w, h = 40, 4, 5
    um = list(np.cumsum(rng.uniform(0.05, 0.09, n)))
    t = np.array(um) * TIME_FS_PER_UM
    span = float(t.max() - t.min())
    df, df_thz = 1.0 / span, 1e3 / span
    t0 = float(t.min())
    th = 2 * np.pi * df * (t - t0)
    lines = {12: 26.0, 13: 14.0, 15: 7.0}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        images, paths = [], []
        for i in range(n):
            img = np.empty((h, w), dtype=np.uint16)
            for y in range(h):
                for x in range(w):
                    v = 480.0
                    for k, a in lines.items():
                        v += a * np.cos(k * th[i] + 0.4 * x - 0.3 * y)
                    img[y, x] = int(round(v))
            images.append(img)
            p = tmp / f"scan0002_{i:05d}.png"
            _save_png16(p, img)
            paths.append(p)
        items = [(i, float(um[i]), paths[i]) for i in range(n)]
        lmin = WAVELENGTH_UM_PER_THZ / ((max(lines) + 0.5) * df_thz)
        lmax = WAVELENGTH_UM_PER_THZ / ((min(lines) - 0.5) * df_thz)
        got = phase_map_at(items, um[23], lmin=lmin, lmax=lmax)
        assert (got["k_lo"], got["k_hi"]) == (min(lines), max(lines)), (got["k_lo"], got["k_hi"])
        assert (got["at_idx"], got["at_j"]) == (23, 23), (got["at_idx"], got["at_j"])
        series = np.array([np.asarray(im).ravel() for im in images], dtype=np.float64).T
        c = _ls_coeffs(series, th, got["k_lo"], got["k_hi"])
        want = np.zeros(c.shape[0], dtype=complex)
        for j, k in enumerate(range(got["k_lo"], got["k_hi"] + 1)):
            want += c[:, j] * np.exp(1j * 2 * np.pi * k * df * (t[23] - t0))

    gotp = got["phase"].reshape(-1)
    assert np.allclose(np.cos(gotp), np.cos(np.angle(want)), atol=2e-4)
    assert np.allclose(np.sin(gotp), np.sin(np.angle(want)), atol=2e-4)
    assert np.allclose(got["amp"].reshape(-1), np.abs(want), rtol=2e-4)


def test_moment_map_really_moves_with_the_moment_when_several_lines():
    """带内多根时，换时刻**确实换出一张不同的图** —— 而且要跟着逐像素相位一起动。

    两根幅度相当的谱线：z(t) = c1·e^(iθ1) + c2·e^(iθ2)，θ 差随 t 走 ⇒ 相位分布也跟着走。
    这条防的是"看着加了个参数、其实权重跟时刻无关"那类假实现。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        n, w, h = 48, 4, 4
        um = [i * 0.08 for i in range(n)]
        t = np.array(um) * TIME_FS_PER_UM
        span = float(t.max() - t.min())
        df, df_thz = 1.0 / span, 1e3 / span
        th = 2 * np.pi * df * (t - t.min())
        # 两根幅度**不等**：等幅时 z 的相位在整幅图上只是整体转一圈，减掉基准就看不见了 ——
        # 那一刻的相位分布要随 t 变，得让两根的相对权重随各自的相位动（这就是色散的样子）。
        lines = {10: 30.0, 11: 10.0}
        images, paths = [], []
        for i in range(n):
            img = np.empty((h, w), dtype=np.uint16)
            for y in range(h):
                for x in range(w):
                    v = 500.0
                    v += 30.0 * np.cos(10 * th[i] + 0.6 * x)
                    v += 10.0 * np.cos(11 * th[i] - 0.9 * x)
                    img[y, x] = int(round(v))
            images.append(img)
            p = tmp / f"scan0003_{i:05d}.png"
            _save_png16(p, img)
            paths.append(p)
        items = [(i, float(um[i]), paths[i]) for i in range(n)]
        lmin = WAVELENGTH_UM_PER_THZ / ((max(lines) + 0.5) * df_thz)
        lmax = WAVELENGTH_UM_PER_THZ / ((min(lines) - 0.5) * df_thz)
        a = phase_map_at(items, um[0], lmin=lmin, lmax=lmax)
        b = phase_map_at(items, um[n // 2], lmin=lmin, lmax=lmax)
        assert (a["k_lo"], a["k_hi"]) == (10, 11), (a["k_lo"], a["k_hi"])
        ref = (2, 2)
        da = _wrap(a["phase"] - a["phase"][ref])
        db = _wrap(b["phase"] - b["phase"][ref])
        # 半圈之后两根的相对权重翻过来了，相位分布必须跟着走（不是差一个常数）
        assert np.abs(_wrap(db - da)).max() > 0.3, np.abs(_wrap(db - da)).max()


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
