"""**这次用哪几根谱线**：两个波长框（µm）落在谱的哪几根上，以及其中哪一根在这串采样上解得出。

这条规矩既不属于相位图、也不属于强度对齐 —— 它属于"波段"这件事本身，所以住在自己家里。
两个整幅图模块与 server 都从这里拿（从前它住在 phasemap，intensity_align 只能转手引用，
还得写一句注释解释那个不属于自己的依赖，是补丁的形状）。

规矩（与前端 `filtKeepRange` 同一条）：
- λ = c/ν，**短波那一头是高频**：所以「波长最小」管频率上限、「波长最大」管下限；
- 上限卡**名义 Nyquist**（k_max = floor((n-1)/2)，与频谱那张图同一个上限）；
- 只填一边，另一边就是不限；两个框都空 = 全通（1..k_max）。
  全通收不收由调用方判：相位图不收（没有载波可比），强度对齐照算。

`solvable` 管的是另一半：区间里的某一根，cos/sin 这两族基函数在这串采样上**几乎线性相关**
时解不出系数（2×2 正规方程的行列式很小）。那一条判据只有这里一处说了算。
"""
from __future__ import annotations

import math
from typing import Optional

from .config import WAVELENGTH_UM_PER_THZ

# 一根谱线「解得出」的判据：2×2 正规方程的两个特征值之比不能小于它。
#
# 为什么不是 `det > 0`：行列式是**两个量相乘**（scc·sss − scs²），量级随采样长度跑，
# 挡不住"符号对、数值烂"那一类。实测等间隔奇数点扫描的 Nyquist 那根：
# n=2001 时 det ≈ 2.4e-19 > 0，两个特征值之比却只有 6e-26 —— 系数被放大到 1e11，
# 相位图两种等价算法差 3.1 rad，强度对齐的 Σb² 靠二次型才没露馅。
#
# 为什么是 1e12：真实读数噪声下解不出那一类离这条线还有很远，能解的那一类也离得很远。
# 实测（本机库里的 49 条扫描、5482 根）：最大 κ = 3.2e7，出现在 n=3 的老扫描上；
# 2000 点等间隔加 40 nm 读数噪声的 Nyquist 那根 κ ≈ 6e1；
# 而读数抖到 1e-6 µm（真实噪声的千分之一）时才升到 1e11、抖到 1e-9 µm 就是 1e25。
# 所以这条线落在"真实采样永远够不着、退化采样一碰就过"之间，两个数量级余量。
SOLVABLE_KAPPA = 1e12


def solvable(scc: float, sss: float, scs: float) -> bool:
    """这个 2×2 正规方程解不解得出 —— 三处解系数的地方（相位图、强度对齐、前端谱）共用这一条。

    参数就是 Gram 矩阵那三个数：`[[scc, scs], [scs, sss]]`（调用方本来就算出来了，不必再算一遍
    cos/sin）。判据是它的条件数：**相对量**，采样整体缩放（µm ↔ fs、步距换一档）不影响它 ——
    这正是 `det > 0` 缺的那一维。
    """
    mid = (scc + sss) / 2.0
    disc = math.hypot((scc - sss) / 2.0, scs)
    lam_max = mid + disc
    lam_min = mid - disc
    if not (lam_max > 0) or lam_min <= 0:
        return False
    return lam_max / lam_min <= SOLVABLE_KAPPA


def band_lines(lmin: Optional[float], lmax: Optional[float], n: int, span_fs: float):
    """两个波长框（µm）→ 留下哪几根谱线。返回 (k_lo, k_hi, k_max, df_thz)。

    不合格就抛 ValueError（点太少 / 跨度 0 / 这一段一根都不剩），话说清是什么格宽。
    """
    k_max = (n - 1) // 2
    if k_max < 1:
        raise ValueError(f"只有 {n} 个点，做不了谱（至少要 3 个）")
    if not (span_fs > 0):
        raise ValueError("所有点的读出位置都在同一处（跨度 0），做不了谱")
    # **「全通」只有一个判据：没填（None）**。填了 0 / 负数 / nan 是填错了，不是「不限」——
    # 从前这里按 > 0 判，于是 ?lmin=0 能绕过「相位图全通必拒」那道守卫按全通算。
    for name, v in (("波长最小", lmin), ("波长最大", lmax)):
        if v is not None and not (v > 0):
            raise ValueError(f"{name}要填一个正数（µm）")
    df_thz = 1e3 / span_fs
    k_lo, k_hi = 1, k_max
    if lmin is not None:
        hi = (WAVELENGTH_UM_PER_THZ / lmin) / df_thz
        if not math.isfinite(hi):
            raise ValueError(f"波长最小填得太小（{lmin} µm），这一段取不出谱线")
        k_hi = min(k_hi, math.floor(hi))
    if lmax is not None:
        lo = (WAVELENGTH_UM_PER_THZ / lmax) / df_thz
        if not math.isfinite(lo):
            raise ValueError(f"波长最大填得太小（{lmax} µm），这一段取不出谱线")
        k_lo = max(k_lo, math.ceil(lo))
    if k_hi < k_lo:
        # 只填一边时另一边不写 None（用户没填那个框，看到 None 不知道该改哪儿）
        seg = ("≥ %g" % lmin if lmax is None else
               "≤ %g" % lmax if lmin is None else "%g ~ %g" % (lmin, lmax))
        raise ValueError(f"波长 {seg} µm 这一段里一根谱线都没有（谱的格是 {df_thz:.4g} THz）")
    return int(k_lo), int(k_hi), int(k_max), df_thz
