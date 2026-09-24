"""一次性上机探测：**SDK 实例寿命 vs 相机句柄寿命**（拔插实验）。

设计里唯一没量的那个假设就靠它回答（见 docs/thorlabs/设备认识账.xml E9）：

  ① 同一 SDK 实例，拔掉再插回之后 discover 还看不看得见设备？
  ② 拔插前拿到的旧句柄，插回后能不能自愈？
  ③ 相机级 open_camera / dispose 各要多久？SDK dispose 之后还能不能再建实例？
  ④ 相机级 open/dispose 会不会让 USB 重新枚举（看 PnP 的 LastArrivalDate）？

**运行期间独占相机**：先把后端停掉（相机同一时刻只能有一个占用者）。
每一步都带超时保护：卡住就记「卡住」并跳到汇总，不让一次调用把整个探测拖死。
输出实时写 data/replug_probe.txt（崩了也留证据）。

用法：
    .venv\Scripts\python.exe tools\thorlabs_replug_probe.py              # 按提示倒计时拔/插
    .venv\Scripts\python.exe tools\thorlabs_replug_probe.py --interactive # 每步等你按回车
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.thorlabs_ccd import _require_dll_dir  # noqa: E402

OUT = ROOT / "data" / "replug_probe.txt"
_lines: list[str] = []
_hung = False


def say(msg: str = "") -> None:
    print(msg, flush=True)
    _lines.append(msg)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(_lines) + "\n", encoding="utf-8")


def window(seconds: int, what: str, interactive: bool) -> None:
    if interactive:
        say(f"\n★ {what} —— 好了按回车")
        input()
        return
    say(f"\n★ {what}（给你 {seconds} 秒）")
    for left in range(seconds, 0, -10):
        say(f"   …还有 {left} 秒")
        time.sleep(min(10, left))


def probe(label: str, fn, timeout: float = 10.0):
    """跑一步 SDK 调用，带超时。卡住就记下来并返回 None（后续步骤由调用方决定跳过）。"""
    global _hung
    box: list = []

    def run() -> None:
        try:
            box.append(("ok", fn()))
        except BaseException as exc:                      # noqa: BLE001 - 探测：什么都记
            box.append(("err", f"{type(exc).__name__}: {str(exc)[:200]}"))

    t0 = time.perf_counter()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    dt = (time.perf_counter() - t0) * 1000
    if not box:
        _hung = True
        say(f"[{label}] ⚠ 超过 {timeout:.0f} s 没返回 —— 记为「卡住」，后面的步骤跳过（{dt:.0f} ms）")
        return None
    kind, payload = box[0]
    if kind == "err":
        say(f"[{label}] 异常（{dt:.0f} ms）：{payload}")
        return None
    say(f"[{label}] 返回 {payload!r}（{dt:.0f} ms）")
    return payload


def pnp_arrival() -> str:
    """相机在 USB 上的「最近到达时间」——变了就说明重新枚举过。"""
    ps = (
        "$d = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_1313&PID_4002' };"
        "if (-not $d) { 'absent' } else {"
        " (Get-PnpDeviceProperty -InstanceId $d.InstanceId"
        " -KeyName 'DEVPKEY_Device_LastArrivalDate').Data }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25)
        return (out.stdout or "").strip() or "?"
    except Exception as exc:                              # noqa: BLE001
        return f"取不到：{exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interactive", action="store_true", help="每步等你按回车，不按倒计时")
    ap.add_argument("--keep-open", action="store_true", help="结尾不 dispose，留着句柄便于再手动试")
    ap.add_argument("--stepwise", action="store_true", help="常驻进程，命令写 data/probe_cmd.txt（你下命令我在动）")
    args = ap.parse_args()
    interactive = args.interactive
    if args.stepwise:
        return stepwise()

    say("=" * 72)
    say("拔插实验：同一 SDK 实例 / 旧句柄，在拔掉-插回之后还能不能用")
    say("=" * 72)

    try:
        dll = _require_dll_dir()
        say(f"DLL 已按全路径预加载：{dll}")
    except Exception as exc:                              # noqa: BLE001
        say(f"❌ DLL 预加载失败：{exc}")
        return 2

    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

    say(f"USB 到达时间（开始）：{pnp_arrival()}")
    sdk = probe("① 建 SDK 实例", TLCameraSDK, timeout=30)
    if sdk is None:
        say("SDK 建不起来，探测到此为止")
        return 2
    serials = probe("② discover（插着）", sdk.discover_available_cameras)
    if not serials:
        say("没发现相机 —— 确认相机插着、ThorCam 没开着、后端没在跑")
        return 2
    serial = serials[0]

    t0 = time.perf_counter()
    cam = probe("③ open_camera", lambda: sdk.open_camera(serial), timeout=30)
    if cam is None:
        return 2
    say(f"   相机级 open 用了 {(time.perf_counter() - t0) * 1000:.0f} ms")
    probe("④ 读型号", lambda: cam.model)
    probe("⑤ 读 ROI（健康时）", lambda: tuple(cam.roi))
    say(f"USB 到达时间（open 之后）：{pnp_arrival()}")

    window(45, "保持插着别动", interactive)

    # ---- 拔掉 ----
    window(70, "★ 现在把相机 USB 拔掉", interactive)
    say(f"\nUSB 到达时间（拔掉后）：{pnp_arrival()}")
    probe("⑥ 拔掉后 discover", sdk.discover_available_cameras)
    probe("⑦ 拔掉后旧句柄读 ROI", lambda: tuple(cam.roi))

    # ---- 插回 ----
    window(70, "★ 现在把相机插回去（插好就别动了）", interactive)
    say(f"\nUSB 到达时间（插回后）：{pnp_arrival()}")

    say("\n---- 插回之后：同一个 SDK 实例 ----")
    again = probe("⑧ 插回后 discover（同一 SDK 实例）", sdk.discover_available_cameras)
    healed = probe("⑨ 插回后旧句柄能不能自愈（读 ROI）", lambda: tuple(cam.roi))
    say(f"   → 旧句柄自愈：{'能' if healed else '不能'}")

    say("\n---- 丢弃旧句柄，换新句柄 ----")
    t0 = time.perf_counter()
    probe("⑩ 旧句柄 dispose", cam.dispose, timeout=30)
    say(f"   旧句柄 dispose 用了 {(time.perf_counter() - t0) * 1000:.0f} ms")
    say(f"USB 到达时间（dispose 之后）：{pnp_arrival()}")

    t0 = time.perf_counter()
    cam2 = probe("⑪ 换新句柄 open_camera", lambda: sdk.open_camera(serial), timeout=30)
    if cam2 is not None:
        say(f"   相机级 open 用了 {(time.perf_counter() - t0) * 1000:.0f} ms")
        probe("⑫ 新句柄读 ROI", lambda: tuple(cam2.roi))
        probe("⑬ 新句柄抓一帧", lambda: cam2.get_pending_frame_or_null() is not None)

    if not args.keep_open:
        if cam2 is not None:
            probe("⑭ 新句柄 dispose", cam2.dispose, timeout=30)
        t0 = time.perf_counter()
        probe("⑮ SDK dispose", sdk.dispose, timeout=30)
        say(f"   SDK dispose 用了 {(time.perf_counter() - t0) * 1000:.0f} ms")
        sdk2 = probe("⑯ SDK dispose 后再建实例（闩锁清了吗）", TLCameraSDK, timeout=30)
        if sdk2 is not None:
            probe("⑰ 再 discover", sdk2.discover_available_cameras)

    say("\n" + "=" * 72)
    say("汇总要看的四件事：")
    say("  ① ⑧ 插回后同一 SDK 实例的 discover 有没有看见设备 → 决定 reopen 要不要重建 SDK")
    say("  ② ⑨ 旧句柄自不自愈 → 决定「一律丢弃」是不是必须的")
    say("  ③ ⑩⑪ 旧句柄 dispose / 新句柄 open 的耗时 → 决定会话粒度值不值")
    say("  ④ ⑮⑯ SDK dispose 之后还能不能建实例 → 决定 reopen 敢不敢碰 SDK")
    say(f"（本次是否有步骤卡住：{'有' if _hung else '没有'}）")
    say(f"完整记录：{OUT}")
    return 0




# ---------------------------------------------------------------- 逐步模式
# 「你下命令我在动」：探测进程常驻，命令写进 data/probe_cmd.txt（一行一个），
# 探测按行执行、把结果追加到 data/replug_probe.txt。这样拔插的时机由人掌握，
# 不靠倒计时窗口，也不需要在探测进程里敲键盘。
STEP_HELP = """可用命令：
  probe         读一次现状（discover / 旧句柄 ROI / USB 到达时间）
  pnp           只读 USB 到达时间
  dispose-old   dispose 旧句柄（计时）
  open-new      open_camera 换新句柄 + 读 ROI + 抓一帧（计时）
  dispose-new   dispose 新句柄（计时）
  dispose-sdk   dispose SDK（计时）
  reinit        再建一个 SDK 实例 + discover（看闩锁清没清）
  quit          结束
