"""数据页那些**整幅图作业**的共同机制：起算 → 进度 → 二进制口。

相位图与强度对齐是同一套东西：一个后台线程读完整条扫描、一个 job 字典报进度与结果、
两列 float32 走 `.bin`。它们**真不同**的地方只有三处，全在 `WholeMap` 的参数里：

- `run`：怎么算（相位图两个口径、强度对齐一遍读盘）；
- `meta`：下发给界面之前要摘掉数组、补哪些统计量（对齐多一个 `dc_median`）；
- `arrays`：`.bin` 里两列的**顺序**（相位/强度 vs 能量/直流）。

**路由不归并**（这是这一条的要害）：六条端点原样留着，各自只调一行。
做成 `/map?kind=phasemap|align` 会把 URL 从六条压成三条，但每条多一个 `kind` ——
`at_um` 只对相位图有意义、全通只对强度对齐有意义，一半参数组合非法，
每个组合都要在路由里再判一次：那是把重复换成了接口变宽。

**每个作业一个实例、一个自己的 job 字典**（不是类属性）：两个线程同时写才不会互相踩。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from fastapi import HTTPException
from fastapi.responses import Response


class WholeMap:
    """一个整幅图作业：`run(plan, lmin, lmax, progress, **kw)` → 含数组的结果字典。

    - `name`：报错文案里的那个名字（"相位图" / "强度对齐"），只有这一处拼；
    - `plan`：要读哪些帧、按哪个波段 —— 由 server 那边给（它知道库与裁剪记录）；
    - `meta`：下发给界面的那一份（摘掉数组、可补统计量）；
    - `arrays`：`.bin` 里按顺序摆的那几列。
    """

    def __init__(self, name: str, run: Callable, meta: Callable, arrays: Callable,
                 thread_name: str):
        self.name = name
        self._run = run
        self._meta = meta
        self._arrays = arrays
        self._thread_name = thread_name
        # 每个作业自己的一份：实例属性，不是类属性（两个线程同时写不共享）
        self.job: dict = {"running": False, "scan_id": None, "done": 0, "total": 0,
                          "error": None, "result": None}
        self.result: Optional[dict] = None      # 最近算完的那一份：meta + 数组

    # ---- 下面四件事就是那六条路由各自的一行 ----

    def start(self, scan_id: int, plan: dict, **kw) -> dict:
        """起算（后台线程）。上一张还在算就 409 —— 与裁剪同一个规矩。"""
        if self.job["running"]:
            raise HTTPException(
                409,
                f"上一张{self.name}还在算（{self.job['done']} / {self.job['total']} 张），"
                f"等它跑完再点",
            )
        self.job.update(running=True, scan_id=scan_id, done=0, total=len(plan["items"]),
                        error=None, result=None)
        threading.Thread(target=self._worker, args=(scan_id, plan, kw),
                         daemon=True, name=self._thread_name).start()
        return {"ok": True, "scan_id": scan_id, "total": len(plan["items"])}

    def state(self) -> dict:
        """进度 + 最近算完的那一份（**不含数组**：它们走 .bin）。

        meta 由 _worker 算好一次存进 job["result"]，这里只读 —— 不在每次 GET 时重算
        （对齐那张的 meta 要 np.median(全幅 dc)，实测全幅 10–15 ms；界面每 400 ms 问一次，
        算完之后会一直重算到关页面）。
        """
        return {"job": dict(self.job)}

    def bin_response(self, scan_id: int) -> Response:
        """那几列 float32 连着给（各 w×h、行优先），形状在结果里。"""
        import numpy as np

        if self.result is None or self.result["scan_id"] != scan_id:
            raise HTTPException(404, f"这一条还没有算好的{self.name}")
        buf = np.concatenate([a.ravel() for a in self._arrays(self.result)])
        return Response(content=buf.astype("<f4").tobytes(),
                        media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    def _worker(self, scan_id: int, plan: dict, kw: dict) -> None:
        """后台线程：算完落账，出错把原话说给界面（不重试、不补一个数）。"""
        def progress(done: int, _total: int) -> None:
            self.job["done"] = done

        try:
            res = self._run(plan, progress=progress, **kw)
            res["scan_id"] = scan_id
            # meta 也在这一个 try 里：它抛异常同样是「整条失败」。
            # 落在 try 外就是线程带着 running=True 死掉 —— 界面永远停在「还在算」，再点永远 409。
            meta = self._meta(res)
        except Exception as exc:                     # noqa: BLE001 —— 整条失败，原话说给界面
            import logging

            logging.getLogger("backend").warning("%s失败：#%s %s", self.name, scan_id, exc)
            self.job.update(running=False, error=str(exc))
            return
        self.result = res
        self.job.update(running=False, result=meta)
