"""**一遍读盘**：数据处理那一层读帧的唯一入口。

相位图、强度对齐（以及将来别的"把每一帧读一遍"的东西）都要读同一条扫描的全部 PNG，
而"这一帧能不能用"这件事有三条不变式，逐字相同：

1. 读不出来（被删 / 被占用 / 不是图片）→ 说清是**第几个点**（idx 几）、哪条路径；
2. 不是二维灰度图 → 说清；
3. 裁剪记录与文件对不上 → **"库和文件对不上，先查清"**。这一条最要紧：
   它不会抛异常，只会让整幅图整体错一块 —— 整幅图这条路（相位图 / 强度对齐）只在这里判
   一次，别在调用方各写一遍。取数那条路（png_pixel_series）不经过这里，它自己核
   尺寸那一半（它只读第一张图定几何）。
   「对不上」有**两半**，都是同一件事（库里那一份 vs 文件那一份）：
   - **尺寸**：库写着 w×h，文件自己解出来不是这个大小；
   - **矩形**：文件自己的 tEXt（`CropRect`）写着 x0,y0,w,h，与库里那一份不一致。
     只比尺寸是不够的 —— 同一个尺寸、原点挪了两格，尺寸那一半一点都看不出来，
     整幅图却会整体错位（用户实测：文件写 2,1,6,6、库写 3,1,6,6，energy 差 0.0）。
   历史上每一次裁剪都写这块 tEXt（crop_frames 从有裁剪那一天起就写 CropRect），
   所以「库里有 crop 而文件里没有」同样算对不上，不当成老图放过；反过来（文件写着裁过、
   库里没记）也一样 —— 两份记录说的不是一件事就停下来查，别猜哪份对。
   **代价**：这一份只读 PNG 块头（read_png_meta 逐块走到 IEND，IDAT 整块 seek 过去），
   2000 帧全幅实测 0.02 s；按整幅读就是几 GB。

**为什么不把累加也放进来**：各家的累加不一样（相位图 2 张复图、强度对齐 2K+1 张），
那是它们自己的事；这里只负责"按顺序、一帧一帧地交出去"。

**为什么是有界窗口**：`ThreadPoolExecutor.map` 会把整条序列一路预取 ——
2000 帧全幅（每帧 float64 12.4 MB）就是 25 GB，实测直接把进程打死。
所以按窗口（默认 4×workers 帧）交出去，边交边消费：峰值内存只跟窗口大小有关，
与扫描多长无关。

**为什么用 `pool.map` 而不是 `as_completed`**：`map` 保序 —— 累加与进度都按 items 的顺序走，
调用方不必自己排序。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, Optional

# 一次最多在内存里压多少帧 = 窗口大小 × 单帧字节数。
# 窗口按「4 × workers」帧算（够让线程池一直有活干，又不至于堆起来）。
# **已知幅面时**（裁剪记录给了 w/h）再用这个上限兜底，窗口自动收小。
# 未知幅面（没裁过）时窗口就按 4 × workers 帧走：默认 workers=8、全幅 float64 时
# 32 帧 ≈ 496 MB，正好贴着这条线 —— 调大 workers 会越过它（这里没有可查的幅面，判不了）。
WINDOW_BYTES = 512 * 1024 * 1024


def where_of(i: int, idx) -> str:
    """报错时那句「第 N 个点（idx M）」—— 一处拼好，别处照抄。"""
    return f"第 {i + 1} 个点（idx {idx}）"


def check_frame(arr, crop, where: str, file_crop=None):
    """三条不变式。arr 是读出来的数组（读不出来时调用方先判 None）。

    `file_crop` 是**文件自己** tEXt 里那一份 `CropRect`（没有就是 None），`crop` 是库里那一份。
    两份必须说的是同一件事：**矩形逐个数相同**（没有就对没有）。只比尺寸是不够的 ——
    同一个尺寸、原点挪两格，尺寸那一半一点都看不出来。

    返回 arr 本身（方便链式调用）；不合格就抛 ValueError，话说清是哪个点。
    """
    if arr is None:
        raise ValueError(f"{where}的图读不出来")
    if arr.ndim != 2:
        raise ValueError(f"{where}不是灰度图")
    if crop and (arr.shape[1], arr.shape[0]) != (int(crop[2]), int(crop[3])):
        raise ValueError(
            f"{where}的图是 {arr.shape[1]}×{arr.shape[0]}，裁剪记录写着 "
            f"{crop[2]}×{crop[3]} —— 库和文件对不上，先查清"
        )
    db_rect = [int(v) for v in crop] if crop else None
    if db_rect != file_crop:
        raise ValueError(
            f"{where}的图上写着裁剪矩形 {file_crop or '没有'}，库里记着 "
            f"{db_rect or '没有'} —— 库和文件对不上，先查清"
        )
    return arr


def usable(items, what: str) -> None:
    """**缺一个点整条不算**：没图 / 没记读数，当场说清是第几个点。

    谱里没有"断开"这回事 —— 少一个点就是另一条采样序列（与频谱那张图同一条规矩）。
    `what` 只用来拼「做不了什么」，判断本身只有这一处。
    """
    n = len(items)
    if n < 3:
        raise ValueError(f"只有 {n} 个点，做不了{what}（至少要 3 个）")
    for i, (idx, um, path) in enumerate(items):
        where = where_of(i, idx)
        if um is None:
            raise ValueError(f"{where}没记下读出位置 —— 整条做不了：缺一个点就是另一条采样序列")
        if path is None:
            raise ValueError(f"{where}没采到图 —— 整条做不了：缺一个点就是另一条采样序列")


def _read_one(it, crop, dtype):
    """读一帧（线程里跑）：读不出来 → None 由 check_frame 说清；成功就交回 float 数组。

    裁剪记录那一份**先读**：`read_png_meta` 只扫块头、不解码、不读像素（tEXt 在 IDAT 之前，
    一帧只有几百字节），所以"解整幅之前就能拒"不花什么钱 —— 对不上的那种文件不必为它把
    几 MB 像素解一遍。**有没有 crop 都读**：库里没记、文件自己写着裁过，同样是"两份说的
    不是一件事"。
    """
    import numpy as np

    from .thorlabs_ccd import _png_gray, read_png_meta

    i, (idx, _, path) = it
    file_crop = read_png_meta(path)["crop"]
    arr = _png_gray(path)
    if arr is None:
        raise ValueError(f"{where_of(i, idx)}的图读不出来：{path}")
    check_frame(arr, crop, where_of(i, idx), file_crop=file_crop)
    return i, arr.shape, np.asarray(arr, dtype=dtype)


def read_frames(items, crop=None, workers: int = 8, dtype=None,
                progress: Optional[Callable[[int, int], None]] = None,
                window: Optional[int] = None) -> Iterator[tuple]:
    """按 items 的顺序，一帧一帧交回 `(i, shape, 数组)`（数组已转成 dtype）。

    **有界窗口**：内部一次只让 window 帧在飞，边读边交 —— 调用方的 for 循环消费完，
    这批才让位给下一批。峰值内存 ≈ window × 单帧字节，与扫描多长无关。

    **尺寸不齐**在这里判（第一帧定 shape）：这是"整条做不了"的那一类，说清第几个点。
    裁剪记录对不上的判定在 check_frame（逐帧，因为它比的是文件本身：解出来的尺寸与
    文件自带的那块 `CropRect`）。
    """
    import numpy as np

    if dtype is None:
        dtype = np.float64
    n = len(items)
    nw = max(1, int(workers))
    todo = list(enumerate(items))
    if window is None:
        # 先按「4 × workers」帧给窗口，再用 WINDOW_BYTES 兜底（不知道幅面时按裁剪记录估）
        window = max(1, 4 * nw)
        if crop:
            per = int(crop[2]) * int(crop[3]) * np.dtype(dtype).itemsize
            if per > 0:
                window = max(1, min(window, WINDOW_BYTES // per))

    shape = None
    done = 0
    with ThreadPoolExecutor(max_workers=nw) as pool:
        for s in range(0, n, window):
            batch = todo[s:s + window]
            # pool.map 保序：累加与进度都照 items 的顺序走
            for i, sh, arr in pool.map(lambda it: _read_one(it, crop, dtype), batch):
                if shape is None:
                    shape = sh
                elif sh != shape:
                    raise ValueError(
                        f"第 {i + 1} 个点的图是 {sh[1]}×{sh[0]}，"
                        f"别的点是 {shape[1]}×{shape[0]} —— 尺寸不齐，整条做不了"
                    )
                done += 1
                if progress:
                    progress(done, n)
                yield i, sh, arr
