"""扫描执行器。

跑在独立线程里，与浏览器连接无关：关页面、断网都继续跑。
单向逼近：从 start 到 stop 单调推进，每点都从同一侧逼近
（往返走会因实测约 0.1 µm 的方向性迟滞导致图像错位）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from . import store
from .ccd import Capture
from .config import (
    APPROACH_OFFSET_UM,
    FIRST_POINT_SETTLE_MS,
    ON_TARGET_TIMEOUT_S,
    SCAN_ARRIVAL_FRACTION,
    SOFT_LIMIT_MARGIN,
)
from .models import ScanRequest
from .stage_api import StageAborted, StageProto, StageStatus

log = logging.getLogger(__name__)


class ScanError(RuntimeError):
    """扫描请求不合法或当前不允许。"""


class Scanner:
    """单任务扫描执行器：同一时刻只允许一个扫描在跑。"""

    def __init__(self, stage: StageProto, capture: Capture) -> None:
        self._stage = stage
        self._capture = capture
        self._thread: Optional[threading.Thread] = None
        self._resume = threading.Event()
        self._abort = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "scan_id": None,
            "status": "idle",
            "index": 0,
            "count": 0,
            "target_um": None,
            "actual_um": None,
            "message": "",
        }

    # ---------------------------------------------------------------- 控制
    def busy(self) -> bool:
        """扫描是否占着设备（暂停中也算）。

        以状态为准，不以线程存活为准：终态是在收尾"停运动之后、写库之前"写入的，
        所以"状态转终态"就等于"扫描不会再碰设备"，判据完备。
        收尾期间仍报占用是对的 —— 那时确实还有一条 STP 要落下去。
        """
        with self._lock:
            return self._state["status"] in ("running", "paused")

    def start(self, req: ScanRequest) -> dict[str, Any]:
        # 设备往返（0.1~0.25 s，异常时更久）放在锁外：
        # 持锁做它会把 state()、遥测线程和 /api/status 一起卡住。
        st = self._stage.poll()
        if not st.servo:
            raise ScanError("伺服未开（已释放），请先开启伺服再扫描")
        lo = st.travel_min + SOFT_LIMIT_MARGIN
        hi = st.travel_max - SOFT_LIMIT_MARGIN
        for label, value in (("起点", req.start_um), ("终点", req.stop_um)):
            if not lo <= value <= hi:
                raise ScanError(f"{label} {value} µm 超出可扫描范围 {lo}–{hi} µm")

        settle_ms = self._settle_ms(req)
        with self._lock:
            # 锁内再判一次：上面放锁外之后这里才是唯一的占位点。
            # 这里直接读状态，不能用 busy()——它是加锁的，锁内调用会死锁。
            if self._state["status"] in ("running", "paused"):
                raise ScanError("已有扫描在运行，请先中止或等待结束")
            scan_id = store.create_scan(
                req.name, req.start_um, req.stop_um, req.count, settle_ms,
                # 位置读回口径跟着设备走（设备属性）：这条扫描以后要按它反推原值
                self._stage.caps.readback_to_um,
            )
            self._abort.clear()
            self._resume.set()
            self._state = {
                "scan_id": scan_id,
                "status": "running",
                "index": 0,
                "count": req.count,
                "target_um": req.start_um,
                "actual_um": None,
                "message": "",
            }
            self._thread = threading.Thread(
                target=self._run, name=f"scan-{scan_id}", args=(scan_id, req), daemon=True
            )
            self._thread.start()
        log.info("扫描 %s 启动：%.4f → %.4f µm，%d 点，稳定延时 %d ms",
                 scan_id, req.start_um, req.stop_um, req.count, settle_ms)
        return dict(self._state)

    def pause(self) -> dict[str, Any]:
        """暂停：当前这一帧采完后停住 —— 不再移动、不再开始新曝光，台子留在当时的位置。"""
        return self._set_paused(True)

    def resume(self) -> dict[str, Any]:
        return self._set_paused(False)

    def abort(self) -> dict[str, Any]:
        """中止。同时放开暂停阻塞，否则暂停中的循环看不到中止标志。"""
        self._abort.set()
        self._resume.set()
        return self.state()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    # ---------------------------------------------------------------- 执行
    def _settle_ms(self, req: ScanRequest) -> int:
        """没给稳定延时就用这台设备的默认值（caps.default_settle_ms）。

        界面一定会给（框里预填的就是这个默认值），这里是 API 直调时的兜底：
        PI 的默认是「到位后的延时」，XMT 的默认是「唯一的等待」，两者不能混用。
        """
        if req.settle_ms is not None:
            return req.settle_ms
        return int(self._stage.caps.default_settle_ms)

    def _run(self, scan_id: int, req: ScanRequest) -> None:
        step = (req.stop_um - req.start_um) / (req.count - 1)
        settle_s = self._settle_ms(req) / 1000.0
        try:
            # 相机归扫描用（预览让位、会话保持到扫描结束）：什么时候算结束只有这里知道，
            # 所以持有/交还由扫描器显式说 —— 不靠「空闲多久」之类的计时器去猜。
            # begin() 建的是**单帧触发**会话：之后每一发触发就是一次曝光的起点。
            self._capture.begin()
            self._approach_start(req, step, settle_s)

            moved_at = 0.0       # 这一点的移动时刻（耗时口径的起点）
            paused_s = 0.0       # 移动之后、触发之前的暂停（从这一点的耗时里扣掉）
            for i in range(req.count):
                target = req.start_um + step * i
                if i == 0:
                    # 第 0 点：走到起点、等稳定，**然后才触发**（曝光从这一刻开始）。
                    ok, _ = self._wait_boundary()
                    if not ok:
                        status, message = "aborted", f"已中止于第 0/{req.count} 点"
                        break
                    self._stage.move(target)
                    moved_at = time.monotonic()
                    # 等待与判到位都归设备层：PI 等 ONT 信号 + 稳定延时；XMT 只等满稳定延时
                    # （不读回、不判任何东西）。上层只要「到位 / 超时 / 中止」这一个结论。
                    if not self._stage.wait_on_target(
                        ON_TARGET_TIMEOUT_S,
                        cancel=self._abort.is_set,
                        settle_s=settle_s,
                    ):
                        if self._abort.is_set():
                            status, message = "aborted", f"已中止于第 0/{req.count} 点"
                        else:
                            status, message = self._not_arrived(scan_id, i, req, target)
                        break
                    self._capture.trigger()

                # 这一读就是**采集时的位置**（触发之后），连同命令值一起入库。
                # XMT 上它还是唯一能事后看出丢帧的东西（我们不做判据了，见下面那条）。
                st = self._stage.poll()
                if not st.on_target:
                    # 采图那一刻读数已经不在目标上：图像照采，如实记录
                    log.warning("第 %d 点采图时已不在位：目标 %.4f µm，实际 %.4f µm",
                                i, target, st.position)
                # 设备层的到达容差是**绝对**的，这里再补一道**相对**校验（取半个步距的理由见
                # config.SCAN_ARRIVAL_FRACTION）：超了就不采图、不入库、中止扫描。
                # 做不做由设备自己声明（stage.step_check）：PI 做；**XMT 不做** —— 用户定的，
                # 等满稳定延时就直接采图，偏差一概不拦。代价写在 docs/xmt/设备认识账.xml E12。
                # 步距为 0（起终点相同）时也没有"上一点"可比，不做。
                if (self._stage.step_check and step
                        and abs(st.position - target) > abs(step) * SCAN_ARRIVAL_FRACTION):
                    status = "failed"
                    message = (f"第 {i}/{req.count} 点偏差 {abs(st.position - target):.4f} µm"
                               f"超过步距的 {SCAN_ARRIVAL_FRACTION:.0%}"
                               f"（{abs(step) * SCAN_ARRIVAL_FRACTION:.4f} µm）："
                               f"设点可能丢了，或台子没走到")
                    log.error("扫描 %s %s", scan_id, message)
                    break

                # **等帧到齐再动**。帧交付本身就是「曝光已经结束」的硬证明：上机实测
                # 触发之后还有一段启动 + 读出延迟（12 ms 曝光也要 91 ms 才见帧），按
                # 「触发 + 曝光」去算什么时候能移动，会动在积分里。慢一点，但严格。
                shot = self._capture.capture(scan_id, i, st.position)

                if i + 1 < req.count:
                    # 点边界（帧已到手）：暂停/中止在这里生效 —— 台子不再动，也不会有
                    # 挂着的未取帧（暂停期间单帧会话里什么都不欠）。
                    ok, _ = self._wait_boundary()
                    if not ok:
                        self._store_point(scan_id, i, target, st, moved_at, paused_s, shot)
                        status, message = "aborted", f"已中止于第 {i}/{req.count} 点"
                        break
                    next_target = req.start_um + step * (i + 1)
                    self._stage.move(next_target)
                    next_moved_at = time.monotonic()
                    if not self._stage.wait_on_target(
                        ON_TARGET_TIMEOUT_S,
                        cancel=self._abort.is_set,
                        settle_s=settle_s,
                    ):
                        # 第 i 点已经采到，帧是好的：先入库再报错（不丢有效数据）。
                        self._store_point(scan_id, i, target, st, moved_at, paused_s, shot)
                        if self._abort.is_set():
                            status, message = "aborted", f"已中止于第 {i}/{req.count} 点"
                        else:
                            status, message = self._not_arrived(scan_id, i + 1, req, next_target)
                        break
                    # 到位之后、触发之前：暂停/中止在这里生效 —— 不再开始新曝光。
                    ok, next_paused = self._wait_boundary()
                    if not ok:
                        self._store_point(scan_id, i, target, st, moved_at, paused_s, shot)
                        status, message = "aborted", f"已中止于第 {i}/{req.count} 点"
                        break
                    # 到位之后才触发下一点：曝光整段都落在目标位置。
                    self._capture.trigger()
                else:
                    next_moved_at, next_paused = moved_at, 0.0

                self._store_point(scan_id, i, target, st, moved_at, paused_s, shot)
                moved_at = next_moved_at
                paused_s = next_paused
            else:
                status, message = "done", f"完成 {req.count} 点"
        except StageAborted:
            status, message = "aborted", "急停，扫描已中止"
        except BaseException as exc:  # 执行器线程：必须落库，不能让任务悬着
            # 相机报错也走这条：**扫描中相机出错就整条停下**（理由见 AGENTS.md 与
            # docs/thorlabs/设备认识账.xml 的处置清单）—— 不重试、不"该点无效后继续"。
            # 跑过的点都在库里，半截那条由人在界面上手动删（DELETE /api/scans/{id}）。
            status, message = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("扫描 %s 失败", scan_id)
        finally:
            # 先把设备放开再转终态：这样"状态转终态"之后扫描不会再碰设备，
            # busy() 以状态为准才成立（否则收尾期间的 409 是误导）。
            # 三步都不抛，所以终态一定写得到。
            self._capture.end()        # 交还相机（内部自己吞异常）
            if status != "done":
                self._stop_quiet(scan_id)
            with self._lock:
                self._state["status"] = status
                self._state["message"] = message
            self._finish_scan(scan_id, status, message)
            log.info("扫描 %s 结束：%s %s", scan_id, status, message)

    # ---------------------------------------------------------------- 内部
    def _wait_boundary(self) -> tuple[bool, float]:
        """点边界：暂停就停在这里（台子不动、不开始新曝光）。

        返回 (是否继续, 等了多久秒)。中止时返回 (False, 已等)；等了多久用来从
        该点的「耗时」里扣掉暂停时间（暂停不是采集开销）。

        只在两个位置调用：**帧到手之后**（移动之前）与**到位之后、触发之前**。
        所以暂停期间单帧会话里没有挂着的未取帧 —— 不会因为暂停把这一帧晾丢。
        """
        if self._abort.is_set():
            return False, 0.0
        if self._resume.is_set():
            return True, 0.0
        t0 = time.monotonic()
        while not self._resume.is_set():
            if self._abort.is_set():
                return False, time.monotonic() - t0
            self._resume.wait(0.05)
        return True, time.monotonic() - t0

    def _not_arrived(self, scan_id: int, i: int, req: ScanRequest,
                     target: float) -> tuple[str, str]:
        """没确认到位：不采图、不继续（宁可不跑，也不能入库错点）。

        文案不提「多少秒」：PI 是超时（10 s），XMT 是等满延时后读回超差，
        两者的时间含义不同，说成超时会把用户引去查信号/通讯。
        """
        pos = self._stage.status().position
        status = "failed"
        message = (f"第 {i}/{req.count} 点未确认到位"
                   f"（目标 {target:.4f} µm，读数 {pos:.4f} µm）")
        log.error("扫描 %s %s", scan_id, message)
        return status, message

    def _store_point(self, scan_id: int, i: int, target: float, st: StageStatus,
                     moved_at: float, paused_s: float, shot) -> None:
        """把第 i 点的帧入库/进状态（帧已经采到、写盘也在 capture 里做完了）。

        **耗时口径** = 从**这一次移动**到这一帧落盘，扣掉移动之后、触发之前的暂停。
        它包含「移动 + 稳定 → 触发 → 等帧交付」整段，首点/末点同一口径；其中
        「触发→帧交付」是相机固定延迟，所以一定大于曝光 —— 严格采集的代价在这里。
        """
        duration_ms = max(0.0, (time.monotonic() - moved_at - paused_s) * 1000.0)
        store.add_point(
            scan_id, i, target, st.position, duration_ms, st.on_target, st.settle_source,
            shot.path, shot.exposure_us,
        )
        with self._lock:
            self._state.update(index=i + 1, target_um=target, actual_um=st.position)

    def _approach_start(self, req: ScanRequest, step: float, settle_s: float) -> None:
        """先退到起点外侧再逼近，让首点与后续点从同一侧过来。

        起点贴着行程端点时退不出去（例如从 0 往上的扫描），只能照常逼近，
        这时首点会比其余点偏约 0.1 µm —— 记一条日志，不假装做到了。
        """
        st = self._stage.status()
        pre = req.start_um - (APPROACH_OFFSET_UM if step >= 0 else -APPROACH_OFFSET_UM)
        if not st.travel_min <= pre <= st.travel_max:
            # 退不出去（起点贴着行程端点）：首点只能从别处直接过来 —— 而控制器的 ONT
            # 会在台子**还在整定时**就置位（实测：一次 90 µm 移动，ONT 在 MOV 后 61 ms
            # 置位，此时离目标还差 0.32 µm；到 +450 ms 才进 0.013）。照界面设的稳定延时
            # （可能只有 20 ms）采，首点读数就会超过半个步距、整条扫描判失败 ——
            # 库里 #70/#72/#74 三条正是这么失败的。所以这里补一次「走到起点 + 等它停稳」，
            # 首点自己的稳定延时仍按界面设的走（A8/B4 见 docs/pi/设备认识账.xml）。
            log.warning("扫描起点 %.4f µm 贴行程端点，首点无法与其他点同侧逼近", req.start_um)
            self._stage.move(req.start_um)
            if not self._stage.wait_on_target(
                ON_TARGET_TIMEOUT_S,
                cancel=self._abort.is_set,
                settle_s=FIRST_POINT_SETTLE_MS / 1000.0,
            ):
                log.warning("首点预停稳到 %.4f µm 没确认到位：首点可能仍在整定", req.start_um)
            return
        self._stage.move(pre)
        if not self._stage.wait_on_target(
            ON_TARGET_TIMEOUT_S,
            cancel=self._abort.is_set,
            settle_s=settle_s,
        ):
            log.warning("预逼近 %.4f µm 没确认到位：首点可能与其他点不同侧", pre)

    def _set_paused(self, paused: bool) -> dict[str, Any]:
        """一次持锁完成"判断 + 写入"，避免与扫描收尾抢状态。"""
        with self._lock:
            current = self._state["status"]
            wanted = "paused" if paused else "running"
            if current not in ("running", "paused") or current == wanted:
                return dict(self._state)
            self._state["status"] = wanted
            # Event 与状态同进同出：写库万一抛异常，也不能出现
            # "界面说暂停了、循环还在往下走"。
            if paused:
                self._resume.clear()
            else:
                self._resume.set()
            # 写库也在同一临界区：否则它可能落在扫描收尾的 _finish_scan 之后，
            # 让已带 finished_at 的行又变成 paused。
            scan_id = self._state["scan_id"]
            if scan_id is not None:
                store.set_status(int(scan_id), wanted)
        return self.state()

    def _stop_quiet(self, scan_id: int) -> None:
        """收尾路径不能抛：抛了终态就写不到，任务会永远停在 running。

        捕 Exception 而不是 StageError——设备层只归一化 GCSError，
        别的类型（回执异常、DLL 异常）仍会逃出来。
        """
        try:
            self._stage.stop_motion()
        except Exception as exc:
            log.warning("扫描 %s 收尾停止失败：%s", scan_id, exc)

    def _finish_scan(self, scan_id: int, status: str, message: str) -> None:
        """写库失败也不把异常带出线程；残留行下次启动会被判为 aborted。"""
        try:
            store.finish_scan(scan_id, status, message)
        except Exception:
            log.exception("扫描 %s 终态写库失败", scan_id)
