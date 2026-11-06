"""数据页：整幅图的**相位差** —— 每个像素跟基准像素差多少相位。

**每个像素一个复数**：

    R_i = Σ_t u'(t)·x_i(t)        相位 = arg R_i（弧度），条纹强度 = |R_i|

**两个口径共用同一段累加**（差别只在权重 u 怎么取，见 `moment_weights`）：

- **整条**（默认）：u' = Σ_k W_k·g_k(t)，W_k = Σ_t e^(i2π(k−k_c)·df·Δt) —— 一节扫描出一个数；
- **某一时刻**（`moment_weights`）：u'(t) = Σ_k g_k(t)·e^(i2πk·df·(t_j−t0)) —— 就是前端的
  `filteredSignal` 在 t_j 那一拍的 z（同一个式子），只是用在所有像素上。
  单根谱线时它与整条那张**逐像素完全相同**（ωt_j 是全图共用常数，减基准就抵掉了）。

**为什么是一遍扫完**：R 对样本是**线性**的（每根谱线的 A/B 本来就是从样本上解两个线性方程
解出来的），所以整幅图 = 每帧一次「权重 × 图」的累加 —— **像素个数不进代价**，代价全在解 PNG 上。
换基准像素因此**不用重算**：Δφ = arg R_i − arg R_ref，减一个常数而已（在界面上做）。
换波段要重扫一遍（u' 变了）；换时刻也要重扫一遍（u' 也变了）—— 两者都只是换一条权重。

**口径**（与前端那一个像素的滤波曲线同源，只是把它一次用在所有像素上）：

- 时间列、非均匀最小二乘、去均值、波段 → 谱线，与 `frontend/app.js` 的 `pixelSpectrum` /
  `filteredSignal` / `filtKeepRange` **同式**。这条口径的家在那边（那是它出生与使用的地方），
  这里只是照搬一次 —— **改那边的式子，这里必须跟着改**：跨语言的同一份知识，
  两边各有一个测试钉住同一组期望数（`backend/tests/test_phase_map.py` 里的自证那一条）。
- 载波 = **留下那一段的正中间那一根**（k_c = (k_lo+k_hi)/2，两个端点都是**实际留下**的那几根，
  解不出的已经丢掉了 —— 见 `_carrier_of`）。它只是**全图共用的一个参考频率**：
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

from .bands import band_lines, solvable   # 波段 → 谱线、「解不解得出」：那条规矩的家在 bands.py
from .config import TIME_FS_PER_UM, WAVELENGTH_UM_PER_THZ


def _grid(t_fs, k_lo: int, k_hi: int, span_fs: float):
    """公共的一段：时间列 → (t, t0, df, th, [(k, g_k), ...])。

    g_k 是第 k 根谱线「由样本解出 c_k = A_k − iB_k」的那一行（2×2 那套，与 app.js 同式）；
    th 是第 1 格的相位（弧度，k 倍就是第 k 格）。解不出（2×2 退化）的那根直接跳过 ——
    与前端不把它当系数同一个道理。

    **一根都不剩就当场拒**：两个口径（整条 / 某一时刻）与两条权重函数都从这里拿谱线，
    所以这条判断只在这里判一次。不拒的话权重是**恒等于 0**的一条（去均值之后还是 0），
    出来的是一整幅"相位 0、强度 0"的图 —— 看着像算完了，其实一根谱线都没用上。
    话术与强度对齐（intensity_align._prep）同一句：同一件事，两处不该有两套说法。
    """
    import numpy as np

    t = np.asarray(t_fs, dtype=np.float64)
    t0 = float(t.min())
    df = 1.0 / float(span_fs)
    th = 2.0 * math.pi * df * (t - t0)
    rows = []
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
        rows.append((k, ((sss * c - scs * s) - 1j * (scc * s - scs * c)) / det))
    if not rows:
        raise ValueError("这一段波长里没有一根能解出来的谱线")
    return t, t0, df, th, rows


def _carrier_of(ks) -> float:
    """公共载波：**实际留下**的那几根的中点数（ks 已排好序，解不出的不在里面）。

    整条那张的权重 W_k = Σ_t e^(i2π(k−k_c)·df·Δt) 要用它；结果里报的 k_c 也是它 ——
    两处同一个数。按波段两端的原值取中点会把丢掉的那几根也算进来（201 点、990~1000 那段
    实际留下 990~999，中点是 994.5，不是 995.0）。
    """
    return (ks[0] + ks[-1]) / 2.0


def _demean(u, n: int):
    """去均值照做（前端也去）：不去的话直流量会按每个像素自己的亮度漏进系数里。"""
    return u - u.sum() / n


def weight_vector(t_fs, k_lo: int, k_hi: int, span_fs: float):
    """**整条**一场扫描一条：把「每个点的样本」直接变成复数的权重 u'(t)（已含去均值）。

    u'(t) = Σ_k W_k·g_k(t) − (Σ_t u(t))/N

    W_k = Σ_t e^(i2π(k−k_c)·df·(t−t0))：与公共载波相关的那一串（全是 W 的模在带内成形）。
    **k_c 不传进来**：它就是「留下那几根的中点」（`_carrier_of`），在这里由**实际参与**的
    那几根定 —— 结果里报的 k_c 也是同一个数，两处不会各说一套。

    返回 (u', ks, k_c)：ks 是实际参与的那几根（解不出的已经丢掉），k_c 是它们的中点。
    """
    import numpy as np

    t, t0, df, th, rows = _grid(t_fs, k_lo, k_hi, span_fs)
    ks = [k for k, _ in rows]
    k_c = _carrier_of(ks)
    u = np.zeros(t.size, dtype=np.complex128)
    for k, g in rows:
        u += np.exp(1j * (k - k_c) * th).sum() * g
    return _demean(u, t.size), ks, k_c


def moment_weight_vector(t_fs, k_lo: int, k_hi: int, span_fs: float, at_fs: float):
    """**某一时刻**的权重 u'(t)：让 Σ_t u'(t)·x(t) = z(t_j)（前端 `filteredSignal` 那一拍的复数）。

        z(t_j) = Σ_k c_k·e^(i·2πk·df·(t_j−t0))，c_k 由 g_k 给出 ⇒ u'(t) = Σ_k g_k(t)·e^(iθ_k(t_j))

    **没有公共载波**：时刻本身就定死了相位基准（θ_k(t_j) 是绝对的那一拍），所以不取 k_c。
    单根谱线时它给出来的相位图与 `weight_vector` 那张**逐像素相同**（ωt_j 全图共用、减基准就抵消）；
    带内多根时相位才随 t_j 走 —— 那时它画的才是"这一刻的相位分布"。

    返回 (u', ks)，ks 的含义与 `weight_vector` 那一份相同（实际参与的那几根）。
    """
    import numpy as np

    t, t0, df, th, rows = _grid(t_fs, k_lo, k_hi, span_fs)
    tj = float(at_fs) - t0
    u = np.zeros(t.size, dtype=np.complex128)
    for k, g in rows:
        u += g * np.exp(1j * 2.0 * math.pi * k * df * tj)
    return _demean(u, t.size), [k for k, _ in rows]


def _nearest_frame(t_fs, at_fs: float) -> int:
    """离这个时刻最近的那一帧（只是给界面报"你选的是第几个点"，权重仍按给的那个时刻算）。"""
    t = [abs(float(x) - float(at_fs)) for x in t_fs]
    return t.index(min(t))


def _accumulate(items, crop, u, workers: int, progress):
    """一遍读盘：每帧一次「权重 × 图」的累加。**像素个数不进代价**，代价全在解 PNG 上。

    u 与像素无关（`weight_vector` / `moment_weight_vector` 各给一条），所以两个口径共用这一段。
    读盘走 `frames.read_frames`（三条不变式与有界窗口都在那里）；这里只剩累加那两行。
    返回 (shape, re, im)。

    **不改成批矩阵乘**：实测相位图每帧只做 2 次乘加（全幅 2000 帧里主线程数学约 1.4 s、
    解码约 3.8 s），改成批反而把解码的并行度让给 GEMM，5.24 s → 5.86 s。只借它的有界窗口省内存。
    """
    import numpy as np

    from . import frames

    shape = None
    total_re = total_im = None
    for i, sh, f in frames.read_frames(items, crop=crop, workers=workers,
                                       dtype=np.float64, progress=progress):
        if shape is None:
            shape = sh
            total_re = np.zeros(sh, dtype=np.float64)
            total_im = np.zeros(sh, dtype=np.float64)
        total_re += u[i].real * f
        total_im += u[i].imag * f
    return shape, total_re, total_im


def _check_items(items) -> None:
    """**缺一个点整条不算**（没图 / 没记读数）：判断在 `frames.usable`，这里只报"做不了相位图"。"""
    from . import frames

    frames.usable(items, "相位图")


def _result(items, crop, shape, re, im, lmin, lmax, ks, k_c, k_max, df_thz, **extra):
    """两个口径共用的一份结果：相位、条纹强度、形状、波段元数据。

    `ks` 是**实际参与**的那几根（解不出的已经丢掉），`k_c` 是它们的中点（`_carrier_of`）——
    报的是这张图真正用上的那一段，不是波段框两端的原值：按原值报会把丢掉的根也算进去
    （界面据此写「共 N 根」就会多报一根，载波也会偏半格）。

    `k_c` 给 None 就是「这一份没有公共载波」（某一时刻那张：相位基准就是那一拍本身）：
    载波那三格（k_c / f_c_thz / lam_c_um）一起给 None —— 有数就得有含义。
    """
    import numpy as np

    x0, y0 = (crop[0], crop[1]) if crop else (0, 0)
    h, w = shape
    phase = np.arctan2(im, re).astype(np.float32)
    amp = np.hypot(re, im).astype(np.float32)
    f_c_thz = None if k_c is None else k_c * df_thz
    out = {
        "phase": phase, "amp": amp,
        "w": int(w), "h": int(h),
        "rect": [int(x0), int(y0), int(w), int(h)] if crop else None,
        "n": len(items), "k_lo": int(ks[0]), "k_hi": int(ks[-1]), "k_c": k_c,
        "k_max": int(k_max), "df_thz": df_thz, "f_c_thz": f_c_thz,
        "lam_c_um": (WAVELENGTH_UM_PER_THZ / f_c_thz) if f_c_thz else None,
        "band": [lmin, lmax],
    }
    out.update(extra)
    return out


def phase_map(items, crop=None, lmin=None, lmax=None, workers: int = 8,
              progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """**整条**扫一遍这一条扫描的所有帧 → 每个像素一个复数（相位 + 条纹强度）。

    items 是 [(序号, 读出位置 µm, PNG 路径 | None), ...]（与 png_pixel_series 同一形状）。
    crop 给了就是 (x0, y0, w, h)：帧已经被裁成**原始坐标**里的那一块。返回的 rect 是**原始坐标**，
    数组是**盘上那张图**的形状 —— 坐标口径不因裁剪改变（与取数、剖面同一套）。

    返回 {phase, amp（float32 二维，h×w）, rect, w, h, n, k_lo, k_hi, k_c, k_max,
          df_thz, f_c_thz, lam_c_um, band}。k_lo / k_hi / k_c 都是**实际留下**的那几根
    （解不出的已经丢掉）。这一段一根都解不出 → ValueError，不交一张全零的图。
    """
    _check_items(items)
    t_fs = [p * TIME_FS_PER_UM for (_, p, _) in items]
    span = max(t_fs) - min(t_fs)
    k_lo, k_hi, k_max, df_thz = band_lines(lmin, lmax, len(items), span)
    u, ks, k_c = weight_vector(t_fs, k_lo, k_hi, span)
    shape, re, im = _accumulate(items, crop, u, workers, progress)
    return _result(items, crop, shape, re, im, lmin, lmax, ks, k_c, k_max, df_thz)


def phase_map_at(items, at_um: float, crop=None, lmin=None, lmax=None, workers: int = 8,
                 progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """**某一时刻**（按读出位置 µm 指定）每个像素的相位与包络 —— 与 `phase_map` 同一段读盘。

    单根谱线时它与 `phase_map` **逐像素相同**（那个时刻的载波相位是全图共用的，减基准就抵掉）；
    带内多根时这里画的才是"这一刻的相位分布"，随 at_um 走。

    返回在 `phase_map` 那一份之上多三个：`at_um`（你要的那一刻）、`at_idx`（离它最近的点序号）、
    `at_um_actual`（那一帧真实记下的读出位置 —— 界面要照实说"落在哪一拍上"）。
    **载波那三格（k_c / f_c_thz / lam_c_um）是 None**：这一刻没有公共载波可比，
    相位基准就是那一拍本身（界面上那一格写「—（某一时刻不取载波）」）。
    """
    _check_items(items)
    t_fs = [p * TIME_FS_PER_UM for (_, p, _) in items]
    span = max(t_fs) - min(t_fs)
    k_lo, k_hi, k_max, df_thz = band_lines(lmin, lmax, len(items), span)
    j = _nearest_frame(t_fs, float(at_um) * TIME_FS_PER_UM)
    u, ks = moment_weight_vector(t_fs, k_lo, k_hi, span, float(at_um) * TIME_FS_PER_UM)
    shape, re, im = _accumulate(items, crop, u, workers, progress)
    # 这一刻**没有公共载波**（相位基准就是那一拍本身）：k_c 给 None，载波那三格一起空着
    return _result(items, crop, shape, re, im, lmin, lmax, ks, None, k_max, df_thz,
                   at_um=float(at_um), at_idx=items[j][0], at_j=int(j),
                   at_um_actual=items[j][1])
