"""FastAPI 服务：REST 命令 + SSE 遥测 + 扫描控制。

单进程单 worker：GCS DLL 非线程安全，全进程只能有一个设备 owner 线程。
所有设备访问都排队进入 pi_stage.Stage，HTTP 处理函数本身不碰 DLL。

启动：run.bat    等价于  .venv\\Scripts\\python.exe -m backend.server
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .ccd import make_capture
from .config import (
    DATA_DIR,
    HEARTBEAT_TIMEOUT_S,
    HOST,
    PORT,
    STATIC_DIR,
    TELEMETRY_HZ,
    TELEMETRY_HZ_SCAN,
)
from .models import (
    JogRequest,
    MoveRequest,
    ScanControl,
    ScanRequest,
    ServoRequest,
    VelocityRequest,
)
from .pi_stage import Stage, StageError, StageNotConnected
from .scanner import ScanError, Scanner

log = logging.getLogger(__name__)

store.init()
stage = Stage()
scanner = Scanner(stage, make_capture())

# 心跳只表示"界面还在"，不参与任何控制（扫描独立于浏览器）
_last_heartbeat = 0.0


class Telemetry:
    """唯一的状态轮询者。

    每次设备查询约 32 ms，多个客户端各查一遍会互相拖垮；
    这里统一轮询，SSE 只读快照。
    """

    def __init__(self, stage_: Stage, scanner_: Scanner, hz: float, hz_scan: float) -> None:
        self._stage = stage_
        self._scanner = scanner_
        self._hz = hz
        self._hz_scan = hz_scan
        self._lock = threading.Lock()
        self._latest: dict = {"stage": None, "scan": scanner_.state(), "ts": 0.0}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def period(self) -> float:
        """当前推送间隔。扫描进行中降频，把设备带宽让给扫描。"""
        running = self._scanner.state()["status"] == "running"
        return 1.0 / (self._hz_scan if running else self._hz)

    def _loop(self) -> None:
        degraded = False
        while not self._stop.is_set():
            t0 = time.monotonic()
            scan = self._scanner.state()
            try:
                st = self._stage.poll().as_dict()
                degraded = False
            except Exception as exc:
                # 这里必须吞掉一切：轮询线程死了，界面会永远停在过期数据上，
                # 看起来还在动，实际已经瞎了。
                st = self._stage.status().as_dict()
                st["error"] = str(exc)
                if not degraded:
                    log.warning("遥测读取失败：%s", exc, exc_info=True)
                    degraded = True
            payload = {
                "stage": st,
                "scan": scan,
                "frontend_online": time.time() - _last_heartbeat < HEARTBEAT_TIMEOUT_S,
                "ts": time.time(),
            }
            with self._lock:
                self._latest = payload
            self._stop.wait(max(0.0, self.period() - (time.monotonic() - t0)))


telemetry = Telemetry(stage, scanner, TELEMETRY_HZ, TELEMETRY_HZ_SCAN)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        stage.start()
    except Exception as exc:
        # 控制器没开也让服务起来，界面会显示未连接，之后可以点"重连"。
        # 这里不能只抓 StageError：DLL 加载/回执异常会是别的类型。
        log.error("设备连接失败：%s", exc, exc_info=True)
    telemetry.start()
    log.info("服务就绪：http://%s:%d", HOST, PORT)
    try:
        yield
    finally:
        telemetry.stop()
        scanner.abort()
        stage.shutdown()


app = FastAPI(title="PI P-621 位移台控制", lifespan=lifespan)


@app.exception_handler(StageError)
def _stage_error(_: Request, exc: StageError) -> JSONResponse:
    code = 503 if isinstance(exc, StageNotConnected) else 409
    return JSONResponse({"detail": str(exc)}, status_code=code)


@app.exception_handler(ScanError)
def _scan_error(_: Request, exc: ScanError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=409)


def _reject_while_scanning() -> None:
    """扫描线程活着就占着设备，暂停中也一样。

    暂停时手动挪台，继续后下一点会从错误一侧逼近（约 0.1 µm 迟滞错位）；
    暂停时关伺服，继续后首个 MOV 会被控制器拒绝。
    """
    if scanner.busy():
        raise HTTPException(409, "扫描进行中或已暂停，请先中止再手动操作")


def _require_servo() -> None:
    """伺服关着的时候 MOV 不会产生位移，与其静默失效不如直接拒绝。"""
    if not stage.poll().servo:
        raise HTTPException(409, "伺服未开（已释放），请先开启伺服")


# ------------------------------------------------------------------ 状态
@app.get("/api/status")
def api_status() -> dict:
    return {
        "stage": stage.poll().as_dict(),
        "scan": scanner.state(),
        "frontend_online": time.time() - _last_heartbeat < HEARTBEAT_TIMEOUT_S,
    }


@app.get("/api/events")
async def api_events(request: Request) -> StreamingResponse:
    async def stream():
        while not await request.is_disconnected():
            yield f"data: {json.dumps(telemetry.latest(), ensure_ascii=False)}\n\n"
            await asyncio.sleep(telemetry.period())

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/heartbeat")
def api_heartbeat() -> dict:
    global _last_heartbeat
    _last_heartbeat = time.time()
    return {"ok": True}


@app.post("/api/connect")
def api_connect() -> dict:
    if not stage.status().connected:
        stage.start()
    return stage.poll().as_dict()


# ------------------------------------------------------------------ 手动
@app.post("/api/move")
def api_move(req: MoveRequest) -> dict:
    _reject_while_scanning()
    _require_servo()
    applied = stage.move(req.target_um)
    return {"target_um": applied, "clamped": abs(applied - req.target_um) > 1e-9}


@app.post("/api/jog")
def api_jog(req: JogRequest) -> dict:
    _reject_while_scanning()
    _require_servo()
    applied = stage.jog(req.delta_um)
    return {"target_um": applied}


@app.post("/api/velocity")
def api_velocity(req: VelocityRequest) -> dict:
    _reject_while_scanning()
    return {"velocity": stage.set_velocity(req.velocity)}


@app.post("/api/servo")
def api_servo(req: ServoRequest) -> dict:
    """开伺服：按当前位置原地保持。关伺服：卸力，与 /api/release 同路。

    关伺服不做扫描占用检查 —— 卸力是断电类动作，被状态检查挡住是反安全方向的。
    """
    if not req.on:
        scanner.abort()
        stage.set_servo(False)
        return {"servo": False, "position_um": None}
    _reject_while_scanning()
    return {"servo": True, "position_um": stage.hold_here()}


# ------------------------------------------------------------------ 停止 / 释放 / 急停
@app.post("/api/stop")
def api_stop() -> dict:
    """停止运动并中止正在跑的扫描，保持伺服（位姿保持）。

    不中止扫描的话，扫描线程会一直在等到位，等满超时后按错位置采图、
    再继续走下一个点 —— 按了停止又自己动起来。
    """
    scan_aborted = scanner.busy()
    scanner.abort()
    stage.stop_motion()
    return {"ok": True, "scan_aborted": scan_aborted}


@app.post("/api/release")
def api_release() -> dict:
    """关伺服卸力。台子会回弹，异常振动时使用。"""
    scanner.abort()
    stage.release()
    return {"ok": True}


@app.post("/api/estop")
def api_estop() -> dict:
    """急停：丢弃排队命令 + STP，并中止正在跑的扫描。"""
    scanner.abort()
    stage.estop()
    return {"ok": True}


# ------------------------------------------------------------------ 扫描
@app.post("/api/scans")
def api_scan_start(req: ScanRequest) -> dict:
    return scanner.start(req)


@app.post("/api/scans/control")
def api_scan_control(req: ScanControl) -> dict:
    return getattr(scanner, req.action)()


@app.get("/api/scans")
def api_scan_list(limit: int = 50) -> list[dict]:
    return store.list_scans(limit)


@app.get("/api/scans/{scan_id}")
def api_scan_detail(scan_id: int) -> dict:
    scan = store.get_scan(scan_id)
    if scan is None:
        raise HTTPException(404, "扫描不存在")
    scan["points"] = store.get_points(scan_id)
    return scan


@app.delete("/api/scans/{scan_id}")
def api_scan_delete(scan_id: int) -> dict:
    if scanner.state()["scan_id"] == scan_id and scanner.state()["status"] in (
        "running",
        "paused",
    ):
        raise HTTPException(409, "扫描进行中，先中止再删除")
    store.delete_scan(scan_id)
    return {"ok": True}


# ------------------------------------------------------------------ 静态页面
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # workers 必须是 1：设备 owner 线程不能跨进程复制
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
