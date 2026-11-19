"""索雷博 CS165MU「一次触发一帧」上机探针（**不碰位移台**）。

为什么要它：扫描现在是「到位 + 稳定 → 触发 → 等曝光 → 才允许移动」，严格性的全部前提
是相机的单帧触发模式真的成立：

  * 一次触发只出一帧；
  * 帧在曝光结束后才交付（触发→帧 ≈ 曝光 + 读出）；
  * **上一帧还在读出时补一发触发会排队，不会丢** —— 扫描的流水线（先触发下一点、
    再取上一点的帧）就靠这一点；若相机不排队，扫描会卡在等帧上。

这些只能上机量，离线用例钉不住。跑之前确认没有别的进程占着相机
（服务端别开预览；扫描进行中不能跑）。跑完自动把会话（句柄 + SDK）收掉。

**实测结论（2026-11-13，CS165MU 32709；详见 docs/thorlabs/设备认识账.xml E11）**：

* 触发→帧 = 曝光 + 固定延迟：arm 后首发 +36 ms，此后 **+79 ms**（曝光 12/50/200 ms 一致）；
* arm 之后不触发：队列 0 帧（首点不会拿到预触发的旧帧）；
* 串行 20 发触发取帧稳定、不停摆；
* **曝光中途补触发会被丢掉**（只到一帧）—— 上一帧没交付时不能补发；
* 曝光结束后的读出期间补触发会被接受（两帧间隔 = 曝光 + 26 ms），但**没有证明**它从哪一刻
  开始曝光，所以代码不拿它做提前触发的优化：扫描一律「等帧交付再移动」。

**2026-11-14 补**（认识账 E12）：长扫描偶发"等帧 30 s 超时"，查下来不是取晚了 ——
retain 模式证明帧在队列里能留 30 s 以上，是**这一发触发没出帧**。所以：
retain 看"等不到帧"到底是丢触发还是丢帧；scanpath 用真实的 _grab_frame 跑扫描的相机路径
（含补发与排空），触发次数减点数 = 用了几次补发。

    python tools/thorlabs_scan_probe.py all --exposure-us 200000
    python tools/thorlabs_scan_probe.py stream --exposure-us 50000 --points 100
    python tools/thorlabs_scan_probe.py retain
    python tools/thorlabs_scan_probe.py scanpath --exposure-us 300000 --points 600 --delay-ms 30 --period-ms 560

注意：探针会显式写曝光（相机掉电保持），下一次打开相机会按配置/界面值重写，不影响数据。
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.thorlabs_ccd import ThorlabsCamera, _Session  # noqa: E402

log = logging.getLogger("scan-probe")


def wait_frame(sess: _Session, timeout: float = 30.0):
    """等一帧；返回 (frame, 拿到时刻)。超时返回 (None, 时刻)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = sess.cam.get_pending_frame_or_null()
        if frame is not None:
            return frame, time.monotonic()
        time.sleep(0.002)
    return None, time.monotonic()


def _stats(ms: list[float]) -> str:
    if not ms:
        return "—"
    return (f"min {min(ms):.0f} / 中位 {statistics.median(ms):.0f} / max {max(ms):.0f} ms")


def mode_arm(cam: ThorlabsCamera, sess: _Session, exposure_ms: float) -> None:
    """arm 之后不触发：应该一帧都不出（首点不会拿到预触发的旧帧）。"""
    print("== arm 后不预触发 ==", flush=True)
    time.sleep(min(2.0, max(0.5, exposure_ms * 2 / 1000)))
    frame = sess.cam.get_pending_frame_or_null()
    print("  队列里" + ("**有帧（错：arm 时就曝光了）**" if frame is not None else "没有帧（对）"),
          flush=True)


