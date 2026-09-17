"""标定设点与读回的尺度差。

实测发现：把读回值原样当设点写回去，台子会跑到 4/3 倍的位置。
即 设点单位 = µm，读回单位 = 0.75 µm。手册没写这个系数，
53（读单位）只回一个"位移"，不含尺度。

本脚本用**几微米**的小阶跃测斜率，不动大行程：
  读回当前位置 → 依次把设点抬高 1/2/4 µm → 每次等读数不动 → 拟合 Δ读回/Δ设点

只写 B3=1。下发前先读 27/35，任一目标落在行程外就整个退出（不夹取、不截断）。
结束回到起始设点。

用法：python tools/xmt_scale.py
"""
from __future__ import annotations

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

ALLOW = frozenset((xp.CMD_SET_POSITION,))
STEPS = (1.0, 2.0, 4.0)      # µm，累计位移最大 7 µm


def read_pos(link):
    f = link.ask(link.cmd(xp.CMD_READ_POSITION, bytes((0,))), xp.CMD_READ_POSITION, wait=0.3)
    return f.value if f is not None else None


def find_port(explicit):
    if explicit:
        return explicit
    for p in list_ports.comports():
        if (p.vid, p.pid) == (VID, PID):
            return p.device
    return None


def wait_still(link, window=6, tol=0.4, timeout=10.0):
    """等读数不动（读回单位）。返回 (稳定均值, 耗时, 读取次数)。"""
    t0 = time.monotonic()
    hist = []
    while time.monotonic() - t0 < timeout:
        x = read_pos(link)
        if x is None:
            continue
        hist.append(x)
        w = hist[-window:]
        if len(w) == window and max(w) - min(w) < tol:
            return statistics.mean(w), time.monotonic() - t0, len(hist)
    return None, time.monotonic() - t0, len(hist)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    port = find_port(None)
    if not port:
        print("没找到设备")
        return 2

    link = Link(port)
    try:
        if link.ask(link.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.5) is None:
            print("握手失败")
            return 1
        fl = link.ask(link.cmd(xp.CMD_READ_LOOP_MODE, bytes((0,))), xp.CMD_READ_LOOP_MODE)
        if not (fl is not None and fl.data[1:2] == b"C"):
            print(f"19 = {fl.data[1:2] if fl is not None else None!r}，不是闭环，退出")
            return 1
        hi = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_HIGH, bytes((0,))),
                      xp.CMD_READ_POS_LIMIT_HIGH).value
        lo = link.ask(link.cmd(xp.CMD_READ_POS_LIMIT_LOW, bytes((0,))),
                      xp.CMD_READ_POS_LIMIT_LOW).value
        print(f"行程（27/35，本就是 µm）{lo:.4f} ~ {hi:.4f}\n")

        r0, dt, n = wait_still(link)
        if r0 is None:
            print("起始读数就没静下来")
            return 1
        print(f"起始读回 {r0:.4f}（{dt:.1f} s / {n} 次）")

        # 读回单位下的当前值 → 设点单位（µm）。这一步只是在假设下取起点，
        # 后面的斜率是相对的，不依赖假设是否精确。
        p0 = r0 * 0.75
        print(f"按 0.75 折算，起始真实位置 ≈ {p0:.4f} µm\n")

        # 安全边界：任何目标越界就整个退出，不夹取 —— 夹取会让斜率量错。
        targets = [p0] + [p0 + s for s in STEPS]
        if min(targets) < lo or max(targets) > hi:
            print(f"目标 {min(targets):.4f} ~ {max(targets):.4f} µm 越出行程 "
                  f"{lo:.4f} ~ {hi:.4f} µm，退出")
            return 1

        pts = [(0.0, r0)]
        for s in STEPS:
            tgt = p0 + s
            link.send(link.cmd(xp.CMD_SET_POSITION, bytes((0,)) + xp.encode_value(tgt)),
                      allow=ALLOW)
            r, dt, n = wait_still(link)
            if r is None:
                print(f"  设点 {tgt:.4f}：没静下来，停止")
                break
            print(f"  设点 {tgt:.4f}（{p0:.4f}+{s:g}） → 读回 {r:.4f}"
                  f"  Δ读回 {r - r0:+.4f}  ({dt:.1f} s / {n} 次)")
            pts.append((s, r))
            time.sleep(0.3)

        if len(pts) >= 3:
            ds = [s for s, _ in pts[1:]]
            dr = [r - pts[0][1] for _, r in pts[1:]]
            slopes = [d / s for s, d in zip(ds, dr) if s]
            k = statistics.mean(slopes)
            print(f"\n== 结果 ==")
            print(f"  各段斜率 Δ读回/Δ设点 = {['%.5f' % v for v in slopes]}")
            print(f"  平均 {k:.5f}   → 读回单位 = {1 / k:.5f} µm   → 真实位置 = 读回 × {1 / k:.5f}")
            print(f"  4/3 = 1.33333。27 读到的高限 {hi:.4f} **就是 µm**，不再乘 "
                  f"{1 / k:.5f}（乘出来那个 {hi * (1 / k):.4f} 是判别用的反证假设，"
                  f"docs/xmt/README.md 里已证伪）")
            if 1.32 < k < 1.35:
                print("  与 4/3 一致：设点是 µm，读回要乘 0.75")
            else:
                print(f"  ! 偏离 4/3 较多，别急着用，先复核")

        print(f"\n回到起始设点 {p0:.4f}")
        link.send(link.cmd(xp.CMD_SET_POSITION, bytes((0,)) + xp.encode_value(p0)), allow=ALLOW)
        r, _, _ = wait_still(link)
        print(f"  回到读回 {r if r is None else round(r, 4)}")
    except serial.SerialException as exc:
        print(f"串口出错：{exc}")
        return 2
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
