"""FastAPI 服务：REST 命令 + SSE 遥测 + 扫描控制。

单进程单 worker：设备通道非线程安全（Windows 的 GCS DLL、Linux 的串口都一样），
全进程只能有一个设备 owner 线程。所有设备访问都排队进入设备层的 Stage 实现，
HTTP 处理函数本身不碰设备。

启动：run.sh（Linux）/ run.bat（Windows）
      等价于  .venv/bin/python -m backend.server
      Windows：.venv\\Scripts\\python.exe -m backend.server
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import store
from .ccd import make_capture
from .config import (
    DATA_DIR,
    HEARTBEAT_TIMEOUT_S,
    HOST,
    IMAGE_DIR,
    PORT,
    STATIC_DIR,
    TELEMETRY_HZ,
    TELEMETRY_HZ_SCAN,
    TRACE_BUFFER_S,
    TRACE_MAX_S,
    TRACE_MIN_S,
)
from .models import (
    JogRequest,
    MoveRequest,
    ScanControl,
    ScanRequest,
    ServoRequest,
    VelocityRequest,
)
from .scanner import ScanError, Scanner
from .stage_api import StageError, StageNotConnected, StageProto, create_stage
from .trace import TraceBuffer

log = logging.getLogger(__name__)

store.init()
stage = create_stage()
scanner = Scanner(stage, make_capture())

# 心跳只表示"界面还在"，不参与任何控制（扫描独立于浏览器）
_last_heartbeat = 0.0


class Telemetry:
    """唯一的状态轮询者。

    每次设备查询约 32 ms，多个客户端各查一遍会互相拖垮；
    这里统一轮询，SSE 只读快照。
    """

    def __init__(self, stage_: StageProto, scanner_: Scanner, hz: float, hz_scan: float) -> None:
        self._stage = stage_
        self._scanner = scanner_
        self._hz = hz
        self._hz_scan = hz_scan
        self._lock = threading.Lock()
        self._latest: dict = {"stage": None, "scan": scanner_.state(), "ts": 0.0}
        self._trace = TraceBuffer(TRACE_BUFFER_S)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def trace(self, from_ts: float, to_ts: float) -> list:
        """位置曲线的一段窗口。**只读内存，不碰设备** —— 曲线与界面读数同源。"""
        with self._lock:
            return self._trace.window(from_ts, to_ts)

    def period(self) -> float:
        """当前推送间隔。扫描进行中降频，把设备带宽让给扫描。"""
        running = self._scanner.state()["status"] == "running"
        return 1.0 / (self._hz_scan if running else self._hz)

    def _loop(self) -> None:
        fresh = True
        while not self._stop.is_set():
            t0 = time.monotonic()
            scan = self._scanner.state()
            try:
                st = self._stage.poll().as_dict()
            except Exception as exc:
                # 这里必须吞掉一切：轮询线程死了，界面会永远停在过期数据上，
                # 看起来还在动，实际已经瞎了。
                st = self._stage.status().as_dict()
                st["error"] = str(exc)
                if fresh:
                    log.warning("遥测读取失败：%s", exc, exc_info=True)
                fresh = False
            else:
                fresh = True
            now = time.time()
            payload = {
                "stage": st,
                "scan": scan,
                "caps": CAPS_DICT,
                "frontend_online": now - _last_heartbeat < HEARTBEAT_TIMEOUT_S,
                "ts": now,
            }
            with self._lock:
                # 写样本必须和换最新帧在同一把锁里：trace() 是持这把锁迭代那个 deque 的
                # （TraceBuffer.window 用 for 迭代），边迭代边 append/popleft 会抛
                # RuntimeError: deque mutated during iteration —— 实测约 0.01%/次读，
                # 表现为"记录曲线记到一半就 500"。
                # 读失败或没连上时 status() 里是上一次的残留位置，记进曲线会把方差压低。
                if fresh and st["connected"]:
                    self._trace.add(now, st["position"], st["target"])
                self._latest = payload
            self._stop.wait(max(0.0, self.period() - (time.monotonic() - t0)))


telemetry = Telemetry(stage, scanner, TELEMETRY_HZ, TELEMETRY_HZ_SCAN)

# 能力声明发给前端：界面文案与按钮可用性由它决定（AGENTS.md「能力标志只用来
# 决定界面文案与上层策略」）。设备固定，算一次就够。
CAPS_DICT = asdict(stage.caps)


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
        # 相机要在进程退出前显式放开：SDK 进程内只开一次、只关一次（见 thorlabs_ccd.py）。
        # 没启用真相机时 _thorlabs 是 None，什么都不做。
        from . import ccd as ccd_mod

        if ccd_mod._thorlabs is not None:
            ccd_mod._thorlabs.close()
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
        "caps": CAPS_DICT,
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


# ------------------------------------------------------------------ 位置曲线
# ------------------------------------------------------------------ 相机
@app.get("/api/ccd/status")
def ccd_status() -> dict:
    """相机状态。没启用真相机时如实说，前端据此决定要不要显示预览。"""
    from .ccd import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        return {"backend": CCD_BACKEND, "preview": False, "available": False,
                "state": "off", "failure": "",
                "message": f"CCD 后端是 {CCD_BACKEND}，没有真相机"}
    from .ccd import thorlabs_camera

    cam = thorlabs_camera()
    try:
        st = cam.status()
    except Exception as exc:                # noqa: BLE001
        # **永远答得出来**：界面每个节拍都要它。给 500 只会让界面瞎猜 + 弹一串 toast。
        st = {"state": "failed", "failure": f"{type(exc).__name__}: {exc}"}
    st["backend"] = CCD_BACKEND
    st["available"] = True        # 后端在：能不能用看 state
    return st


def _ccd_idle() -> None:
    """相机与位移台同一条规矩：扫描占着设备时，预览一律拒。"""
    if scanner.state()["status"] in ("running", "paused"):
        raise HTTPException(409, "扫描进行中，相机归扫描用；先暂停或中止再看预览")


@app.post("/api/ccd/preview")
async def ccd_preview(on: bool = Query(True)) -> dict:
    """开/关连续预览。开的时候要打开相机，慢，所以丢到线程里。"""
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    camera = thorlabs_camera()
    try:
        return await asyncio.to_thread(camera.preview, on)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


def _safe_image_path(path: str) -> Path:
    """把请求里的相对路径夹在 data/ 里，而且必须是一个**真实存在的文件**。

    用 is_file() 而不是 exists()：目录也能 exists —— 空路径会落到 data/ 自己身上，
    读元数据会静默返回 null、取缩略图会 500。这种"看起来成功其实什么都没做"最难查。
    """
    root = DATA_DIR.resolve()
    src = (root / path).resolve()
    if not src.is_relative_to(root) or not src.is_file():
        raise HTTPException(404, "图像不存在")
    return src


@app.get("/api/grabs")
def list_grabs(limit: int = Query(60, ge=1, le=500)) -> dict:
    """**只列手动保存的采集帧**（「保存原生帧」存下的那些，按库里的登记表）。

    扫描各点的图**不混进来** —— 它们属于各自的扫描，在「扫描」页的点位表里看，
    一堆自动图会把手动存的那些淹掉。
    """
    # **以库里的登记为准**，不是"扫目录里叫 grab_* 的文件"：
    # 登记只发生在手动保存那一条路上（save_raw），所以扫描采的图（哪怕改了名、
    # 或者 index 号排到很大）都不会漏进来。
    from .thorlabs_ccd import read_png_meta

    items = []
    for row in store.list_grabs():
        f = IMAGE_DIR / row["filename"]
        if not f.exists():          # 文件被手工删了就跳过，免得点开 404
            continue
        # 曝光与质心都从 PNG 自己的 tEXt 里读（**不存库，文件自证**）；老图没有就是 None。
        # 质心是**传感器坐标**，与文件里的像素同一套 —— 拿它对像素不用换算。
        meta = read_png_meta(f)
        items.append({
            "name": f.name, "path": f"images/{f.name}",
            "exposure_us": meta["exposure_us"],
            "centroid": meta["centroid"],
            "bytes": f.stat().st_size, "mtime": f.stat().st_mtime,
        })
    items.sort(key=lambda it: it["mtime"], reverse=True)
    return {"total": len(items), "items": items[:limit]}


def _clean_text(value: str, limit: int) -> str:
    """去掉控制字符（含换行/制表）再截断。

    换行不清掉的话，名字里贴一坨多行文本会把图库网格撑变形；
    控制字符还可能被当成终端转义序列。
    """
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()[:limit]


def _grab_file(name: str) -> str:
    """校验请求里的文件名：只能是文件名（不是路径），而且必须真实存在。"""
    if Path(name).name != name:
        raise HTTPException(400, "只接受文件名，不接受路径")
    if not (IMAGE_DIR / name).exists():
        raise HTTPException(404, "没有这一帧")
    return name


# Windows 文件名非法字符（反斜杠 / 斜杠 / 冒号 / 星号 / 问号 / 引号 / 尖括号 / 竖线）+ 控制字符
_BAD_IN_NAME = set('\\/:*?"<>|')
# Windows 保留设备名：CON.png 这种在 Windows 上建不出来（会被当成设备）
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _safe_grab_name(raw: str, old: str) -> str:
    """把用户输入整成一个安全的、带 .png 的**文件名**。

    安全边界，别删：名字直接来自 HTTP 请求，而且要拿去在磁盘上创建文件 ——
    路径分隔符、上跳、保留设备名、首尾点号全部挡掉。
    """
    base = _clean_text(raw, 80).strip().rstrip(". ")
    if not base:
        raise HTTPException(400, "新名字不能为空")
    if any(ch in _BAD_IN_NAME for ch in base):
        raise HTTPException(400, "名字里不能有 反斜杠 斜杠 冒号 星号 问号 引号 尖括号 竖线")
    if not base.lower().endswith(".png"):
        base += ".png"      # 内容仍是 16 位 PNG；用户写了 .tif 也保留那个后缀，不假装换格式
    stem = base[:-4]
    if stem.upper() in _RESERVED:
        raise HTTPException(400, f"{stem} 是 Windows 保留名，换一个")
    if base == old:
        raise HTTPException(400, "新名字和现在一样")
    if (IMAGE_DIR / base).exists():
        raise HTTPException(409, f"{base} 已存在，换一个名字")
    return base


@app.post("/api/grabs/rename")
def rename_grab(
    name: str = Query(..., description="当前文件名，例如 grab_20260920_153222.png"),
    new_name: str = Query("", alias="new_name", description="新名字（不带后缀会自动补 .png）"),
) -> dict:
    """**真正改文件名**：磁盘上 rename + 更新库里的记录，两件事一起做。

    为什么必须一起做：图库按库里的记录列文件，只改文件不改库（或反过来）就会出现
    "库里有记录、点开是 404"，或者"文件还在、列表里却没了"。
    磁盘先动：失败就直接报错，不留半成品；库改失败则把文件改回去。
    """
    _grab_file(name)
    new = _safe_grab_name(new_name, name)
    src, dst = IMAGE_DIR / name, IMAGE_DIR / new
    src.rename(dst)
    try:
        store.rename_grab(name, new)
    except Exception:
        dst.rename(src)
        raise HTTPException(500, "改库失败，文件名已回滚") from None
    for cache in (DATA_DIR / "thumbs").glob(f"{Path(name).stem}_*.jpg"):
        cache.unlink(missing_ok=True)      # 缩略图缓存按文件名做键，旧的清掉
    log.info("原始帧改名：%s → %s", name, new)
    return {"ok": True, "name": new, "old": name}


@app.post("/api/grabs/purge")
def purge_grabs(keep: int = Query(0, ge=0, description="只保留最近 N 张，其余全删；0 = 全删")) -> dict:
    """批量清理手动保存的原始帧：**只保留最近 N 张**。

    用途是收尾（比如一轮调试留下的十几张"能跑通"的帧，没必要留着）。
    同样文件+记录+缩略图缓存一起删。
    """
    keep_set = {it["name"] for it in list_grabs(limit=500)["items"][:keep]}
    removed = []
    for row in store.list_grabs():
        name = row["filename"]
        if name in keep_set:
            continue
        (IMAGE_DIR / name).unlink(missing_ok=True)
        for cache in (DATA_DIR / "thumbs").glob(f"{Path(name).stem}_*.jpg"):
            cache.unlink(missing_ok=True)
        removed.append(name)
    store.delete_grabs(removed)
    return {"ok": True, "removed": len(removed), "kept": sorted(keep_set)}


@app.delete("/api/grabs/{name}")
def delete_grab(name: str) -> dict:
    """删掉一帧：文件和库里的记录一起删。"""
    _grab_file(name)
    src = IMAGE_DIR / name
    if src.exists():
        src.unlink()
    for cache in (DATA_DIR / "thumbs").glob(f"{Path(name).stem}_*.jpg"):
        cache.unlink(missing_ok=True)
    store.delete_grab(name)
    return {"ok": True}


@app.get("/api/ccd/profile")
async def ccd_profile(
    x: int = Query(..., ge=0, description="显示坐标（跟随预览朝向）"),
    y: int = Query(..., ge=0),
) -> dict:
    """过 (x, y) 的两条**全长**剖面：水平整行 + 垂直整列，取自最近一帧**原生 16 位**数据。

    坐标是**显示坐标** —— 先按当前朝向转成视图再切，所以转了 90° 之后"水平"仍是你看到的水平。
    不做任何处理（不扣背景、不平滑），值就是相机的 0~1022 ADU。
    """
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    try:
        return await asyncio.to_thread(thorlabs_camera().profile, x, y)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/image/profile")
def image_profile(
    path: str = Query(..., description="相对 data/ 的路径"),
    x: int = Query(..., ge=0, description="文件坐标（保存的文件是传感器朝向）"),
    y: int = Query(..., ge=0),
) -> dict:
    """存下来的 PNG 的剖面。文件不转朝向，所以这里的 (x, y) 就是你在图上点的位置。"""
    from .thorlabs_ccd import png_profile

    try:
        return png_profile(_safe_image_path(path), x, y)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/image/meta")
def image_meta(path: str = Query(..., description="相对 data/ 的路径，例如 images/scan0001_00000.png")) -> dict:
    """任意一张 data/ 下图片**自己身上**记的元数据（曝光、质心）。

    图库那批走 /api/grabs（一次列完），这里给**点位表的扫描帧**用：它们不在图库列表里，
    但文件里同样写着曝光与质心（同一段保存代码写成 tEXt）。老图没有就是 None —— 不猜值。
    """
    from .thorlabs_ccd import read_png_meta

    return read_png_meta(_safe_image_path(path))


@app.get("/api/grabs/thumb")
def grab_thumb(
    path: str = Query(..., description="相对 data/ 的路径"),
    max_side: int = Query(260, ge=64, le=2000, description="长边像素"),
) -> Response:
    """缩略图：走缓存（data/thumbs/），列表里几十张也打得开；弹窗里要更大就调 max_side。"""
    from .thorlabs_ccd import thumb_jpeg

    src = _safe_image_path(path)
    return Response(content=thumb_jpeg(src, max_side), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/ccd/exposure")
async def ccd_exposure(
    exposure_us: int = Query(..., gt=0, le=270_000_000, description="曝光（µs），预览与采图共用"),
) -> dict:
    """改曝光（**预览与采图一起改**），立刻生效。范围由相机层按设备自报值校验（安全边界）。

    设完之后采的每一帧，元数据与 PNG 自带的 tEXt 里记的都是相机读回的实际曝光。
    """
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    try:
        return await asyncio.to_thread(thorlabs_camera().set_exposure, exposure_us)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/ccd/reopen")
async def ccd_reopen() -> dict:
    """重开相机：USB 接触不良 / 相机掉线之后，只有把旧句柄丢掉再开才能接回来。

    **手动功能，不做自动重连**（用户定的）：什么时候重开由人决定。
    扫描进行中一律 409（相机归扫描用），与其它相机接口同规矩。
    """
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    try:
        return await asyncio.to_thread(thorlabs_camera().reopen)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/ccd/rotation")
async def ccd_rotation(
    deg: int = Query(..., description="预览显示朝向，顺时针 0/90/180/270；**只转预览，不动保存的文件**"),
) -> dict:
    """转预览的显示朝向。不是设备设置、更不是数据加工：取帧后转过来显示而已（90° 整数倍是精确置换）。

    保存的原生帧永远是传感器朝向 —— 要"文件也转"是另一件事（会让像素坐标和传感器脱钩），没做。
    """
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    try:
        return await asyncio.to_thread(thorlabs_camera().set_rotation, deg)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/ccd/gain")
def ccd_gain_locked(gain: int = Query(0, description="已固定为 0")) -> dict:
    """增益固定 0，不给改。"""
    raise HTTPException(
        400,
        "增益固定为 0：它把信号和噪声一起放大，信噪比不会变好；亮度只用曝光调。",
    )


@app.post("/api/ccd/capture")
async def ccd_capture() -> dict:
    """采一帧原生全幅存盘（不裁剪）。走扫描采图同一段代码，但不属于任何扫描。

    **登记放在这一层**：相机层在 store 之下，不许反向依赖它（见 AGENTS.md 分层）。
    图库按登记表列，"存了盘"和"进图库"是两件事 —— 后者只有手动保存才有，
    扫描各点的图归 point.image_path。
    """
    _ccd_idle()
    from .config import CCD_BACKEND

    if CCD_BACKEND != "thorlabs":
        raise HTTPException(503, f"CCD 后端是 {CCD_BACKEND}，没有真相机")
    from .ccd import thorlabs_camera

    try:
        info = await asyncio.to_thread(thorlabs_camera().save_raw)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    store.register_grab(Path(info["path"]).name)
    return info


@app.get("/api/ccd/preview.jpg")
def ccd_preview_jpg() -> Response:
    """最近一帧预览。不排队等设备；还没出帧就给 204，前端继续按自己的节奏拉。

    **相机掉线时必须报错**，不能把上一帧接着发出去：那样界面会一直显示最后那张图、
    看着像活着，人就以为「重开没用」（实测就是这么被骗的）。掉线给 503，
    前端的取帧失败计数接住它，提示与「重开相机」按钮就在旁边。
    """
    from .ccd import thorlabs_camera

    cam = thorlabs_camera()
    st = cam.status()
    jpeg = cam.latest_jpeg()
    if jpeg is None:
        if st.get("state") == "failed":
            # 会话没了（掉线/出错）：**必须报错**，不能拿上一帧冒充实时画面
            raise HTTPException(503, f"相机不可用：{st.get('failure')}；点「重开相机」重新连接")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(
        content=jpeg, media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/trace")
def api_trace(
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    seconds: float = 10.0,
) -> dict:
    """遥测环形缓冲里的一段位置：界面用它画曲线、算标准差。

    **只读内存，不查设备** —— 多一个轮询者就是从扫描和遥测手里抢设备带宽。
    时刻是服务器墙钟（time.time()），与 SSE 每帧的 ts 同一把尺子：
    前端把上一帧的 ts 当 from，正好落在样本边界上，不会漏一个点。
    from 给了就按它，没给就取 to 之前的 seconds 秒；两端都是闭区间。

    长度只管在最终解出来的窗口上判一次 —— 两条入口（seconds / from+to）走同一条规则，
    前端填了荒唐的时长也是在这里被拒，不用它在界面上自己判。
    """
    now = time.time()
    to = to_ts if to_ts is not None else now
    frm = from_ts if from_ts is not None else to - seconds
    span = to - frm
    if not TRACE_MIN_S <= span <= TRACE_MAX_S:
        raise HTTPException(422, f"窗口长度要在 {TRACE_MIN_S:g}~{TRACE_MAX_S:g} 秒之间")
    items = telemetry.trace(frm, to)
    return {
        "from": frm,
        "to": to,
        "now": now,
        "ts": [it[0] for it in items],
        "position": [it[1] for it in items],
        "target": [it[2] for it in items],
    }


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
    """停止运动并中止正在跑的扫描。

    不中止扫描的话，扫描线程会一直在等到位，等满超时后按错位置采图、
    再继续走下一个点 —— 按了停止又自己动起来。

    返回值里的 stop 说明这台设备实际做到了什么：hard = 立即停住并保持；
    soft = 只能软停（不再发新目标 + 当前位置写回），慢一个帧间隔、可能过冲。
    界面要按它说实话，别一律写「保持伺服」。
    """
    scan_aborted = scanner.busy()
    scanner.abort()
    result = stage.stop_motion()
    return {"ok": True, "scan_aborted": scan_aborted, "stop": result.value}


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
async def api_scan_start(req: ScanRequest) -> dict:
    """开始扫描。相机互斥由设备层保证（见下面那行注释），不靠界面先点一下。"""

    # 相机与位移台互斥：**不用在这里手动关预览** —— 扫描器一开始就跑
    # capture.begin() → begin_scan()，设备层会把取帧停下、会话留给扫描（状态变 held）。
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


@app.get("/api/scans/{scan_id}/pixel")
def api_scan_pixel(
    scan_id: int,
    x: int = Query(..., ge=0, description="像素列（保存的 PNG 自己的坐标，传感器朝向）"),
    y: int = Query(..., ge=0, description="像素行"),
) -> dict:
    """一条扫描里、**每个扫描点上同一个像素**的值 —— 数据处理页那条曲线的取数口子。

    数据全部来自各点**已经落盘的 PNG**：不是重采、不碰设备、不占设备带宽（只是读文件）。
    position_um 是**采图那一刻记下来的读出位置**（point.actual_um），曲线的横轴就是它 ——
    命令值只说明"想让台子去哪"，这里的每一个点都是"当时读数是多少"。
    没图的点给 null，**不补值、不插值**：曲线在那里断开。
    """
    from .thorlabs_ccd import png_pixel_series

    scan = store.get_scan(scan_id)
    if scan is None:
        raise HTTPException(404, "扫描不存在")
    points = store.get_points(scan_id)
    items = [
        (p["idx"], p["actual_um"],
         (DATA_DIR / p["image_path"]) if p["image_path"] else None)
        for p in points
    ]
    try:
        series = png_pixel_series(items, x, y)
    except ValueError as exc:                 # 点落在画面外：这是坐标填错了，说清楚
        raise HTTPException(400, str(exc)) from exc
    return {
        "scan_id": scan_id,
        "name": scan["name"],
        "status": scan["status"],
        "count": len(points),
        **series,
    }


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
# 前端是**零构建**的：改了 app.js，浏览器必须立刻拿到新的。Starlette 的文件响应只给
# ETag/Last-Modified、不带 Cache-Control —— 浏览器于是按「启发式新鲜度」（约 Last-Modified
# 到现在的 10%）直接吃缓存：等于改了前端却在浏览器里看不到（实测：新按钮点下去连请求都
# 没发出去，因为手里还是旧的 app.js）。这几个是自家文件，一律要求回源校验 —— ETag 在，
# 回源就是一次 304；vendor 里的大件（uPlot）保持默认可缓存。
def _own_asset(path: str) -> bool:
    """是不是「我们自己会改的文件」。用规则不用白名单：漏一个文件就再踩一次
    （而且白名单里写 /index.html 是死配置 —— 那条路由根本不存在）。vendor 里的大件
    （uPlot）不算自家文件，保持默认可缓存。"""
    return path == "/" or (path.startswith("/static/") and "/vendor/" not in path)


@app.middleware("http")
async def no_cache_for_own_assets(request: Request, call_next):
    response = await call_next(request)
    if _own_asset(request.url.path):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> HTMLResponse:
    """首页。给自家 JS/CSS 的 URL 带上文件 mtime（index.html 里写的是 ?v=dev）。

    零构建的前端改了文件就必须让浏览器拿到新的，光靠 Cache-Control 不够：已经缓存过旧文件
    的浏览器会按启发式新鲜度继续用旧的、连问都不问（实测反复踩：页面刷新了、HTML 也回源了，
    app.js 却还是旧的，于是新按钮点下去连请求都没有）。HTML 每次都回源，所以在这里换成带
    版本号的 URL 最稳 —— 文件一动，URL 就变。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        stamp = int((STATIC_DIR / name).stat().st_mtime)
        html, n = re.subn(re.escape(f"/static/{name}?v=dev"), f"/static/{name}?v={stamp}", html)
        if not n:
            # 占位符被改掉/删掉就会静默失效（浏览器又吃旧 JS）——所以这里必须吵一句
            log.error("index.html 里没有 /static/%s?v=dev 占位符：版本化 URL 没生效", name)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # workers 必须是 1：设备 owner 线程不能跨进程复制
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