def mode_latency(cam: ThorlabsCamera, sess: _Session, exposure_us: int, n: int = 10) -> None:
    """逐发触发：触发→帧 应 ≈ 曝光 + 读出（固定延迟）。"""
    exp_ms = exposure_us / 1000
    print(f"== latency：{n} 发，曝光 {exp_ms:.0f} ms，触发→帧 应 ≈ 曝光 + 读出 ==", flush=True)
    lat = []
    for k in range(n):
        t0 = time.monotonic()
        cam._fire(sess)
        frame, t1 = wait_frame(sess)
        if frame is None:
            print(f"  第 {k} 发没等到帧（**触发丢了？**）", flush=True)
            return
        ms = (t1 - t0) * 1000
        lat.append(ms)
        if k < 3 or k >= n - 3:
            print(f"  第 {k} 发：{ms:.0f} ms（= 曝光 {exp_ms:.0f} + 读出 {ms - exp_ms:.0f}）",
                  flush=True)
    print(f"  统计：{_stats(lat)}；读出估计 ≈ {statistics.median(lat) - exp_ms:.0f} ms", flush=True)


def mode_pending(cam: ThorlabsCamera, sess: _Session, exposure_us: int) -> None:
    """上一帧还没取走就补一发触发：两帧都要完整到达、且第二帧排在后面。

    扫描的流水线就是「先触发下一点 → 再取上一点的帧」。如果相机不排队，这里会
    等不到第二帧或拿到的帧错位。
    """
    exp_ms = exposure_us / 1000
    print(f"== pending：曝光 {exp_ms:.0f} ms，半曝光时补第二发触发 ==", flush=True)
    tA = time.monotonic()
    cam._fire(sess)
    time.sleep(exp_ms / 2000)                    # 半曝光
    tB = time.monotonic()
    cam._fire(sess)
    fA, t1 = wait_frame(sess)
    fB, t2 = wait_frame(sess)
    if fA is None or fB is None:
        print(f"  只拿到 {'A' if fA else ''}{'B' if fB else ''} —— **相机不排队，流水线不成立**",
              flush=True)
        return
    print(f"  第一帧：触发 A 后 {(t1 - tA) * 1000:.0f} ms（应 ≈ {exp_ms:.0f} + 读出）", flush=True)
    print(f"  第二帧：触发 B 后 {(t2 - tB) * 1000:.0f} ms，两帧间隔 {(t2 - t1) * 1000:.0f} ms",
          flush=True)
    print("  两帧都到了、顺序是 A→B（对）", flush=True)


def mode_readout(cam: ThorlabsCamera, sess: _Session, exposure_us: int) -> None:
    """曝光已经结束、帧还在读出/传输时补一发触发：能排队的话点周期可以少等一个读出。

    扫描的「快」就快在这里：曝光一结束就移向下一点，到位后立刻触发下一点，
    趁上一帧还在读出。若这一发被丢掉，就只能等上一帧交付完再触发（每点多付一个读出）。
    """
    exp_ms = exposure_us / 1000
    print(f"== readout：曝光 {exp_ms:.0f} ms，曝光结束后（读出中）补一发触发 ==", flush=True)
    tA = time.monotonic()
    cam._fire(sess)
    time.sleep(exp_ms / 1000 + 0.010)             # 曝光已结束，读出/传输进行中
    tB = time.monotonic()
    cam._fire(sess)
    fA, t1 = wait_frame(sess)
    if fA is None:
        print("  第一帧都没等到 —— **异常**", flush=True)
        return
    print(f"  触发 A→帧 A：{(t1 - tA) * 1000:.0f} ms（应 ≈ {exp_ms:.0f} + 读出）", flush=True)
    fB, t2 = wait_frame(sess, timeout=3.0)
    if fB is None:
        print("  读出期间补的触发被丢掉了 —— 点周期必须等上一帧交付完", flush=True)
        return
    print(f"  触发 B 被接受：B→帧 B {(t2 - tB) * 1000:.0f} ms（≈ {exp_ms:.0f} + 读出 说明没重叠；"
          f"更短说明曝光与读出重叠，可以更快）", flush=True)
    print(f"  两帧间隔 {(t2 - t1) * 1000:.0f} ms", flush=True)


