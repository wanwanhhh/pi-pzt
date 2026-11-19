"""数据页：沿一条线的 **x-t / y-t 包络图** —— 每个位置、每一帧的带通信号幅度 |z|。

    E = |z_p(t)| / d_p^{(2n−1)/(2n)}        （默认：滤波后的包络）
    E = |I_p(t) − d_p| / d_p^{(2n−1)/(2n)}  （unfiltered：不滤波，逐帧的偏差取绝对值）

- **哪条线**：固定一行 y（`axis="y"`）沿 x 跑，或固定一列 x（`axis="x"`）沿 y 跑。
  位置是**原始坐标**（与取数、剖面、相位图同一套）：裁剪只在读文件那一层减一次偏移。
- **z(t)**：与〈滤波曲线〉那条**同一个式子** —— 非均匀最小二乘解出每根谱线的 A_k、B_k，
  再按 z = Σ(A_k − i·B_k)·e^{iθ_k} 加回去（系数行与基函数直接用 intensity_align.prep，
  那是同一条式子的家）。所以同一个位置、同一段波段的 |z| 就是那条曲线上灰细线包络的各点。
- **不滤波**（unfiltered）：去掉带通这一步，b(t) 直接是 I(t) − d，图上画 |b| —— 三步与分母
  一字不改。它答的是「整条时间序列的逐帧偏差」，与「全通」（所有能解出的谱线的包络）
  是两个口径（与强度对齐那张的「不滤波」同一条分界）。
- **d**：该位置所有帧的均值 —— 与强度对齐的 d 是同一个数（同一条扫描、同一个像素）。

**与强度对齐的关系**：那张图把时间加掉（Σ_t b²，每个像素一个数）；这张**把时间留着**
（每个位置一条时间序列，图上多一条时间轴）。两件事共用同一份系数解、同一个分母，
所以分母的规矩、n 的范围、门限相对谁，全都照旧（后端只给 |z| 与 d 两张与 n 无关的图，
改 n、改门限、改显示上限都只重画，不重读盘）。

**为什么两样都要交出去**：除以 d^α 是**逐位置**的常数因子，n 只决定它。与强度对齐同一个理由：
把 n 留在界面上，这一层就永远不用知道"归一化到什么程度"。

**代价**：一遍读盘（每帧只取一行 / 一列，不碰别的像素），两样缓冲各是「帧数 × 线长」
个 float64 —— 2000 帧 × 1440 px 是 23 MB，远小于整幅图那条（见 config.LINE_MAP_MAX_BYTES）。
"""
from __future__ import annotations

from typing import Callable, Optional

from .bands import band_lines
from .config import LINE_MAP_MAX_BYTES, TIME_FS_PER_UM

# 一块算多少帧的包络：同时活着的是「块宽 × 线长」的两张矩阵（实部、虚部各一张）。
# 128 MiB 的块落在 1440 px 的线上是 11000 帧，正常扫描（几千帧）一块就完。
_ENV_CHUNK_BYTES = 128 * 1024 * 1024


def _check_memory(n: int, length: int) -> None:
    """样本缓冲 + 输出各「帧数 × 线长」个 float64 —— 分配之前判，不跑到一半才爆。

    与强度对齐同一套做法（那里判的是 2K 张累加图），话术各说各的图，因为省下来的办法不同。
    """
    need = n * length * 8 * 2
    if need > LINE_MAP_MAX_BYTES:
        raise ValueError(
            f"这一条有 {n} 帧、线长 {length} 像素，要 {need / 2**20:.0f} MB 缓冲，"
            f"超过上限 {LINE_MAP_MAX_BYTES / 2**20:.0f} MB —— 先把这条扫描裁小，或换一条短的"
        )