"""


def stepwise() -> int:
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

    cmd_path = ROOT / "data" / "probe_cmd.txt"
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    if cmd_path.exists():
        cmd_path.unlink()

    say("=" * 72)
    say("逐步探测（常驻进程）：命令写 data/probe_cmd.txt，结果追加到本文件")
    say("=" * 72)
    try:
        say(f"DLL 已按全路径预加载：{_require_dll_dir()}")
    except Exception as exc:                              # noqa: BLE001
        say(f"❌ DLL 预加载失败：{exc}")
        return 2
    say(STEP_HELP)

    st: dict = {"sdk": None, "cam": None, "cam2": None, "serial": None}

    say(f"USB 到达时间（开始）：{pnp_arrival()}")
    st["sdk"] = probe("① 建 SDK 实例", TLCameraSDK, timeout=30)
    if st["sdk"] is None:
        say("SDK 建不起来，结束")
        return 2
    serials = probe("② discover", st["sdk"].discover_available_cameras)
    if serials:
        st["serial"] = serials[0]
        st["cam"] = probe("③ open_camera（旧句柄）", lambda: st["sdk"].open_camera(st["serial"]), 30)
        if st["cam"] is not None:
            probe("④ 读 ROI（健康时）", lambda: tuple(st["cam"].roi))
    else:
        say("② 现在没发现设备（不在）—— 直接等命令；插回来之后用 open-new 打开")
    say("\n[就绪] 等我下命令（写到 data/probe_cmd.txt）")

    seen = 0
    while True:
        try:
            cmds = [c.strip() for c in cmd_path.read_text(encoding="utf-8").splitlines() if c.strip()]
        except FileNotFoundError:
            cmds = []
        while seen < len(cmds):
            cmd = cmds[seen]
            seen += 1
            say(f"\n>>> [{time.strftime('%H:%M:%S')}] 执行命令 #{seen}：{cmd}")
            if cmd == "quit":
                say("[退出] 探测结束")
                return 0
            if cmd == "pnp":
                say(f"    USB 到达时间：{pnp_arrival()}")
            elif cmd == "probe":
                probe("discover", st["sdk"].discover_available_cameras)
                if st["cam"] is not None:
                    probe("旧句柄 ROI（自愈？）", lambda: tuple(st["cam"].roi))
                say(f"    USB 到达时间：{pnp_arrival()}")
            elif cmd == "dispose-old":
                if st["cam"] is not None:
                    t0 = time.perf_counter()
                    probe("旧句柄 dispose", st["cam"].dispose, timeout=30)
                    say(f"    耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
                    st["cam"] = None
                else:
                    say("    旧句柄已经没了")
            elif cmd == "open-new":
                if st["serial"] is None:
                    found = probe("先 discover 找 serial", st["sdk"].discover_available_cameras)
                    if not found:
                        say("    现在没有设备，open-new 做不了")
                        say("[就绪] 等下一条命令")
                        continue
                    st["serial"] = found[0]
                t0 = time.perf_counter()
                st["cam2"] = probe("新句柄 open_camera", lambda: st["sdk"].open_camera(st["serial"]), 30)
                say(f"    耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
                if st["cam2"] is not None:
                    probe("新句柄 ROI", lambda: tuple(st["cam2"].roi))
                    probe("新句柄抓一帧", lambda: st["cam2"].get_pending_frame_or_null() is not None)
            elif cmd == "dispose-new":
                if st["cam2"] is not None:
                    t0 = time.perf_counter()
                    probe("新句柄 dispose", st["cam2"].dispose, timeout=30)
                    say(f"    耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
                    st["cam2"] = None
                else:
                    say("    新句柄不存在")
            elif cmd == "dispose-sdk":
                if st["sdk"] is not None:
                    t0 = time.perf_counter()
                    probe("SDK dispose", st["sdk"].dispose, timeout=30)
                    say(f"    耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")
                    st["sdk"] = None
            elif cmd == "reinit":
                st["sdk"] = probe("再建 SDK 实例（闩锁？）", TLCameraSDK, timeout=30)
                if st["sdk"] is not None:
                    probe("再 discover", st["sdk"].discover_available_cameras)
            else:
                say(f"    ⚠ 不认识的命令：{cmd}")
            say("[就绪] 等下一条命令")
        time.sleep(0.3)


if __name__ == "__main__":
    raise SystemExit(main())
