"""静止波形：台子不动，用 1 ms 周期推送抓几秒读数，看"噪声底"到底是什么。

阶段 2.5 用 50 ms 轮询量到约 0.18 µm 极差 —— 但 50 ms 采样分不出它是
**传感器噪声、蠕变漂移，还是极限环**。而这三者对应的处置完全不同：

    噪声   → 等等待时长（稳定延时）就够，读回确认的容差按它定
    漂移   → 等多久都白等：位置是**采集时读回来的**，漂多少就记多少
    极限环 → 台子在振，图会糊 —— 加长稳定延时也救不了，要看曲线

1 ms 采样能直接看见波形。只读：只发 8（启动推送）、11（停止）、77（心跳）。

用法：
    python tools/xmt_wave.py                 # 5 秒，1 ms
    python tools/xmt_wave.py --seconds 10 --period 1
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import serial

from backend import xmt_protocol as xp
from xmt_check import VID, PID, Link, find_port


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="静止波形采集（只读）")
    ap.add_argument("--port")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--period", type=int, default=1, help="推送周期 ms，1~255")
    args = ap.parse_args()
    if not 1 <= args.period <= 255:
        print(f"--period 是 1~255 ms（协议表），给的是 {args.period}")
        return 2

    port = find_port(args.port)
    if not port:
        print(f"没找到 VID:PID={VID:04X}:{PID:04X}，也没用 --port 指定")
        return 2
    link = Link(port)
    try:
        f = link.ask(link.cmd(xp.CMD_HANDSHAKE), xp.CMD_HANDSHAKE, wait=0.5)
        if f is None or f.data != b"OK":
            print("握手失败，先跑 tools/xmt_check.py")
            return 1
        print(f"已连上 {port} @ {link.baud} 8N1，地址 {link.addr}（只读）\n")

        link.drain()
        link.send(link.cmd(xp.CMD_STREAM_POSITION, bytes((0, args.period))))
        samples = link.poll(args.seconds)
        link.send(link.cmd(xp.CMD_STOP_STREAM))
        link.poll(0.3)

        pairs = [(t, f.value) for t, f in samples
                 if f.b3 == xp.CMD_STREAM_POSITION and f.value is not None]
        if len(pairs) < 10:
            print(f"只收到 {len(pairs)} 个有效包，没法分析")
            return 1

        vals = [v for _, v in pairs]
        t0 = pairs[0][0]
        span = pairs[-1][0] - t0
        diffs = [abs(b - a) for a, b in zip(vals, vals[1:])]
        print(f"样本 {len(vals)} 个 / {span:.2f} s / 请求周期 {args.period} ms"
              f"（实测中位 {statistics.median(
                  [(b - a) * 1000 for a, b in zip([t for t, _ in pairs], [t for t, _ in pairs][1:])]
              ):.2f} ms）")
        print(f"均值 {statistics.mean(vals):.4f}   σ {statistics.pstdev(vals):.4f}")
        print(f"最小 {min(vals):.4f}   最大 {max(vals):.4f}   峰峰 {max(vals) - min(vals):.4f}")
        print(f"相邻 |Δ| ：中位 {statistics.median(diffs):.4f}   "
              f"90分位 {sorted(diffs)[int(len(diffs) * 0.9)]:.4f}   最大 {max(diffs):.4f}")

        # 分段均值：看它是在原地抖，还是在慢慢漂
        nseg = 5
        seg = max(1, len(vals) // nseg)
        means = [statistics.mean(vals[i:i + seg]) for i in range(0, len(vals) - seg + 1, seg)]
        print(f"分 {len(means)} 段均值：" + "  ".join(f"{m:.4f}" for m in means))
        print(f"  段间极差 {max(means) - min(means):.4f}   段内平均 σ "
              f"{statistics.mean([statistics.pstdev(vals[i:i + seg]) for i in range(0, len(vals) - seg + 1, seg)]):.4f}")

        print("\n前 40 个读数：")
        for i in range(0, min(40, len(vals)), 8):
            print("  " + "  ".join(f"{v:.4f}" for v in vals[i:i + 8]))

        print("\n判读：")
        print("  段内 σ 大、段间极差小  → 传感器噪声")
        print("  段内 σ 小、段间极差大  → 蠕变/漂移")
        print("  读数在两个值之间来回  → 极限环/振荡")
    except serial.SerialException as exc:
        print(f"串口出错：{exc}")
        return 2
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
