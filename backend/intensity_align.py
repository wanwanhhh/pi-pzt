"""数据页：整幅图的**强度对齐** —— 每个像素的带内交流能量，除以它自己直流的 (2n−1)/(2n) 次方。

    S_xy = Σ_t b_xy(t)² / d_xy^((2n−1)/(2n))

- **b** = 〈滤波曲线〉所选那段波长内的重建信号（带通结果，**本来就不含直流**）；
  它的平方和就是「带内交流能量」。
- **d** = 该像素这条扫描的**直流**：所有帧的均值。每个像素除自己的 —— 这就是「对齐」：
  亮暗、照明不均、反射率差异都被自己的直流除掉，图上比的是**条纹的相对深度**。
- **n** 由用户设（默认 3）：指数 1 − 1/(2n) 是归一化程度，n 越大亮度差压得越狠
  （n→∞ 时指数 2 = 完全归一化，只剩对比度²；n=1/2 时指数 0 = 不归一化）。

**为什么一遍读盘只产出两张与 n 无关的图**：b 对样本是线性的，除以 d^α 只是逐像素常数缩放，
所以「除以 d^α」与「带通」可以交换顺序 —— 先算 Σb²（energy）与 d（dc），n 只决定最后那个
逐像素因子。**改 n 只重画，不重读盘、不重算带通**。

**为什么 energy 要 2K 张累加图（相位图只要 2 张）**：b(t) = Σ_k (A_k·cosθ_k + B_k·sinθ_k)，
所以 Σ_t b² 是系数向量 a = [A_1..A_K, B_1..B_K] 的**二次型** aᵀGa，G = ΦᵀΦ 只由时间列与
波段决定、与像素无关 —— 要它就得逐像素的 2K 个系数。G **不是对角阵**：采样非均匀，
cos/sin 在这 N 个点上并不正交，交叉项丢掉就是编数。

**口径与相位图同源**：时间列、非均匀最小二乘、去均值、波段 → 谱线，与 frontend/app.js 的
pixelSpectrum / filteredSignal / filtKeepRange 同式（那条口径的家在那边，这里照搬一次）；
波段 → 谱线直接用 bands.band_lines（那条规矩的家在 bands.py：λ = c/ν、上限卡名义 Nyquist）。
**全通也能算**（这是它与相位图的重要区别）：这里不需要载波，全通就是「所有谱线的交流能量」。
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from .bands import band_lines, solvable
from .config import ALIGN_MAX_BYTES, TIME_FS_PER_UM


def _prep(t_fs, k_lo: int, k_hi: int, span: float):
    """解系数的那几行（A_k、B_k 各一行，**已去均值**）＋ 建 G 的基函数。

    A_k = Σ_t I(t)·ga_k(t)、B_k = Σ_t I(t)·gb_k(t)（2×2 正规方程，与前端同式）；
    去均值是把每行自己的和减掉 —— 与「先对样本去均值再解」等价（见 phasemap.weight_vector）。
    返回 (ks, rows_a[K,N], rows_b[K,N], phi[N,2K])；phi 的列顺序 = [cos…, sin…]，
    与 a = [A_1..A_K, B_1..B_K] 一一对应。
    """
    import numpy as np

    t = np.asarray(t_fs, dtype=np.float64)
    t0 = float(t.min())
    df = 1.0 / float(span)
    th = 2.0 * math.pi * df * (t - t0)
    n = t.size
    ks, rows_a, rows_b = [], [], []
    for k in range(k_lo, k_hi + 1):
        c = np.cos(k * th)
        s = np.sin(k * th)
        scc = float(c @ c)
        sss = float(s @ s)
        scs = float(c @ s)
        # 解不解得出只有 bands.solvable 一处说了算（条件数，不是行列式的符号）
        if not solvable(scc, sss, scs):
            continue
        det = scc * sss - scs * scs
        ga = (sss * c - scs * s) / det
        gb = (scc * s - scs * c) / det
        ks.append(k)
        rows_a.append(ga - ga.sum() / n)
        rows_b.append(gb - gb.sum() / n)
    if not ks:
        raise ValueError("这一段波长里没有一根能解出来的谱线")
    # G 的基函数就是**重建时用的那一对 cos/sin**（不去中心）：系数行虽然去了均值（等价于对样本
    # 去均值，与前端同式），但 b(t) = Σ(A_k·cosθ_k + B_k·sinθ_k) 是拿未去中心的 cos/sin 拼回来的
    # —— 两处必须是同一对，不然 energy 会系统性偏一点（非均匀采样时看得见，不报错）。
    phi = np.stack([np.cos(k * th) for k in ks] + [np.sin(k * th) for k in ks], axis=1)
    return ks, np.stack(rows_a), np.stack(rows_b), phi


def _need_bytes(k: int, w: int, h: int) -> int:
    """2K 张系数图 + 1 张直流图，全 float64。"""
    return (2 * k + 1) * 8 * int(w) * int(h)


def _check_memory(k: int, w: int, h: int) -> None:
    """累加内存超上限就当场拒（两条规矩只写这一处：上限与那句话）。

    `w/h` 有两个来源 —— 裁剪记录（读盘前就知道）与第一帧的真实幅面（读出来才知道）。
    两处都调这一个函数，报的话才不会是两套。
    """
    need = _need_bytes(k, w, h)
    if need > ALIGN_MAX_BYTES:
        raise ValueError(
            f"这一段波长有 {k} 根谱线，这幅面（{w}×{h}）要 "
            f"{need / 2**20:.0f} MB 累加内存，超过上限 {ALIGN_MAX_BYTES / 2**20:.0f} MB —— "
            f"把波段收窄，或先把这条扫描裁小"
        )


def _check_items(items) -> None:
    """**缺一个点整条不算**（没图 / 没记读数）：判断在 `frames.usable`，这里只报"做不了强度对齐"。"""
    from . import frames

    frames.usable(items, "强度对齐")


# 二次型分块时**一块**的预算：块宽 = 它 ÷ (2K×8 字节)。峰值是它的两倍（见 align_maps 那一段），
# 所以这一块 128 MiB 对应峰值约 256 MiB。实测全幅（K=24，npix 1.56 M）上 128 与 256 两种预算
# 耗时与峰值 RSS 都一样（11.5 s / 2.4 GiB，差在噪声里）—— 那就取小的那个，把峰值压在 256 MiB。
QUADRATIC_CHUNK_BYTES = 128 * 1024 * 1024


def _batch_size(k: int, npix: int) -> int:
    """一批堆多少帧。

    **至少 32**（BLAS 才吃满：实测 8 → 13.9 ms/帧、16 → 6.6、32 → 3.8、64 → 3.0），
    上限 128；中间按「这批缓冲不超过 WINDOW_BYTES」收 —— 幅面大时批变小。

    **下限就是 32，不会为了省内存再往下退**：真到「32 帧都放不下」的幅面
    （npix > WINDOW_BYTES/(32×8) ≈ 200 万像素），兜底让位给 `_check_memory`
    —— 它按 2K 张累加图判，比这一批缓冲大得多，先拒的是它。
    """
    from .frames import WINDOW_BYTES

    per = max(1, npix) * 8
    return max(1, min(128, max(32, WINDOW_BYTES // per)))


def _accumulate(items, crop, rows_a, rows_b, workers: int, progress):
    """一遍读盘：每帧累加 2K 个系数图 + 一张直流图。

    返回 (shape, sA[K,h,w], sB[K,h,w], sdc[h,w])。像素个数**只进加法**，不进任何解方程。

    **一批帧一次矩阵乘**：把 B 帧堆成 `(B, npix)`，一次 `(2K,B)@(B,npix)` 就把 2K 张系数图
    这一批的贡献全算完。逐帧对 K 根各做一次 `+=` 是 O(K) 次 Python 循环，这里是 O(1) 次 BLAS 调用 ——
    实测 K=36 时 12.8 s → 3.4 s（500×400）、114 s → 16 s（全幅），K 越大赚得越多。
    数学没变：与逐帧写法相对偏差 1e-14（同一批系数、同一组帧，只是加法顺序换了）。

    读盘仍走 `frames.read_frames`（三条不变式 + 有界窗口都在那里）：这里再按批大小把交回来的帧
    攒成一批，攒满就乘一次。
    """
    import numpy as np

    from . import frames

    n = len(items)
    k = rows_a.shape[0]
    shape = None
    s_a = s_b = s_dc = None
    batch, idxs = [], []

    def flush():
        """把攒下的这一批乘进累加图（最后不满一批的也走这里）。"""
        nonlocal s_a, s_b, s_dc
        if not batch:
            return
        ch = np.stack(batch)                      # (B, npix)
        cols = np.asarray(idxs)
        s_a += (rows_a[:, cols] @ ch).reshape(s_a.shape)
        s_b += (rows_b[:, cols] @ ch).reshape(s_b.shape)
        s_dc += ch.sum(axis=0).reshape(s_dc.shape)
        batch.clear()
        idxs.clear()

    for i, sh, f in frames.read_frames(items, crop=crop, workers=workers,
                                       dtype=np.float64, progress=progress):
        if shape is None:
            # **内存判定必须在分配之前**：没裁剪时幅面由第一帧说了算，这里是唯一知道它的时刻。
            # 放到 align_maps 里"读完再判"就是先分配 2K 张图、再拒 —— 全通全幅会先要 23 GiB。
            _check_memory(k, sh[1], sh[0])
            shape = sh
            s_a = np.zeros((k,) + sh, dtype=np.float64)
            s_b = np.zeros_like(s_a)
            s_dc = np.zeros(sh, dtype=np.float64)
            bsize = _batch_size(k, sh[0] * sh[1])
        batch.append(f.ravel())
        idxs.append(i)
        if len(batch) >= bsize:
            flush()
    flush()
    s_dc /= n
    return shape, s_a, s_b, s_dc


def align_maps(items, crop=None, lmin=None, lmax=None, workers: int = 8,
               progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """这条扫描的每一帧读一遍 → 两张与 n 无关的图：带内交流能量 Σb² 与直流 d。

    items 是 [(序号, 读出位置 µm, PNG 路径 | None), ...]（与 png_pixel_series 同一形状）。
    crop 给了就是 (x0, y0, w, h)：帧已经被裁成**原始坐标**里的那一块。返回的 rect 是**原始坐标**，
    数组是**盘上那张图**的形状 —— 坐标口径不因裁剪改变（与取数、剖面、相位图同一套）。

    返回 {energy, dc（float64 二维，h×w）, rect, w, h, n, k_lo, k_hi, k_max, df_thz, band}。
    """
    import numpy as np

    _check_items(items)
    t_fs = [p * TIME_FS_PER_UM for (_, p, _) in items]
    span = max(t_fs) - min(t_fs)
    k_lo, k_hi, k_max, df_thz = band_lines(lmin, lmax, len(items), span)
    ks, rows_a, rows_b, phi = _prep(t_fs, k_lo, k_hi, span)

    # 内存先算清：超了当场拒，别跑到一半爆掉。裁过就**读盘前**用裁剪幅面判（这时就知道），
    # 没裁过由 _accumulate 在第一帧读出来、分配之前判 —— 两处都调同一个 _check_memory。
    if crop:
        _check_memory(len(ks), crop[2], crop[3])

    shape, s_a, s_b, s_dc = _accumulate(items, crop, rows_a, rows_b, workers, progress)
    h, w = shape

    # 逐像素的二次型 aᵀGa：G 只由时间列与波段决定，与像素无关。
    # **按像素列分块算**：不分块要造一个 (npix, 2K) 的临时数组（全幅 K=90 时 7 GB）。
    # 分块后同时活着的是 sub（块宽×2K）与 sub@g_mat（又一份），**峰值 ≈ 2×块宽**。
    # 二次型只能在**最后**算一次 —— 它不能像系数那样跨批累加（那是 Σ(Σb)²，不是 Σb²）。
    npix = h * w
    g_mat = phi.T @ phi
    k2 = 2 * len(ks)
    energy = np.empty(npix, dtype=np.float64)
    col = max(1, min(npix, QUADRATIC_CHUNK_BYTES // max(1, k2 * 8)))
    for c0 in range(0, npix, col):
        c1 = min(npix, c0 + col)
        sub = np.concatenate([s_a.reshape(len(ks), npix)[:, c0:c1],
                              s_b.reshape(len(ks), npix)[:, c0:c1]], axis=0).T
        # 逐行点积走 einsum：与 (…)*sub 再 sum(axis=1) 逐位相同的数，实测快 20~25%
        # （全幅 K=24：0.17 s → 0.13 s；总耗时不变，这段本来就不是瓶颈）
        energy[c0:c1] = np.einsum("ij,ij->i", sub @ g_mat, sub)
    energy = energy.reshape(h, w)

    x0, y0 = (crop[0], crop[1]) if crop else (0, 0)
    return {
        "energy": energy, "dc": s_dc,
        "w": int(w), "h": int(h),
        "rect": [int(x0), int(y0), int(w), int(h)] if crop else None,
        "n": len(items), "k_lo": int(ks[0]), "k_hi": int(ks[-1]), "k_max": int(k_max),
        "df_thz": df_thz, "band": [lmin, lmax],
    }
