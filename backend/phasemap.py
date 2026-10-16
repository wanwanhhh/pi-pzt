"""数据页：整幅图的**相位差** —— 每个像素跟基准像素差多少相位。

**每个像素一个复数**：

    R_i = Σ_t u'(t)·x_i(t)        相位 = arg R_i（弧度），条纹强度 = |R_i|

**为什么是一遍扫完**：R 对样本是**线性**的（每根谱线的 A/B 本来就是从样本上解两个线性方程
解出来的），所以整幅图 = 每帧一次「权重 × 图」的累加 —— **像素个数不进代价**，代价全在解 PNG 上。
换基准像素因此**不用重算**：Δφ = arg R_i − arg R_ref，减一个常数而已（在界面上做）。
换波段要重扫一遍（u' 变了）。

**口径**（与前端那一个像素的滤波曲线同源，只是把它一次用在所有像素上）：

- 时间列、非均匀最小二乘、去均值、波段 → 谱线，与 `frontend/app.js` 的 `pixelSpectrum` /
  `filteredSignal` / `filtKeepRange` **同式**。这条口径的家在那边（那是它出生与使用的地方），
  这里只是照搬一次 —— **改那边的式子，这里必须跟着改**：跨语言的同一份知识，
  两边各有一个测试钉住同一组期望数（`backend/tests/test_phase_map.py` 里的自证那一条）。
- 载波 = **留下那一段的正中间那一根**（k_c = (k_lo+k_hi)/2）。它只是**全图共用的一个参考频率**：
  像素之间的差与它同源，所以**差值与它无关**（同一串 W_k）；取在带内是为了
  W_k = Σ_t e^(i2π(k−k_c)·df·Δt) 还在带内成形 —— 取到带外时各根几乎互相抵消，出来的是噪声。
  **与滤波曲线里那种「每像素各自的带内功率重心」不是一回事**：那个是减掉线性相位用的（显示口径），
  这个是全图比较用的公共基准；差别只在带宽很宽、各根相位差异大时才看得见。
- **相位不锚定**：滤波曲线把首点锚成 0、把线性相位减掉（那是为了看残差）——
  被锚掉的那个常数**正好就是这里要量的东西**。所以不能拿那条曲线逐点相减再平均。
- **去均值**照做（前端也去）：不去的话，cos/sin 在非均匀网格上不正交，直流量会按每个像素
  **自己的亮度**漏进系数里 —— 那就是一种凭空的像素间相位差。算式上等价于把权重里减掉
  U/N（见 weight_vector），所以不必另攒一个均值列。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ


def band_lines(lmin: Optional[float], lmax: Optional[float], n: int, span_fs: float):
    """两个波长框（µm）→ 留下哪几根谱线。返回 (k_lo, k_hi, k_max, df_thz)。

    与前端 `filtKeepRange` 同一条规矩：λ = c/ν，上限卡**名义 Nyquist**
    （kMax = floor((n-1)/2)，与频谱那张图同一个上限）；只填一边，另一边就是不限；
    两个框都空 = 全通。**相位图不收全通**（没有载波可比）—— 由调用方判，这里照实返回 1..kMax。
    """
    k_max = (n - 1) // 2
    if k_max < 1:
        raise ValueError(f"只有 {n} 个点，做不了谱（至少要 3 个）")
    if not (span_fs > 0):
        raise ValueError("所有点的读出位置都在同一处（跨度 0），做不了谱")
    df_thz = 1e3 / span_fs
    k_lo, k_hi = 1, k_max
    if lmin is not None and lmin > 0:
        k_hi = min(k_hi, math.floor((WAVELENGTH_UM_PER_THZ / lmin) / df_thz))
    if lmax is not None and lmax > 0:
        k_lo = max(k_lo, math.ceil((WAVELENGTH_UM_PER_THZ / lmax) / df_thz))
    if k_hi < k_lo:
        raise ValueError(
            f"{lmin} ~ {lmax} µm 这一段里一根谱线都没有（谱的格是 {df_thz:.4g} THz）"
        )
    return int(k_lo), int(k_hi), int(k_max), df_thz


def weight_vector(t_fs, k_lo: int, k_hi: int, k_c: float, span_fs: float):
    """一场扫描一条：把「每个点的样本」直接变成复数的权重 u'(t)（已含去均值）。

    u'(t) = Σ_k W_k·g_k(t) − (Σ_t u(t))/N

    - g_k(t)：第 k 根谱线「由样本解出 c_k = A_k − iB_k」的那一行（2×2 那套，与 app.js 同式）；
    - W_k = Σ_t e^(i2π(k−k_c)·df·(t−t0))：与公共载波相关的那一串（全是 W 的模在带内成形）。
    """
    import numpy as np

    t = np.asarray(t_fs, dtype=np.float64)
    n = t.size
    t0 = float(t.min())
    df = 1.0 / float(span_fs)
    th = 2.0 * math.pi * df * (t - t0)          # 第 1 格的相位（弧度），k 倍就是第 k 格
    u = np.zeros(n, dtype=np.complex128)
    for k in range(k_lo, k_hi + 1):
        c = np.cos(k * th)
        s = np.sin(k * th)
        scc = float(c @ c)
        sss = float(s @ s)
        scs = float(c @ s)
        det = scc * sss - scs * scs
        if det <= 0:
            continue
        # x ↦ c_k 的那一行（去均值在下面整条一起做）
        g = ((sss * c - scs * s) - 1j * (scc * s - scs * c)) / det
        w = np.exp(1j * (k - k_c) * th).sum()
        u += w * g
    return u - u.sum() / n, df


def phase_map(items, crop=None, lmin=None, lmax=None, workers: int = 8,
              progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """扫一遍这一条扫描的所有帧 → 每个像素一个复数（相位 + 条纹强度）。

    items 是 [(序号, 读出位置 µm, PNG 路径 | None), ...]（与 png_pixel_series 同一形状）。
    **缺一个点整条不算**（没图 / 没记读数）：谱里没有"断开"这回事，少一个点就是另一条采样序列 ——
    与频谱那张图同一条规矩，当场说清是第几个点。

    crop 给了就是 (x0, y0, w, h)：帧已经被裁成**原始坐标**里的那一块。返回的 rect 是**原始坐标**，
    数组是**盘上那张图**的形状 —— 坐标口径不因裁剪改变（与取数、剖面同一套）。

    返回 {phase, amp（float32 二维，h×w）, rect, w, h, n, k_lo, k_hi, k_c, k_max,
          df_thz, f_c_thz, lam_c_um, band}。
    """
    import numpy as np
    from .thorlabs_ccd import _png_gray

    n = len(items)
    if n < 3:
        raise ValueError(f"只有 {n} 个点，做不了相位图（至少要 3 个）")
    for i, (idx, um, path) in enumerate(items):
        where = f"第 {i + 1} 个点（idx {idx}）"
        if um is None:
            raise ValueError(f"{where}没记下读出位置 —— 整条做不了：缺一个点就是另一条采样序列")
        if path is None:
            raise ValueError(f"{where}没采到图 —— 整条做不了：缺一个点就是另一条采样序列")

    t_fs = [p * TIME_FS_PER_UM for (_, p, _) in items]
    span = max(t_fs) - min(t_fs)
    k_lo, k_hi, k_max, df_thz = band_lines(lmin, lmax, n, span)
    k_c = (k_lo + k_hi) / 2.0
    u, _ = weight_vector(t_fs, k_lo, k_hi, k_c, span)

    x0, y0 = (crop[0], crop[1]) if crop else (0, 0)

    def frame(it):
        """读一帧并把它乘进增量里；形状与裁剪记录对不上当场报错（不猜、不缩）。"""
        i, (idx, _, path) = it
        arr = _png_gray(path)
        if arr is None:
            raise ValueError(f"第 {i + 1} 个点（idx {idx}）的图读不出来：{path}")
        if arr.ndim != 2:
            raise ValueError(f"第 {i + 1} 个点（idx {idx}）不是灰度图")
        if crop and (arr.shape[1], arr.shape[0]) != (int(crop[2]), int(crop[3])):
            raise ValueError(
                f"第 {i + 1} 个点的图是 {arr.shape[1]}×{arr.shape[0]}，裁剪记录写着 "
                f"{crop[2]}×{crop[3]} —— 库和文件对不上，先查清"
            )
        return i, arr.shape, arr

    done = 0
    shape = None
    total_re = total_im = None
    jobs = list(enumerate(items))
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for i, sh, arr in pool.map(frame, jobs):
            if shape is None:
                shape = sh
                total_re = np.zeros(sh, dtype=np.float64)
                total_im = np.zeros(sh, dtype=np.float64)
            elif sh != shape:
                raise ValueError(
                    f"第 {i + 1} 个点的图是 {sh[1]}×{sh[0]}，"
                    f"别的点是 {shape[1]}×{shape[0]} —— 尺寸不齐，整条做不了相位图"
                )
            f = arr.astype(np.float64)
            total_re += u[i].real * f
            total_im += u[i].imag * f
            done += 1
            if progress:
                progress(done, n)

    re, im = total_re, total_im
    phase = np.arctan2(im, re).astype(np.float32)
    amp = np.hypot(re, im).astype(np.float32)
    h, w = shape
    f_c_thz = k_c * df_thz
    return {
        "phase": phase, "amp": amp,
        "w": int(w), "h": int(h),
        "rect": [int(x0), int(y0), int(w), int(h)] if crop else None,
        "n": int(n), "k_lo": k_lo, "k_hi": k_hi, "k_c": k_c, "k_max": int(k_max),
        "df_thz": df_thz, "f_c_thz": f_c_thz,
        "lam_c_um": (WAVELENGTH_UM_PER_THZ / f_c_thz) if f_c_thz > 0 else None,
        "band": [lmin, lmax],
    }
