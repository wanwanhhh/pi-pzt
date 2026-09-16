"""设备层上机冒烟测试：真实驱动 P-621.1CD（行程 100 µm）。

用法（项目根目录）：.venv\\Scripts\\python.exe tools\\hwtest.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.pi_stage import Stage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    if not ok:
        FAIL.append(name)


def show(st) -> str:
    return (
        f"pos={st.position:9.4f} target={st.target:9.4f} ont={int(st.on_target)} "
        f"svo={int(st.servo)} vel={st.velocity:8.1f} err={st.error_code} ovf={int(st.overflow)}"
    )


def run(s: Stage) -> None:
    t0 = time.monotonic()
    s.start()
    print(f"连接耗时 {time.monotonic() - t0:.2f}s")
    st = s.poll()
    print("初始状态", show(st))
    check("识别位移台", st.stage_type == "P-621.1CD", st.stage_type)
    check("行程来自设备", (st.travel_min, st.travel_max) == (0.0, 100.0), f"{st.travel_min}–{st.travel_max}")
    check("伺服保持中", st.servo)
    check("错误码为 0", st.error_code == 0, str(st.error_code))

    print("\n[1] 绝对移动 0 -> 10 µm")
    s.move(10.0)
    t0 = time.monotonic()
    check("到位信号", s.wait_on_target(10.0))
    dt = time.monotonic() - t0
    st = s.poll()
    print("   ", show(st), f"| ONT? 等待 {dt * 1000:.0f} ms")
    check("位置正确", abs(st.position - 10.0) < 0.15, f"{st.position:.4f}")

    print("\n[2] 绝对移动 10 -> 50 µm")
    s.move(50.0)
    t0 = time.monotonic()
    check("到位信号", s.wait_on_target(10.0))
    dt = time.monotonic() - t0
    st = s.poll()
    print("   ", show(st), f"| ONT? 等待 {dt * 1000:.0f} ms")
    check("位置正确", abs(st.position - 50.0) < 0.15, f"{st.position:.4f}")

    print("\n[3] 降速 + 中途停止（STP 保持伺服）")
    s.set_velocity(100.0)
    s.move(0.0)
    time.sleep(0.20)
    s.stop_motion()
    time.sleep(0.10)
    st = s.poll()
    print("   ", show(st))
    check("确实中途停下", 0.0 < st.position < 45.0, f"{st.position:.4f} µm")
    check("伺服未关", st.servo)

    print("\n[4] 相对移动 jog(+2.5 µm)")
    s.set_velocity(10000.0)
    s.move(10.0)
    s.wait_on_target(10.0)
    base = s.poll().position
    s.jog(2.5)
    s.wait_on_target(10.0)
    st = s.poll()
    delta = st.position - base
    print("   ", show(st), f"| 实际位移 {delta:+.4f} µm")
    check("相对位移正确", abs(delta - 2.5) < 0.15, f"{delta:+.4f}")

    print("\n[5] 急停（丢弃队列 + STP）")
    s.set_velocity(100.0)
    s.move(80.0)
    time.sleep(0.25)
    s.estop()
    time.sleep(0.15)
    st = s.poll()
    print("   ", show(st))
    check("急停后停住", st.position < 70.0, f"{st.position:.4f} µm")
    check("伺服保持", st.servo)
    check("急停后仍可通信", st.connected and st.error_code == 0)

    print("\n[6] 遥测轮询速率（目标 10 Hz）")
    n = 30
    t0 = time.perf_counter()
    for _ in range(n):
        s.poll()
    dt = time.perf_counter() - t0
    hz = n / dt
    print(f"    {n} 次 poll 用时 {dt:.2f}s -> {hz:.1f} Hz")
    check("达到 10 Hz", hz >= 9.0, f"{hz:.1f} Hz")

    print("\n[7] 释放伺服（卸力，台子回弹）")
    s.set_velocity(10000.0)
    s.move(50.0)
    s.wait_on_target(10.0)
    s.release()
    time.sleep(0.30)
    st = s.poll()
    print("   ", show(st))
    check("伺服已关", not st.servo)
    check("释放后仍能读位置", st.connected, f"{st.position:.4f} µm")


def main() -> int:
    s = Stage()
    try:
        run(s)
    finally:
        s.shutdown()
        print("已关闭设备（伺服状态按最后一步保持）")
    print(f"\n===== {len(FAIL)} 项失败 =====" if FAIL else "\n===== 全部通过 =====")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