def line_envelope(items, axis: str, index: int, crop=None,
                  lmin: Optional[float] = None, lmax: Optional[float] = None,
                  unfiltered: bool = False, workers: int = 8,
                  progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """这条扫描里**一条线**的逐帧包络。

    items 是 [(序号, 读出位置 µm, PNG 路径 | None), ...]（与取数、相位图同一形状）。
    axis/index 是**原始坐标**里的那一行 / 那一列；crop 给了就是 (x0, y0, w, h)，帧已经被裁成
    那一块（偏移在这里减一次，与取数同一套）。

    unfiltered 去掉带通：b 直接是 I − d，图上画 |b|（与强度对齐的「不滤波」同一口径）。

    返回 {env（帧 × 线长，float64）, dc（线长）, pos_um（每帧的读出位置）, axis, at, from,
    p, n, w, h, rect, span_fs, t0_fs, unfiltered, k_lo, k_hi, k_max, df_thz, band}：

    - env[i, j]：第 i 帧、线上第 j 个位置的 |z|（ADU，与幅度同一个单位、不含直流）；
    - dc[j]：线上第 j 个位置所有帧的均值；
    - from：线上第一个位置在原始坐标里的数（axis="y" 时是 x 起点，axis="x" 时是 y 起点）。
    """
    import numpy as np

    from . import frames
    from .intensity_align import prep

    if axis not in ("x", "y"):
        raise ValueError("线只有两种：x（固定一列、沿 y 跑）或 y（固定一行、沿 x 跑）")
    frames.usable(items, "线包络图")
    if index < 0:
        raise ValueError(f"位置不能是负数（填的是 {index}）")
    if unfiltered and (lmin is not None or lmax is not None):
        raise ValueError("不滤波就不分波段：别同时给波长")

    t_fs = [p * TIME_FS_PER_UM for (_, p, _) in items]
    span = max(t_fs) - min(t_fs)
    ks = rows_a = rows_b = phi = None
    if not unfiltered:
        k_lo, k_hi, k_max, df_thz = band_lines(lmin, lmax, len(items), span)
        ks, rows_a, rows_b, phi = prep(t_fs, k_lo, k_hi, span)

    n = len(items)
    off = ((crop[0] if axis == "x" else crop[1]) if crop else 0)
    j = index - off                          # 盘上那张图里的位置

    samples = None
    length = 0
    shape = None
    for i, sh, f in frames.read_frames(items, crop=crop, workers=workers,
                                       dtype=np.float64, progress=progress):
        if samples is None:
            # 线有多长、位置在不在画面里，第一帧读出来才知道 —— 判在这里、分配之前。
            h, w = int(sh[0]), int(sh[1])
            length = h if axis == "x" else w
            if not (0 <= j < (w if axis == "x" else h)):
                what = f"x 可填 {off} ~ {off + w - 1}" if axis == "x" else f"y 可填 {off} ~ {off + h - 1}"
                raise ValueError(f"位置 {index} 不在这条扫描的画面里（{what}）")
            _check_memory(n, length)
            shape = (h, w)
            samples = np.empty((n, length), dtype=np.float64)
        samples[i] = f[:, j] if axis == "x" else f[j, :]

    dc = samples.mean(axis=0)
    if unfiltered:
        # 不做带通：b = I − d，图上画 |b|（逐帧的偏差绝对值）。
        env = np.abs(samples - dc)
    else:
        k = rows_a.shape[0]
        coef_a = rows_a @ samples             # (K, 线长)：每根谱线在每个位置上的 A_k
        coef_b = rows_b @ samples
        cos_m, sin_m = phi[:, :k], phi[:, k:]

        # 逐块拼 z = (Σ A·cos + B·sin) + i·(Σ A·sin − B·cos) —— 与前端 filteredSignal 逐点同式，
        # 只是这里一次乘一整块帧（同一个式子、换一种加法顺序，都是 BLAS 的一步）。
        env = np.empty((n, length), dtype=np.float64)
        step = max(1, _ENV_CHUNK_BYTES // max(1, 8 * length * 4))
        for s0 in range(0, n, step):
            s1 = min(n, s0 + step)
            cb, sb = cos_m[s0:s1], sin_m[s0:s1]
            zr = cb @ coef_a + sb @ coef_b
            zi = sb @ coef_a - cb @ coef_b
            env[s0:s1] = np.hypot(zr, zi)

    h, w = shape
    x0, y0 = (int(crop[0]), int(crop[1])) if crop else (0, 0)
    out = {
        "env": env,
        "dc": dc,
        "pos_um": np.asarray([p for (_, p, _) in items], dtype=np.float64),
        "axis": axis,
        "at": int(index),
        "from": int(x0 if axis == "y" else y0),
        "p": int(length),
        "n": int(n),
        "w": int(w),
        "h": int(h),
        "rect": [x0, y0, int(w), int(h)] if crop else None,
        "span_fs": float(span),
        "t0_fs": float(min(t_fs)),
        "unfiltered": bool(unfiltered),
        "band": [None, None] if unfiltered else [lmin, lmax],
    }
    if not unfiltered:
        out.update(k_lo=int(ks[0]), k_hi=int(ks[-1]), k_max=int(k_max), df_thz=df_thz)
    return out
