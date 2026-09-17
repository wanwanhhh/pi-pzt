"""接口冒烟测试：假定服务已在 127.0.0.1:8000 运行。

会真实驱动位移台，跑完整条链路：伺服 -> 手动移动 -> 扫描 -> 每点元数据与图像。
用法：.venv/bin/python tools/apitest.py
      Windows：.venv\\Scripts\\python.exe tools\\apitest.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
FAIL: list[str] = []


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    if not ok:
        FAIL.append(name)


def status() -> dict:
    return call("GET", "/api/status")[1]


def main() -> int:
    code, st = call("GET", "/api/status")
    check("GET /api/status", code == 200, f"HTTP {code}")
    stage = st["stage"]
    print(f"    位移台 {stage['stage_type']} 行程 {stage['travel_min']}–{stage['travel_max']} µm"
          f" 位置 {stage['position']:.4f} 伺服 {stage['servo']}")

    print("\n[1] 伺服关着时应该拒绝运动")
    call("POST", "/api/servo", {"on": False})
    code, body = call("POST", "/api/move", {"target_um": 5.0})
    check("拒绝移动", code == 409, f"HTTP {code} {body.get('detail', '')}")

    print("\n[2] 开伺服（应在当前位置原地保持，不跳回旧目标）")
    before = status()["stage"]["position"]
    code, body = call("POST", "/api/servo", {"on": True})
    time.sleep(0.5)
    after = status()["stage"]["position"]
    check("伺服已开", code == 200 and status()["stage"]["servo"], f"HTTP {code}")
    check("未发生跳变", abs(after - before) < 1.0, f"{before:.4f} -> {after:.4f} µm")

    print("\n[3] 手动移动 / 夹限位")
    call("POST", "/api/move", {"target_um": 5.0})
    time.sleep(0.6)
    check("到位 5 µm", abs(status()["stage"]["position"] - 5.0) < 0.15,
          f"{status()['stage']['position']:.4f}")
    code, body = call("POST", "/api/move", {"target_um": 500.0})
    check("超程被夹到 100 µm", body.get("clamped") and body.get("target_um") == 100.0, str(body))
    call("POST", "/api/move", {"target_um": 0.0})
    time.sleep(0.6)
    code, body = call("POST", "/api/jog", {"delta_um": 1.0})
    time.sleep(0.6)
    check("jog +1 µm", abs(status()["stage"]["position"] - 1.0) < 0.15,
          f"{status()['stage']['position']:.4f}")

    print("\n[4] 自动扫描 0 → 2 µm，5 点")
    code, body = call("POST", "/api/scans",
                      {"name": "冒烟", "start_um": 0.0, "stop_um": 2.0,
                       "count": 5, "settle_ms": 100})
    check("扫描已启动", code == 200, f"HTTP {code} {body}")
    scan_id = body.get("scan_id")
    if scan_id is None:
        # 起不来的时候最需要诊断信息，不能自己崩掉
        print("\n扫描没能启动，后续用例无法继续")
        print(f"\n===== {len(FAIL)} 项失败 =====")
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    detail = {}
    for _ in range(120):
        time.sleep(0.2)
        detail = call("GET", f"/api/scans/{scan_id}")[1]
        if detail.get("status") in ("done", "aborted", "failed"):
            break
    check("扫描正常结束", detail.get("status") == "done", detail.get("status", "unknown"))
    pts = detail["points"]
    check("5 个点全部落库", len(pts) == 5, f"{len(pts)} 点")
    check("每点都有图像", all(p["image_path"] for p in pts),
          str([p["image_path"] for p in pts[:2]]))
    check("每点都在到位容差内", all(p["on_target"] for p in pts),
          str([round(p["actual_um"], 4) for p in pts]))
    check("目标等间隔", [round(p["target_um"], 4) for p in pts] == [0.0, 0.5, 1.0, 1.5, 2.0],
          str([p["target_um"] for p in pts]))
    print("    实际位置:", [round(p["actual_um"], 4) for p in pts])
    print("    每点耗时 ms:", [round(p["settled_ms"]) for p in pts])

    print("\n[5] 扫描进行中拒绝手动运动")
    call("POST", "/api/scans", {"name": "并发", "start_um": 0.0, "stop_um": 20.0,
                                "count": 40, "settle_ms": 200})
    time.sleep(0.5)
    code, body = call("POST", "/api/move", {"target_um": 50.0})
    check("拒绝手动移动", code == 409, f"HTTP {code} {body.get('detail', '')}")
    code, body = call("POST", "/api/scans", {"name": "并发2", "start_um": 0.0,
                                             "stop_um": 5.0, "count": 3, "settle_ms": 100})
    check("拒绝第二个扫描", code == 409, f"HTTP {code} {body.get('detail', '')}")

    print("\n[6] 暂停 / 继续 / 中止")
    call("POST", "/api/scans/control", {"action": "pause"})
    time.sleep(0.6)
    i1 = call("GET", "/api/status")[1]["scan"]["index"]
    time.sleep(0.8)
    i2 = call("GET", "/api/status")[1]["scan"]["index"]
    check("暂停后不再推进", i1 == i2, f"index {i1} -> {i2}")
    call("POST", "/api/scans/control", {"action": "resume"})
    time.sleep(0.8)
    i3 = call("GET", "/api/status")[1]["scan"]["index"]
    check("继续后恢复推进", i3 > i2, f"index {i2} -> {i3}")
    call("POST", "/api/scans/control", {"action": "abort"})
    time.sleep(1.0)
    sc = call("GET", "/api/status")[1]["scan"]
    check("已中止", sc["status"] == "aborted", sc["status"])

    print("\n[7] 首点预逼近：先退到起点外侧再逼近（单向逼近）")
    call("POST", "/api/velocity", {"velocity": 30.0})  # 放慢，便于观测退让过程
    call("POST", "/api/move", {"target_um": 50.0})
    time.sleep(2.0)
    call("POST", "/api/scans", {"name": "逼近", "start_um": 20.0, "stop_um": 21.0,
                                "count": 3, "settle_ms": 100})
    lowest = 1e9
    t0 = time.time()
    while time.time() - t0 < 1.6:
        lowest = min(lowest, status()["stage"]["position"])
    check("退到起点外侧（应低于 19.5）", lowest < 19.5, f"最低观测 {lowest:.4f} µm")
    for _ in range(60):
        time.sleep(0.2)
        if not status()["scan"]["status"] == "running":
            break
    call("POST", "/api/velocity", {"velocity": 10000.0})

    print("\n[8] 「停止」必须中止扫描，不能让扫描等满超时再自己动")
    call("POST", "/api/scans", {"name": "停止", "start_um": 0.0, "stop_um": 80.0,
                                "count": 60, "settle_ms": 300})
    time.sleep(1.5)
    t0 = time.time()
    code, body = call("POST", "/api/stop")
    check("停止同时中止扫描", body.get("scan_aborted") is True, str(body))
    sc = status()["scan"]
    for _ in range(40):
        time.sleep(0.1)
        sc = status()["scan"]
        if sc["status"] != "running":
            break
    dt = time.time() - t0
    check("扫描立刻结束且记为 aborted", sc["status"] == "aborted" and dt < 3.0,
          f"{sc['status']} 用时 {dt:.1f}s")
    code, _ = call("POST", "/api/move", {"target_um": 10.0})
    check("停止后手动操作未被永久拒绝", code == 200, f"HTTP {code}")

    print("\n[9] 急停收尾：状态必须是 aborted，不能卡在 running")
    call("POST", "/api/scans", {"name": "急停", "start_um": 0.0, "stop_um": 80.0,
                                "count": 60, "settle_ms": 300})
    time.sleep(1.5)
    call("POST", "/api/estop")
    time.sleep(1.2)
    sc = status()["scan"]
    check("急停后记为 aborted（不是 failed）", sc["status"] == "aborted", sc["status"])
    code, _ = call("POST", "/api/move", {"target_um": 5.0})
    check("急停后手动操作正常", code == 200, f"HTTP {code}")

    print("\n[10] 回到 0 µm 保持")
    call("POST", "/api/move", {"target_um": 0.0})
    time.sleep(0.8)
    st = status()["stage"]
    check("停在 0 µm 且保持伺服", abs(st["position"]) < 0.15 and st["servo"],
          f"{st['position']:.4f} µm servo={st['servo']}")

    print(f"\n===== {len(FAIL)} 项失败 =====" if FAIL else "\n===== 全部通过 =====")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
