"""把一条扫描的整幅强度对齐图里的超亮斑抹成"环光"，出一张 PNG。

**一次性出图，不是服务功能**：结果只落在文件里，库里的 energy / dc、界面上的画法都不动。
以后要重做一张，跑这个脚本，别再从头摸算法。

    .venv/bin/python tools/retouch_align.py --scan 114 --out data/scan0114-retouch.png
    .venv/bin/python tools/retouch_align.py --bin dump.bin --width 1440 --height 1080 --out x.png

输入是**后端算好的那一份数组**（`GET /api/scans/{id}/align.bin`：先 energy 后 dc、float32 行优先），
所以要先在数据页点过「算强度对齐」、服务还开着；也可以自己把 .bin 存盘再喂进来。
输出：PNG（8 位，上限按 `--cap-pct` 分位）+ 同目录一份 .npy（float32 原始值，换上限重画用它）。

## 做法（这套数就是当初那张被认可的图用的）

1. S = energy / dc^((2n−1)/(2n))，与界面同一个式子（n 默认 3）。
2. 环心取"亮区质心"；在极坐标里对 log10(S) 做归一化模糊（σ_r = 6 px、σ_θ = 30 格、迭代 8 次），
   把判成亮斑的格子排除在外 —— 得到的是**这一带本该有的亮度**，不是斑有多亮。
3. 亮斑 = S 超过模型 1.3 倍、面积 ≥ 20 像素的连通域（比值不取绝对值：环本身明暗差很大）。
4. 掩区膨胀 4 px，再膨胀 30 px 后做 81 px 的盒式模糊当羽化（≈ 60 px 的渐变），边界看不出硬边。
5. 填**模型值**，再按周围的高通残差补 55% 的颗粒（log 域）—— 不补就是一块平涂。
6. 显示上限取修完后数据的 99.5% 分位。原图那套 98.5% 在这张图上会偏低一档：斑点抹掉后，
   "最亮的那 1.5%" 已经不是同一批像素了。

## 经验（按这个顺序踩的坑，别再踩）

- **只抹斑心** → 外面留一圈亮边，甜甜圈。
- **从掩区边界往里铺平**（界面上「掩掉亮斑」就是这招）→ 填进去的是斑自己的光晕，**照样白**。
- **填模型值**才压得下去；代价是放大 3 倍能看出那几块"太干净"的平滑斑，1:1 看不出。
- **模型要返回"整幅平滑后的场"**，不是"原值 + 掩区填空"：后者在斑的位置仍然跟着斑走，填了等于没填。
  这一条是当初那张图能成的关键，动代码前先读一遍。
- `box(mask, k)` 是**对整窗求平均**，不是膨胀：小斑上 alpha 只有零点几，等于没填。
  羽化要 `box(dilate(mask, ramp), k)`。

## 试过但这条路不通（省得再想）

- **用直流图归一化**（S / k^(7/6)，k = dc / dc 的平滑模型）：S 已经除过一次直流，再除一次把整条环一起压黑。
- **泊松融合**（把同半径干净扇区的纹理搬进斑里）：大斑上出黑斑、半月形。
- **对数域方位插值**：回来是一圈圈条纹带。

## 参数

    --mask-ratio 1.3   超过局部模型多少倍算亮斑
    --min-area   20    连通域最小像素数（滤掉单点噪声）
    --dilate     4     掩区膨胀（px）
    --halo       0     按斑自身半径再放几倍（盖住渐弱光晕；>0 会把斑周围压出一圈灰盘，慎用）
    --ramp       30    羽化前再膨胀多少（px）
    --blur       81    羽化的盒式模糊窗口（px）
    --noise      0.55  补颗粒的比例（0 = 平涂）
    --cap-pct    99.5  显示上限取哪个分位
    --n          3     归一化强度，与界面上的 n 同义
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from collections import deque

import numpy as np
from PIL import Image


def load_bin(path: str, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    buf = np.fromfile(path, dtype="<f4") if not path.startswith("http") else _fetch(path)
    if buf.size != 2 * w * h:
        raise SystemExit(f"数组长度不对：{buf.size}，按 {w}×{h} 该是 {2 * w * h}")
    energy = buf[: w * h].reshape(h, w).astype(np.float64)
    dc = buf[w * h :].reshape(h, w).astype(np.float64)
    return energy, dc


def _fetch(url: str) -> np.ndarray:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return np.frombuffer(resp.read(), dtype="<f4")


def series(energy: np.ndarray, dc: np.ndarray, n: float) -> np.ndarray:
    expo = (2 * n - 1) / (2 * n)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = energy / np.power(np.where(dc > 0, dc, np.nan), expo)
    return s


def centre(s: np.ndarray) -> tuple[float, float]:
    """环心：最亮那一圈像的质心（亮度阈值取中位数的 5 倍，免得背景把质心拽偏）。"""
    v = np.nan_to_num(s)
    m = v > np.median(v) * 5
    if not m.any():
        h, w = s.shape
        return (w - 1) / 2.0, (h - 1) / 2.0
    ys, xs = np.nonzero(m)
    return float(xs.mean()), float(ys.mean())


def polar_grid(shape, cx, cy, rmax=660, ntheta=1440):
    """笛卡尔 → 极坐标的两个方向：`grid` 是采样极坐标格点用的下标/小数部分，
    `coords` 是把笛卡尔图采样成极坐标图用的（反过来用 `grid`）。"""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(xx - cx, yy - cy)
    th = np.arctan2(yy - cy, xx - cx) % (2 * np.pi)
    rq = np.clip(r - 0.5, 0, rmax - 1.001)
    tq = th / (2 * np.pi) * ntheta
    i0 = np.floor(rq).astype(int)
    j0 = np.floor(tq).astype(int) % ntheta
    i1 = np.minimum(i0 + 1, rmax - 1)
    j1 = (j0 + 1) % ntheta
    fr = rq - i0
    ft = tq - np.floor(tq)
    ths = np.arange(ntheta) * (2 * np.pi / ntheta)
    rs = np.arange(rmax) + 0.5
    X = cx + rs[:, None] * np.cos(ths)[None, :]
    Y = cy + rs[:, None] * np.sin(ths)[None, :]
    x0 = np.clip(np.floor(X).astype(int), 0, w - 2)
    y0 = np.clip(np.floor(Y).astype(int), 0, h - 2)
    fx = np.clip(X - np.floor(X), 0, 1)
    fy = np.clip(Y - np.floor(Y), 0, 1)
    ok = (X >= 0) & (X < w - 1) & (Y >= 0) & (Y < h - 1)
    coords = (y0, x0, fy, fx, ok)
    return r, th, (i0, i1, j0, j1, fr, ft), coords


def to_polar(arr: np.ndarray, coords) -> np.ndarray:
    y0, x0, fy, fx, ok = coords
    a = arr[y0, x0] * (1 - fx) + arr[y0, x0 + 1] * fx
    b = arr[y0 + 1, x0] * (1 - fx) + arr[y0 + 1, x0 + 1] * fx
    return np.where(ok, a * (1 - fy) + b * fy, 0.0)


def blur_r(a: np.ndarray, sig: float) -> np.ndarray:
    rad = int(3 * sig)
    k = np.exp(-0.5 * (np.arange(-rad, rad + 1) / sig) ** 2)
    k /= k.sum()
    p = np.pad(a, ((rad, rad), (0, 0)), mode="edge")
    return np.apply_along_axis(lambda v: np.convolve(v, k, "valid"), 0, p)


def blur_t(a: np.ndarray, sig: float) -> np.ndarray:
    rad = int(3 * sig)
    k = np.exp(-0.5 * (np.arange(-rad, rad + 1) / sig) ** 2)
    k /= k.sum()
    p = np.concatenate([a[:, -rad:], a, a[:, :rad]], axis=1)
    return np.apply_along_axis(lambda v: np.convolve(v, k, "valid"), 1, p)


def polar_model(log_v: np.ndarray, coords, weight: np.ndarray) -> np.ndarray:
    """极坐标里的归一化卷积：**只用 weight 为真的格子**，得到环的平滑模型（log 域）。

    未选中的格子从邻域估一个值补齐、权重记 1，再卷积 —— 迭代 8 次；
    补进来的值会参与下一轮，所以大块掩区里也是平滑过渡（不是空着的洞）。
    """
    lp = to_polar(log_v, coords)
    wp = (to_polar(weight.astype(float), coords) > 0.5).astype(float)
    num = lp * wp
    den = wp.copy()
    for _ in range(8):
        # 分子分母各自接着上一轮继续模糊，**不把补进来的值再喂回去**：
        # 掩区越被补，分母越接近 1，补出来的模型就比"邻域平均"低一截。
        # 这一截正是当初那张图"斑被压下去"的原因（见文件头的说明）。
        num = blur_t(blur_r(num, 6.0), 30.0)
        den = blur_t(blur_r(den, 6.0), 30.0)
        f = np.divide(num, den, out=np.zeros_like(num), where=den > 1e-8)
        lp = np.where(wp > 0, lp, f)
        wp = np.ones_like(wp)
    return f


def sample_polar(f: np.ndarray, grid) -> np.ndarray:
    i0, i1, j0, j1, fr, ft = grid
    a = f[i0, j0] * (1 - ft) + f[i0, j1] * ft
    b = f[i1, j0] * (1 - ft) + f[i1, j1] * ft
    return a * (1 - fr) + b * fr


def dilate(m: np.ndarray, k: int) -> np.ndarray:
    out = m.copy()
    for _ in range(k):
        o = out.copy()
        o[1:, :] |= out[:-1, :]
        o[:-1, :] |= out[1:, :]
        o[:, 1:] |= out[:, :-1]
        o[:, :-1] |= out[:, 1:]
        out = o
    return out


def box(a: np.ndarray, k: int) -> np.ndarray:
    p = k // 2
    ap = np.pad(a, p, mode="reflect")
    c = np.cumsum(np.cumsum(ap, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def components(m: np.ndarray, min_area: int) -> list[list[tuple[int, int]]]:
    h, w = m.shape
    seen = np.zeros_like(m, dtype=bool)
    out = []
    for y, x in zip(*np.nonzero(m)):
        if seen[y, x]:
            continue
        q = deque([(y, x)])
        seen[y, x] = True
        pix = []
        while q:
            a, b = q.popleft()
            pix.append((a, b))
            for da in (-1, 0, 1):
                for db in (-1, 0, 1):
                    na, nb = a + da, b + db
                    if 0 <= na < h and 0 <= nb < w and m[na, nb] and not seen[na, nb]:
                        seen[na, nb] = True
                        q.append((na, nb))
        if len(pix) >= min_area:
            out.append(pix)
    return out


def retouch(s: np.ndarray, args) -> tuple[np.ndarray, dict]:
    v = np.nan_to_num(s)
    cx, cy = centre(s)
    r, _th, grid, coords = polar_grid(s.shape, cx, cy)
    log_v = np.log10(np.maximum(v, 1.0))
    # 先用"按半径取中位数"粗挑一遍：粗挑出来的**连通块**（面积够大、峰值够高）不进模型，
    # 模型才是"这一带本该有的亮度"。用连通块而不是逐格，是为了让斑的核心整块被排除掉。
    rb = np.clip((r / 2).astype(int), 0, 340)
    prof = np.array([np.median(v[rb == k]) if (rb == k).any() else 1.0 for k in range(341)])
    keep = (v / np.maximum(prof[rb], 1e-9)) <= args.mask_ratio
    model = sample_polar(polar_model(log_v, coords, keep), grid)
    ratio = v / np.maximum(10.0 ** model, 1e-9)

    comps = components(ratio > args.mask_ratio, args.min_area)
    mask = np.zeros(v.shape, dtype=bool)
    for pix in comps:
        ay = np.array([p[0] for p in pix])
        ax = np.array([p[1] for p in pix])
        m = np.zeros(v.shape, dtype=bool)
        m[ay, ax] = True
        # 每颗斑按**自己的大小**往外放：斑的渐弱光晕有它自身半径那么大，
        # 只放固定几像素的话光晕留在掩区外，补完就是一圈亮边（"甜甜圈"的边）。
        r_eq = np.sqrt(len(pix) / np.pi)
        mask |= dilate(m, args.dilate + int(round(args.halo * r_eq)))

    hp = log_v - box(log_v, 9)
    sig = np.clip(np.sqrt(np.maximum(box(hp * hp, 31), 0.0)), 0.01, 0.25)
    rng = np.random.default_rng(23)
    alpha = np.clip(box(dilate(mask, args.ramp).astype(float), args.blur), 0.0, 1.0)
    fill = model + rng.standard_normal(v.shape) * sig * args.noise
    out = 10.0 ** np.clip(log_v * (1 - alpha) + fill * alpha, 0, 6)
    return out, {"comps": len(comps), "cells": int(mask.sum()), "centre": (round(cx, 1), round(cy, 1))}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="抹掉强度对齐图上的超亮斑，出 PNG（一次性出图）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--scan", type=int, help="扫描号：从跑着的服务取 /api/scans/<id>/align.bin")
    src.add_argument("--bin", help="已经存盘的 .bin（先 energy 后 dc，float32）")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址（--scan 用）")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--n", type=float, default=3.0)
    ap.add_argument("--mask-ratio", type=float, default=1.3)
    ap.add_argument("--min-area", type=int, default=20)
    ap.add_argument("--dilate", type=int, default=4)
    ap.add_argument("--halo", type=float, default=0.0, help="掩区在斑自身半径之外再放多少倍（盖住渐弱的光晕）")
    ap.add_argument("--ramp", type=int, default=30)
    ap.add_argument("--blur", type=int, default=81)
    ap.add_argument("--noise", type=float, default=0.55)
    ap.add_argument("--cap-pct", type=float, default=99.5)
    ap.add_argument("--out", required=True, help="写哪里（.png）；同目录再存一份 .npy 原始值")
    args = ap.parse_args(argv)

    path = args.bin or f"{args.url}/api/scans/{args.scan}/align.bin"
    energy, dc = load_bin(path, args.width, args.height)
    s = series(energy, dc, args.n)
    out, stat = retouch(s, args)
    vals = np.sort(out.ravel())
    cap = float(vals[int(args.cap_pct / 100.0 * (vals.size - 1))])
    img = np.round(np.clip(out / cap, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(img).save(args.out)
    np.save(args.out.rsplit(".", 1)[0] + ".npy", out.astype(np.float32))
    print(
        f"抹了 {stat['comps']} 块、{stat['cells']} 格；环心 {stat['centre']}；"
        f"上限 {cap:.0f}（{args.cap_pct}% 分位）；白像素 {int((img == 255).sum())} → {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))