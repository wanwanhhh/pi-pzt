"""上机运动测试：空走一步 + 阶跃 + 蠕变 + 持位重读。

**单位关系是实测出来的，不是猜的**（tools/xmt_scale.py 量出的斜率；
"27/35 是 µm" 那半见 docs/xmt/README.md 的判别链）：

    设点 B3=1     : µm
    读回 B3=6 / 8 : 4/3 µm     真实 = 读回 x 0.75
    行程 B3=27/35 : µm

所以**读回值绝不能直接当设点写回去** —— 会跑偏 4/3 倍。
实测：写 144.88（当时读回值）让台子从 108.66 µm 走到 144.88 µm，位移 36 µm。

**这个脚本会动台子。** 默认幅度 ±2 µm，占行程 1%。
硬门：19 必须读到 'C'（闭环）—— 开环下 B3=1 的数值是伏特不是位移，立即退出。
结束前回到起始位置。

用法：
    python tools/xmt_move.py
    python tools/xmt_move.py --step 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import serial
from serial.tools import list_ports

from backend import xmt_protocol as xp
from xmt_check import VID, PID, Link

# 这个脚本唯一被允许的写命令
ALLOW = frozenset((xp.CMD_SET_POSITION,))

READBACK_TO_UM = 0.75   # 读回 -> µm，实测
TOL_RB = 0.8            # 到位判据，读回单位（≈0.6 µm）
WINDOW = 6
TIMEOUT = 8.0


def read_rb(link: Link) -> float | None:
    f = link.ask(link.cmd(xp.CMD_READ_POSITION, bytes((0,))), xp.CMD_READ_POSITION, wait=0.3)
    return f.value if f is not None else None


def find_port(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for p in list_ports.comports():
        if (p.vid, p.pid) == (VID, PID):
            return p.device
    return None


def go(link: Link, tgt_um: float):
    """走到 tgt_um（µm），等读数停稳。返回 (读回均值, µm, 耗时, 次数)。"""
    want = tgt_um / READBACK_TO_UM
    link.send(link.cmd(xp.CMD_SET_POSITION, bytes((0,)) + xp.encode_value(tgt_um)), allow=ALLOW)
    t0 = time.monotonic()
    hist: list[float] = []
    while time.monotonic() - t0 < TIMEOUT:
        x = read_rb(link)
        if x is None:
            continue
        hist.append(x)
        win = hist[-WINDOW:]
        if len(win) == WINDOW and abs(x - want) < TOL_RB and max(win) - min(win) < TOL_RB:
            mean = statistics.mean(win)
            return mean, mean * READBACK_TO_UM, time.monotonic() - t0, len(hist)
    return None, None, time.monotonic() - t0, len(hist)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="E53.D1S-H 运动测试（会动，幅度 µm 级）")
    ap.add_argument("--port")
    ap.add_argument("--step", type=float, default=2.0, help="阶跃幅度 µm")
    args = ap.parse_args()

    port = find_port(args.port)
    if not port:
        print(f"没找到 VID:PID={VID:04X}:{PID:04X}")
        return 2

    link = Link(port)
    try:
        f = link.ask(link.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.5)
        if f is None or f.data != b"OK":
            print("握手失败，先跑 tools/xmt_check.py")
            return 1

        fl = link.ask(link.cmd(xp.CMD_READ_LOOP_MODE, bytes((0,))), xp.CMD_READ_LOOP_MODE)
        if not (fl is not None and fl.data[1:2] == b"C"):
            print(f"19 读到 {fl.data[1:2] if fl is not None else None!r}，不是闭环 —— 拒绝发设点")
            return 1
        hi = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_HIGH, bytes((0,))),
                      xp.CMD_READ_POS_LIMIT_HIGH).value
        lo = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_LOW, bytes((0,))),
                      xp.CMD_READ_POS_LIMIT_LOW).value
        print(f"19='C' 闭环   行程 {lo:.4f} ~ {hi:.4f} µm   读回 x {READBACK_TO_UM} = µm")

        seeds = [v for v in (read_rb(link) for _ in range(6)) if v is not None]
        p0 = statistics.mean(seeds) * READBACK_TO_UM
        print(f"起始 {p0:.4f} µm   σ {statistics.pstdev(seeds) * READBACK_TO_UM:.4f} µm\n")

        # 空走一步：写的是**真实的 µm 位置**，所以它真的是零位移
        n_rep = 0
        for _ in range(8):
            link.send(link.cmd(xp.CMD_SET_POSITION, bytes((0,)) + xp.encode_value(p0)), allow=ALLOW)
            n_rep += len(link.wait(0.15))
            link.gap()
        _, after, _, _ = go(link, p0)
        print(f"空走一步 x8（写真实位置 {p0:.4f} µm）→ 回包 {n_rep} 条 → "
              f"{'有应答' if n_rep else '无应答，与指令表一致'}")
        print(f"  位后 {after:.4f} µm，位移 {after - p0:+.4f} µm\n")

        s = args.step
        res: dict[str, float] = {}

        def step(label: str, tgt: float) -> float | None:
            _, um, dt, n = go(link, tgt)
            if um is None:
                print(f"  {label:<24} -> {tgt:.4f} µm：未停稳（读了 {n} 次）")
                return None
            print(f"  {label:<24} -> {tgt:.4f} µm：实到 {um:.4f}  误差 {um - tgt:+.4f}  用时 {dt:.2f}s")
            res[label] = um
            return um

        print(f"== 阶跃 ±{s:g} µm ==")
        a = step(f"从下往上 +{s:g}", p0 + s)
        if a is not None:
            creep = [v for v in (read_rb(link) for _ in range(40)) if v is not None]
            if len(creep) >= 10:
                h = len(creep) // 2
                print(f"       停稳后 2 s：极差 {(max(creep) - min(creep)) * READBACK_TO_UM:.4f}  "
                      f"漂移 {(statistics.mean(creep[h:]) - statistics.mean(creep[:h])) * READBACK_TO_UM:+.4f} µm")
        step(f"到 -{s:g}（这次是反向的）", p0 - s)
        c = step(f"再回到 +{s:g}", p0 + s)
        # 这两步的设点和上一步 c 相同 —— 台子不会动，量到的是**持位重读**，不是重复性
        reps = [v for v in (step(f"同点重写 {i + 1}", p0 + s) for i in range(2))
                if v is not None]

        print("\n== 结果 ==")
        if a is not None and c is not None:
            print(f"  同一目标 {p0 + s:.4f} µm 的两次到达：{a:.4f}（起点 p0）"
                  f" / {c:.4f}（起点 p0-{s:g}）")
            print(f"  两次之差 = {abs(c - a):.4f} µm")
            print("  **这不是迟滞**：两次都是向上逼近，只是起点距离不同（2 µm 与 4 µm）。"
                  "迟滞要反向逼近到同一目标，本脚本没做 —— 见 docs/xmt/README.md")
        if reps:
            print(f"  持位重读：{['%.4f' % v for v in reps]}  极差 {max(reps) - min(reps):.4f} µm")
            print("  （设点和上一步相同，台子没动 —— 这不是重复性；"
                  "要重复性得换个目标再回来，本脚本没做）")
        if a is not None:
            print(f"  符号方向：命令 +{s:g} µm 得到 {a - p0:+.4f} µm")

        print(f"\n复位到 {p0:.4f} µm")
        _, um, _, _ = go(link, p0)
        print(f"  -> {um:.4f} µm（偏差 {um - p0:+.4f}）")
    except serial.SerialException as exc:
        print(f"串口出错：{exc}")
        return 2
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
