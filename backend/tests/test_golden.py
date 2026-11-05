"""跨语言 golden：同一份期望值，Python 与 JS 两边各跑一遍。

**这条钉子补的是什么**：`app.js` 的 `pixelSpectrum` / `filteredSignal` 与
`phasemap._grid` / `intensity_align._prep` 是同一条式子的两份实现（非均匀采样下逐根解 2×2 正规方程）。
改了一边、另一边不报错、只出错数 —— 而现有的两条"自证"都是拿 Python 重写一遍去对 Python，
**对 JS 那一份零约束**。这里用一份独立算出来的期望值把两边钉在同一根桩上。

**期望值怎么来的**：它就是**最小二乘投影的定义本身** —— 把样本去均值，**每一根谱线各投影一次**
到 span{cos_k, sin_k} 上，用 `np.linalg.lstsq`（QR/SVD）算。两边实现走的是 2×2 正规方程的代数式，
算法不同，所以这里钉的是"这条式子该给什么数"，不是"现状给什么数"。

**"逐根各解一次"是口径的一半，不能省**：把 2K 个基函数放在一起解一次是**另一个**最小二乘问题 ——
非均匀采样下 cos7/sin7 与 cos19/sin19 彼此不正交，联合解会把能量在两根之间重新分配
（实测用例 2 的 k=7：联合解 A=3.974292、B=0.013210，逐根解 A=3.973640、B=−0.121714）。
两边实现（`pixelSpectrum` 的 2×2、`_prep` 的 2×2）都是逐根，所以 golden 也必须逐根。

**为什么不是漂亮的 2.5**：采样非均匀时 cos/sin 在 N 个点上**并不正交**，
所以即使样本是 `3.0 + 2.5·cos(11θ)`，投影系数也会略偏离 2.5（实测 2.49976）。
这正是这套算法的实质，期望值里必须带着它 —— 谁把它"修正"成 2.5，谁就把这条口径改坏了。

**JS 那半边**在 `tools/uitest.js` 的 `[AC]` 段：它读同一个 `golden_spectrum.json`，
跑 `pixelSpectrum` 与 `filtKeepRange`，与这里的期望值逐根比。

直接跑：python backend/tests/test_golden.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.bands import band_lines, solvable  # noqa: E402
from backend.intensity_align import _prep  # noqa: E402
from backend.phasemap import _grid  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parent / "golden_spectrum.json").read_text("utf-8"))


def _theta():
    t = np.asarray(GOLDEN["t_fs"], dtype=np.float64)
    return 2 * math.pi * (1.0 / GOLDEN["span_fs"]) * (t - t.min())


def _projection(x, ks):
    """**定义**：去均值之后，**每一根各**投影到 span{cos_k, sin_k}（lstsq/QR，与两边的正规方程不同路）。

    `ks` 只是"要算哪几根"，不是"一起解"—— 每一根一个 2×2（见文件头"逐根各解一次"）。
    """
    th = _theta()
    xm = np.asarray(x, dtype=np.float64) - np.mean(x)
    out = {}
    for k in ks:
        basis = np.stack([np.cos(k * th), np.sin(k * th)], axis=1)
        coef, *_ = np.linalg.lstsq(basis, xm, rcond=None)
        out[k] = (float(coef[0]), float(coef[1]))
    return out


def test_golden_is_internally_consistent():
    """golden 文件自己那几个数必须互相对得上 —— 这是它**唯一能自证的部分**。

    `want` 是用 lstsq 算的（见 `_rule`），而 `_projection` 也是 lstsq，两边同一套算法、同一份数据，
    所以"重算一遍 want"只能证明**文件没被手改坏**，证明不了"期望值是对的"（那是同义反复）。
    真正能自证的是这几条恒等式：时间列、跨度、格宽、上限必须互相对得上 —— 改掉任何一个，
    其它三个就不再成立。
    """
    t = GOLDEN["t_fs"]
    n = GOLDEN["n"]
    assert len(t) == n, (len(t), n)
    assert abs((max(t) - min(t)) - GOLDEN["span_fs"]) < 1e-9, (max(t) - min(t), GOLDEN["span_fs"])
    assert abs(GOLDEN["df_thz"] - 1e3 / GOLDEN["span_fs"]) < 1e-9, GOLDEN["df_thz"]
    assert GOLDEN["k_max"] == (n - 1) // 2, GOLDEN["k_max"]
    # 起点**不在 0**：真机的时间列起点是扫描起点。放在 0 会让「以 t0 为相位零点」这条
    # 四处共用的约定在两侧都测不出来（减不减 t0 给出同一串数）。
    assert min(t) > 1e-6, min(t)


def test_golden_expectations_are_reproducible_from_the_definition():
    """golden 文件里的期望值必须能从定义（lstsq 投影）重新算出来。

    **这条是文件完整性检查，不是"定义自证"**（两边同一个 lstsq，见上一条的说明）。
    它能拦住的：want 被手改、被别的口径（联合解）覆盖、x 与 want 不再配对。
    """
    for case in GOLDEN["cases"]:
        want = {int(k): (v["A"], v["B"]) for k, v in case["want"].items()}
        got = _projection(case["x"], sorted(want))
        for k, (a, b) in want.items():
            assert abs(got[k][0] - a) < 1e-9, (case["name"], k, got[k][0], a)
            assert abs(got[k][1] - b) < 1e-9, (case["name"], k, got[k][1], b)


def test_band_lines_matches_the_analytic_rule():
    """波段 → 谱线：与解析式子（λ = c/ν、上限卡名义 Nyquist）逐条对上，不是"跟现状一致"。"""
    for b in GOLDEN["bands"]:
        got = band_lines(b["lmin"], b["lmax"], GOLDEN["n"], GOLDEN["span_fs"])
        assert (got[0], got[1]) == (b["k_lo"], b["k_hi"]), (b, got)
        assert got[2] == GOLDEN["k_max"], (b, got)
        assert abs(got[3] - GOLDEN["df_thz"]) < 1e-9, (b, got)


def test_solvable_matches_the_golden_rows():
    """「这根解不解得出」两边同一份期望值：判据是**条件数**，不是行列式的符号。

    那三行钉住的正是 `det > 0` 挡不住的那一格：等间隔 2001 点的 Nyquist 那根
    `det ≈ 2.4e-19 > 0`，条件数却是 6e-26（系数被放大到 1e11）；
    同一根在 40 nm 读数噪声下条件数只有 6e1 —— 真实扫描的 Nyquist 该留着。
    JS 那半边（`specSolvable`）在 tools/uitest.js 的 [AC] 段读同一段。
    """
    block = GOLDEN["solvable"]
    assert block["threshold"] == 1e12, block["threshold"]
    for row in block["rows"]:
        got = solvable(row["scc"], row["sss"], row["scs"])
        assert got == row["solvable"], (row["why"], got, row["solvable"])


def test_prep_rows_agree_with_the_golden():
    """`intensity_align._prep` 交出来的系数行 = golden 那组投影（同一份式子，两处实现）。

    **比的是"逐根单独解"**（与前端 `pixelSpectrum` 同一个做法：每根各解一个 2×2），
    不是"把 2K 个基函数放在一起解一次"—— 后者是另一个最小二乘问题，系数会不同（非均匀采样
    下两族基函数不正交，联合解会把能量在它们之间重新分配）。golden 的期望值就是逐根投影。

    做法：只把**这一根**交给 `_prep`（k_lo = k_hi = k），这样它交出来的就是逐根那一份。
    """
    for case in GOLDEN["cases"]:
        x = np.asarray(case["x"], dtype=np.float64)
        for k_str, want in case["want"].items():
            k = int(k_str)
            _ks, rows_a, rows_b, _phi = _prep(GOLDEN["t_fs"], k, k, GOLDEN["span_fs"])
            got_a, got_b = float(x @ rows_a[0]), float(x @ rows_b[0])
            assert abs(got_a - want["A"]) < 1e-9, (case["name"], k_str, got_a, want["A"])
            assert abs(got_b - want["B"]) < 1e-9, (case["name"], k_str, got_b, want["B"])


def test_prep_multi_root_agrees_with_the_golden():
    """`_prep` 一次要**多根**时，交出来的每一行都对得上 golden 里对应的那一根。

    **这条补的是上面那条的缺口**：上面只把一根（k_lo = k_hi = k）交给 `_prep`，而单根时
    「逐根各解一个 2×2」与「把 2K 个基函数一起解一次」在数学上**完全相同** —— 所以那条
    证明不了它自己声称的口径。这里用 golden 用例 2（k = 7 与 19 各带自己的相位）真正跨多根：

    - 逐根解给出 A₇ = 3.973640、B₇ = −0.121714；联合解会给出 A₇ = 3.989286、B₇ = −0.020345
      —— 差别在 1e-2 量级，1e-9 的容差拦得住；
    - rows 的顺序（第 i 行 = 第 ks[i] 根）与 phi 的列顺序（[cos…, sin…]）也一并钉住：
      倒过来就红。
    """
    case = next(c for c in GOLDEN["cases"] if len(c["want"]) > 1)
    ks_want = sorted(int(k) for k in case["want"])
    # `_prep` 吃的是**一段**谱线（k_lo..k_hi），所以这里要 7..19 整段：多根同时解，
    # 只有每根各解一个 2×2 才对得上 golden；联合解在第 7 根上差 1.6e-2。
    ks, rows_a, rows_b, phi = _prep(GOLDEN["t_fs"], ks_want[0], ks_want[-1], GOLDEN["span_fs"])
    assert ks == list(range(ks_want[0], ks_want[-1] + 1)), ks
    assert rows_a.shape == (len(ks), len(GOLDEN["t_fs"])), rows_a.shape
    # phi 的列 = [cos_k …, sin_k …]，与 a = [A_1..A_K, B_1..B_K] 一一对应
    assert phi.shape == (len(GOLDEN["t_fs"]), 2 * len(ks)), phi.shape
    th = _theta()
    for i, k in enumerate(ks):
        assert np.allclose(phi[:, i], np.cos(k * th), atol=1e-12), i
        assert np.allclose(phi[:, len(ks) + i], np.sin(k * th), atol=1e-12), i
    x = np.asarray(case["x"], dtype=np.float64)
    for k in ks_want:
        i = ks.index(k)
        want = case["want"][str(k)]
        assert abs(float(x @ rows_a[i]) - want["A"]) < 1e-9, (k, float(x @ rows_a[i]), want["A"])
        assert abs(float(x @ rows_b[i]) - want["B"]) < 1e-9, (k, float(x @ rows_b[i]), want["B"])


def test_grid_rows_agree_with_the_golden():
    """`phasemap._grid` 与 `intensity_align._prep` 解的是同一对 2×2，只在**去均值**上分工不同。

    `_grid` 交的是**原始**权重（去均值由调用方 `_demean` 统一做：`weight_vector` 那一步）；
    `_prep` 交的**已经去过均值**（它的调用方直接拿去乘样本）。所以这里比的是
    「`_grid` 的行减掉它自己的均值」= 「`_prep` 的行」—— 差的只是一个常数，不是两套式子。

    复数拼法是 `ga − i·gb`（"c_k = A − iB"那个约定的另一半），两处一致。
    """
    t = GOLDEN["t_fs"]
    _t, _t0, _df, _th, rows = _grid(t, 7, 7, GOLDEN["span_fs"])
    _ks, rows_a, rows_b, _phi = _prep(t, 7, 7, GOLDEN["span_fs"])
    assert len(rows) == 1 and _ks == [7], (rows, _ks)
    row = rows[0][1]
    demeaned = (row - row.mean())
    assert np.allclose(demeaned, rows_a[0] - 1j * rows_b[0], atol=1e-12), (
        np.abs(demeaned - (rows_a[0] - 1j * rows_b[0])).max(),
    )
    # 反过来：原始行与"去过均值的行"只差一个常数（这条钉住"分工不同、式子同一个"）
    assert np.allclose(row - demeaned, np.full(row.shape, row.mean()), atol=1e-12)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
