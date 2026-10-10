"""索雷博 CS165MU 连续出帧探针：这台相机的**流稳定性**实验台（上机跑，不碰位移台）。

为什么要这个脚本：2026-10-08 定位「预览中途保存原生帧，预览就断」时，靠它把原因缩到了
**同一个会话里第二次 arm**：

    python tools/thorlabs_stream_probe.py steady      # 基线：预览在不在出帧
    python tools/thorlabs_stream_probe.py cycles 2    # 在预览跑着时做 2 轮 disarm→arm
    python tools/thorlabs_stream_probe.py backlog     # 停取帧 3 s 后队列里积了几帧

实测结论（本机 CS165MU 34331，2026-10-08）：

* **一轮** disarm→arm：出帧不受影响；
* **两轮**（以及更多轮）disarm→arm：第二轮之后**一帧都不再出**，1.4 s 后按"没出新帧"判掉线。
  设置写与取帧都不是必要条件（两轮 arm 里一个设置不写、一帧不取，照样停摆）；
  单独写 ROI / 曝光 / 增益、单独取帧，都无害。
* 停取帧 3 s 后帧队列里 **0 帧**：没人取帧时驱动直接丢帧、不攒 —— 所以扫描接手时不需要清队列。

因此设备层的规矩是「**一个会话最多 arm 一次**，设置只在会话出生时写一次」
（backend/thorlabs_ccd.py 文件头第 5 条、docs/thorlabs/设备认识账.xml E10）。

注意：`cycles` 模式**会**把预览弄停（那正是它要复现的现象），跑完要重开相机。
跑之前先让服务端把相机放掉：POST /api/ccd/preview?on=false。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.thorlabs_ccd import ThorlabsCamera                    # noqa: E402

log = logging.getLogger("stream-probe")


def _frames(cam: ThorlabsCamera) -> int:
    return cam.status()["frames"]


def _observe(cam: ThorlabsCamera, label: str, secs: float = 3.0) -> int:
    """看 secs 秒里出没出帧；返回新增帧数。"""
    a = _frames(cam)
    time.sleep(secs)
    b = _frames(cam)
    st = cam.status()
    print(f"  [{label}] 预览 {b - a:+d} 帧 / {secs:.1f}s  "
          f"状态={st['state']} failure={st['failure']!r}", flush=True)
    return b - a


def _cycle(sess) -> None:
    """一轮 disarm→arm（就是老代码在保存/交接时干的事）。调用方保证在 owner 线程里跑。"""
    if sess.armed:
        sess.cam.disarm()
        sess.armed = False
    time.sleep(0.05)
    sess.cam.arm(2)
    sess.cam.issue_software_trigger()
    sess.last_trigger = time.monotonic()
    sess.armed = True


def mode_steady(cam: ThorlabsCamera) -> int:
    print("== steady：开预览，看 5 s 出帧 ==", flush=True)
    cam.preview(True)
    time.sleep(1.5)
    n = _observe(cam, "预览", 5.0)
    cam.preview(False)
    print("  结论：", "在正常出帧" if n > 0 else "**一帧都没出**", flush=True)
    return 0 if n > 0 else 1


def mode_backlog(cam: ThorlabsCamera, pause: float = 3.0) -> int:
    print(f"== backlog：停取帧 {pause:.0f} s，数队列里积了几帧 ==", flush=True)
    cam.preview(True)
    time.sleep(1.5)
    sess = cam._sess
    cam._hold = 1                       # 停取帧（扫描接手就是这么停的），相机继续自由出帧
    time.sleep(pause)
    sess.cam.image_poll_timeout_ms = 1  # 1 ms：把"等下一帧"变成"几乎不等"，才能数积压
    n = 0
    t0 = time.monotonic()
    while sess.cam.get_pending_frame_or_null() is not None and n < 500:
        n += 1
    dt = time.monotonic() - t0
    sess.cam.image_poll_timeout_ms = 2000
    cam._hold = 0
    cam.preview(False)
    print(f"  队列里积了 {n} 帧（数完用了 {dt * 1000:.0f} ms）", flush=True)
    return 0


def mode_cycles(cam: ThorlabsCamera, rounds: int) -> int:
    print(f"== cycles {rounds}：预览跑着的时候做 {rounds} 轮 disarm→arm ==", flush=True)
    cam.preview(True)
    time.sleep(1.5)
    _observe(cam, "动作前", 2.0)
    dead = 0
    for i in range(1, rounds + 1):
        cam._submit(("probe:cycle",), 30)
        if _observe(cam, f"第 {i} 轮之后", 3.0) <= 0:
            dead = i
            break
    cam.preview(False)
    if dead:
        print(f"  结论：第 {dead} 轮 arm 之后不再出帧（与实测一致：一个会话只许 arm 一次）", flush=True)
    else:
        print("  结论：这几轮之后仍在出帧 —— 与 2026-10-08 的实测不符，先别信旧结论", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CS165MU 连续出帧探针")
    ap.add_argument("mode", nargs="?", default="steady",
                    choices=("steady", "backlog", "cycles"))
    ap.add_argument("rounds", nargs="?", type=int, default=2, help="cycles 模式的轮数")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cam = ThorlabsCamera()
    cam.start()
    base_run = cam._run

    def _run(job):                        # 只给 cycles 模式加一个探针动作，别的不动
        if job[0] == "probe:cycle":
            _cycle(cam._sess)
            return None
        return base_run(job)

    cam._run = _run
    try:
        if args.mode == "steady":
            return mode_steady(cam)
        if args.mode == "backlog":
            return mode_backlog(cam)
        return mode_cycles(cam, max(1, args.rounds))
    finally:
        time.sleep(0.3)
        cam.close()


if __name__ == "__main__":
    raise SystemExit(main())
