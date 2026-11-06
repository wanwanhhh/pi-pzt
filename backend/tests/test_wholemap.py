"""整幅图作业的共同机制（WholeMap）：起算、进度、二进制口、落账。**不碰相机、不碰硬件。**

相位图与强度对齐共用这一个类，所以它的几条不变式在这里钉一次：

  1. **上一张还在算就 409**（与裁剪同一个规矩），算完才放开；
  2. **算的过程中报错**：原话进 job、running 落回 False、result 不动 —— 界面据此说话；
  3. **meta 抛异常也走同一条路**：它和 run 一样是"整条失败"，不能落在 try 外面
     （落在外面就是线程带着 running=True 死掉：界面永远停在"还在算"，再点永远 409）；
  4. **meta 只算一次**：state() 每次 GET 都重算的话，对齐那张每次都要 np.median(全幅 dc)；
  5. **.bin 的形状与顺序**：两列各 w×h、行优先、float32，没算过或不是这条扫描就 404。

直接跑：python backend/tests/test_wholemap.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from backend.wholemap import WholeMap  # noqa: E402


def _wait(w: WholeMap, timeout: float = 5.0) -> None:
    """等后台线程收口（它自己把 running 置回 False）。"""
    end = time.time() + timeout
    while w.job["running"] and time.time() < end:
        time.sleep(0.005)
    assert not w.job["running"], "后台线程没有收口"


def _mk(name="试", run=None, meta=None, arrays=None) -> WholeMap:
    def default_run(plan, progress, **kw):
        for i in range(len(plan["items"])):
            progress(i + 1, len(plan["items"]))
        return {"scan_id": None, "n": len(plan["items"]), "a": np.zeros((2, 2)),
                "b": np.ones((2, 2)), "w": 2, "h": 2}

    return WholeMap(name,
                    run or default_run,
                    meta or (lambda r: None if r is None else
                             {k: v for k, v in r.items() if k not in ("a", "b")}),
                    arrays or (lambda r: [r["a"], r["b"]]),
                    "test-wholemap")


def test_start_then_state_reports_the_result():
    w = _mk()
    plan = {"items": [(0, 1.0, None), (1, 2.0, None), (2, 3.0, None)], "rect": None}
    out = w.start(7, plan)
    assert out == {"ok": True, "scan_id": 7, "total": 3}, out
    _wait(w)
    job = w.state()["job"]
    assert job["running"] is False and job["error"] is None and job["scan_id"] == 7, job
    assert job["done"] == job["total"] == 3, job
    # meta 里没有数组（它们走 .bin），形状与谱线数都在
    assert "a" not in job["result"] and "b" not in job["result"], job["result"]
    assert (job["result"]["w"], job["result"]["h"], job["result"]["n"]) == (2, 2, 3), job["result"]


def test_second_start_while_running_is_409():
    gate = {"go": False}

    def slow_run(plan, progress, **kw):
        while not gate["go"]:
            time.sleep(0.005)
        return {"scan_id": None, "n": 1, "a": np.zeros((2, 2)), "b": np.ones((2, 2)),
                "w": 2, "h": 2}

    w = _mk(run=slow_run)
    plan = {"items": [(0, 1.0, None)], "rect": None}
    w.start(7, plan)
    try:
        w.start(8, plan)
    except HTTPException as exc:
        assert exc.status_code == 409 and "还在算" in exc.detail, exc
    else:
        raise AssertionError("上一张还在算时又起了一张")
    gate["go"] = True
    _wait(w)
    # 算完之后能再起一张（409 是暂时的，不是永久锁）
    w.start(9, plan)
    _wait(w)
    assert w.state()["job"]["scan_id"] == 9


def test_run_error_lands_in_the_job():
    def boom(plan, progress, **kw):
        raise ValueError("这一段波长有 900 根谱线，超过上限")

    w = _mk(run=boom)
    w.start(7, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)
    job = w.state()["job"]
    assert job["error"] == "这一段波长有 900 根谱线，超过上限", job
    assert job["running"] is False and job["result"] is None, job
    # 出错之后还能再起（不留在 running 上）
    w2 = _mk()
    w2.start(8, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w2)
    assert w2.state()["job"]["error"] is None


def test_meta_error_does_not_wedge_the_job():
    """**meta 抛异常也是"整条失败"** —— 不能落在 try 外面。

    落在外面时线程带着 running=True 死掉：界面永远停在"还在算"，再点永远 409，
    只能重启进程。这条钉子就是钉那个边界。
    """
    def bad_meta(res):
        raise KeyError("dc")

    w = _mk(meta=bad_meta)
    w.start(7, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)
    job = w.state()["job"]
    assert job["running"] is False, "meta 抛异常之后 running 没落回 False（线程死在半路）"
    assert job["error"] is not None and "dc" in job["error"], job
    # 还能再起一张（没有被永久锁住）
    w.start(8, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)


def test_meta_is_computed_once():
    """meta 只在算完时算一次；state() 每次 GET 都重算的话，对齐那张每次都要 median 全幅。"""
    calls = {"n": 0}

    def counting_meta(res):
        if res is not None:
            calls["n"] += 1
        return None if res is None else {"w": res["w"]}

    w = _mk(meta=counting_meta)
    w.start(7, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)
    after_run = calls["n"]
    for _ in range(5):
        w.state()
    assert calls["n"] == after_run == 1, (calls["n"], after_run)


def test_bin_response_shape_and_order():
    w = _mk()
    w.start(7, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)
    resp = w.bin_response(7)
    buf = np.frombuffer(resp.body, dtype="<f4")
    assert buf.size == 2 * (2 * 2), buf.size        # 两列 × 每列 2×2
    # 顺序：[第一列全部, 第二列全部]（行优先），第一列是 zeros、第二列是 ones
    assert np.all(buf[:4] == 0) and np.all(buf[4:] == 1), buf
    assert resp.headers["cache-control"] == "no-store", dict(resp.headers)


def test_start_drops_the_previous_result_so_bin_never_hands_out_the_old_one():
    """起算那一刻，手里那一份结果就没了 —— `.bin` 不会在起算瞬间照常交出上一份。

    同一个 scan_id 换波段重算是最容易撞上的一格：扫描号没变，`bin_response` 只按扫描号认，
    上一份（16 字节的旧数组）会一直给到新的算完为止。界面拿到的是"新元数据 + 旧数组"。
    """
    gate = {"go": False}

    def slow_run(plan, progress, **kw):
        while not gate["go"]:
            time.sleep(0.005)
        return {"scan_id": None, "n": 1, "a": np.zeros((2, 2)), "b": np.ones((2, 2)),
                "w": 2, "h": 2}

    w = _mk(run=slow_run)
    plan = {"items": [(0, 1.0, None)], "rect": None}
    w.start(7, plan)
    gate["go"] = True
    _wait(w)
    assert len(w.bin_response(7).body) == 2 * 2 * 2 * 4, "算完之后该给得出 .bin"

    gate["go"] = False
    w.start(7, plan)                       # 同一个扫描号，换一份重算
    try:
        w.bin_response(7)
    except HTTPException as exc:
        assert exc.status_code == 404, exc
    else:
        raise AssertionError("起算之后仍然交出了上一份 .bin")
    gate["go"] = True
    _wait(w)
    assert len(w.bin_response(7).body) == 2 * 2 * 2 * 4, "算完之后要重新给得出来"


def test_bin_response_404_before_any_result_and_for_other_scan():
    w = _mk()
    try:
        w.bin_response(7)
    except HTTPException as exc:
        assert exc.status_code == 404, exc
    else:
        raise AssertionError("还没算过就给了 .bin")
    w.start(7, {"items": [(0, 1.0, None)], "rect": None})
    _wait(w)
    try:
        w.bin_response(8)
    except HTTPException as exc:
        assert exc.status_code == 404 and "试" in exc.detail, exc
    else:
        raise AssertionError("别的扫描号也给了这一份 .bin")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