def mode_window(cam: ThorlabsCamera, sess: _Session, exposure_us: int) -> None:
    """找**真正的曝光结束点**：触发在曝光中被丢、在读出中被接受。

    扫描的移动时刻是按「触发 + 曝光」算的；如果相机在触发之后还先等一段才开始积分，
    按"触发 + 曝光"移动就会动在积分里。这里用"A 触发后第 d 毫秒补一发 B"扫一遍，
    被接受的第一个 d 就是曝光真正结束的位置（相对触发）。
    """
    exp_ms = exposure_us / 1000
    print(f"== window：曝光 {exp_ms:.0f} ms，找 A 之后下一发被接受的时刻 ==", flush=True)
    for d in (-100, -50, -20, 0, 20, 40, 50, 60, 70, 80, 100, 130):
        cam._fire(sess)                               # A
        time.sleep(max(0.0, (exp_ms + d) / 1000))
        cam._fire(sess)                               # B
        fA, _ = wait_frame(sess, timeout=5.0)
        fB, _ = wait_frame(sess, timeout=1.5)
        tag = "**被接受**" if fB is not None else "被丢掉"
        print(f"  B 在 A 触发后 {exp_ms + d:.0f} ms（d={d:+d}）：{tag}", flush=True)


def mode_stream(cam: ThorlabsCamera, sess: _Session, exposure_us: int, n: int) -> None:
    """连续 n 点：每点触发→等帧，看会不会停摆、延迟稳不稳（模拟扫描）。"""
    exp_ms = exposure_us / 1000
    print(f"== stream：{n} 点，曝光 {exp_ms:.0f} ms，逐点触发→取帧 ==", flush=True)
    lat, gaps = [], []
    prev = None
    t_start = time.monotonic()
    for k in range(n):
        t0 = time.monotonic()
        cam._fire(sess)
        frame, t1 = wait_frame(sess)
        if frame is None:
            print(f"  第 {k} 点没等到帧 —— **停摆了**", flush=True)
            return
        lat.append((t1 - t0) * 1000)
        if prev is not None:
            gaps.append((t0 - prev) * 1000)
        prev = t0
        if (k + 1) % max(1, n // 10) == 0:
            print(f"  {k + 1}/{n} 点：等帧 {lat[-1]:.0f} ms，点周期 {gaps[-1] if gaps else 0:.0f} ms",
                  flush=True)
    total = time.monotonic() - t_start
    print(f"  等帧：{_stats(lat)}", flush=True)
    print(f"  点周期：{_stats(gaps)}；总 {total:.1f} s / {n} 点 = {total / n * 1000:.0f} ms/点",
          flush=True)


def mode_retain(cam: ThorlabsCamera, sess: _Session, delays: list[float]) -> None:
    """触发后**故意不取帧**，隔 d 秒再取：帧还在不在队列里。

    这决定"等不到帧"该怎么解释：帧还在 → 是触发没出帧；帧没了 → 是取晚了/驱动丢帧。
    实测 2026-11-14：0.02~30 s 都还在（见认识账 E12）。
    """
    print("== retain：触发后等 d 秒再取帧 ==", flush=True)
    for d in delays:
        cam._drain_frames(sess)
        t0 = time.monotonic()
        cam._fire(sess)
        time.sleep(d)
        frame = sess.cam.get_pending_frame_or_null()
        print(f"  d={d:6.1f}s -> {'FRAME' if frame is not None else 'NO FRAME'} "
              f"(总 {time.monotonic() - t0:.2f}s)", flush=True)
        time.sleep(0.05)


def mode_scanpath(cam: ThorlabsCamera, sess: _Session, exposure_us: int, n: int,
                  delay_ms: float, period_ms: float) -> None:
    """按扫描的真实相机路径跑：触发 → 模拟台子读数 → _grab_frame（含补发与排空）。

    触发次数减点数 = 用了几次补发；_grab_frame 自己会把"第 N 点 … 补发一发触发"
    写进日志。点周期按 period_ms 走，尽量贴近扫描的节奏。
    """
    exp_ms = exposure_us / 1000
    print(f"== scanpath：{n} 点，曝光 {exp_ms:.0f} ms，触发后等 {delay_ms:.0f} ms、"
          f"点周期 {period_ms:.0f} ms；帧时间 {sess.frame_time_us} µs，"
          f"等帧预算 {cam._frame_wait_s(sess):.2f} s ==", flush=True)
    fired = 0
    orig_fire = cam._fire

    def counted_fire(sess_: _Session):
        nonlocal fired
        fired += 1
        return orig_fire(sess_)

    cam._fire = counted_fire                       # type: ignore[method-assign]
    t_start = time.monotonic()
    try:
        for k in range(n):
            t0 = time.monotonic()
            cam._fire(sess)
            time.sleep(delay_ms / 1000.0)
            try:
                cam._grab_frame(0, k, 0.0, save=False)
            except Exception as exc:               # noqa: BLE001
                print(f"  *** 第 {k} 点采不到帧：{exc}", flush=True)
                return
            if (k + 1) % max(1, n // 10) == 0:
                print(f"  {k + 1}/{n} 点，触发 {fired} 次"
                      f"（补发 {fired - (k + 1)} 次），{time.monotonic() - t_start:.0f} s",
                      flush=True)
            dt = t0 + period_ms / 1000.0 - time.monotonic()
            if dt > 0:
                time.sleep(dt)
    finally:
        cam._fire = orig_fire                      # type: ignore[method-assign]
    print(f"  完成 {n} 点，触发 {fired} 次（补发 {fired - n} 次），"
          f"总 {time.monotonic() - t_start:.1f} s", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="CS165MU 单帧触发探针（不碰位移台）")
    ap.add_argument("mode", choices=("arm", "latency", "pending", "readout", "window", "stream",
                                     "retain", "scanpath", "all"),
                    help="要跑哪一项；all = arm + latency + pending + readout + stream(20 点)")
    ap.add_argument("--exposure-us", type=int, default=200_000, help="曝光（µs），默认 200 ms")
    ap.add_argument("--points", type=int, default=100, help="stream / scanpath 点数，默认 100")
    ap.add_argument("--delays", default="0.02,0.1,0.5,1,2,5,10,30",
                    help="retain 模式的等待秒数（逗号分隔）")
    ap.add_argument("--delay-ms", type=float, default=30.0,
                    help="scanpath 模式：触发后到取帧之间的等待（模拟台子读数），默认 30 ms")
    ap.add_argument("--period-ms", type=float, default=560.0,
                    help="scanpath 模式的点周期，默认 560 ms")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cam = ThorlabsCamera(exposure_us=args.exposure_us)
    sess = None
    try:
        t0 = time.monotonic()
        sess = cam._open_session(0.0, single_frame=True)
        cam._sess = sess
        exposure_us = int(sess.cam.exposure_time_us)     # 相机读回值，下面全按它算
        print(f"会话已建（单帧触发，读回曝光 {exposure_us} µs，"
              f"{time.monotonic() - t0:.1f} s）", flush=True)
        if exposure_us != args.exposure_us:
            print(f"  注意：请求 {args.exposure_us} µs，读回 {exposure_us} µs（按读回值算）",
                  flush=True)
        cam._ensure_armed(sess)
        if args.mode in ("arm", "all"):
            mode_arm(cam, sess, exposure_us / 1000)
        if args.mode in ("latency", "all"):
            mode_latency(cam, sess, exposure_us)
        if args.mode in ("pending", "all"):
            mode_pending(cam, sess, exposure_us)
        if args.mode in ("readout", "all"):
            mode_readout(cam, sess, exposure_us)
        if args.mode == "window":
            mode_window(cam, sess, exposure_us)
        if args.mode == "stream":
            mode_stream(cam, sess, exposure_us, args.points)
        if args.mode == "retain":
            mode_retain(cam, sess, [float(x) for x in args.delays.split(",")])
        if args.mode == "scanpath":
            mode_scanpath(cam, sess, exposure_us, args.points, args.delay_ms, args.period_ms)
        if args.mode == "all":
            mode_stream(cam, sess, exposure_us, min(20, args.points))
        return 0
    finally:
        if sess is not None:
            cam._discard("探针收工", failed=False)


if __name__ == "__main__":
    raise SystemExit(main())
