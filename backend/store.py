"""SQLite 持久化：扫描任务与每点元数据。

写入频率很低（每点一次，秒级），所以连接即用即关，不做连接池。
WAL 模式让"执行器写入"与"界面读取"互不阻塞。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .config import DATA_DIR, DB_PATH, IMAGE_DIR
from .stage_api import SETTLE_UNKNOWN

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    start_um    REAL    NOT NULL,
    stop_um     REAL    NOT NULL,
    count       INTEGER NOT NULL,
    settle_ms   INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    message     TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    finished_at REAL,
    -- 这条扫描的位置读回口径：**µm = 读回值 × 这个系数**（设备属性，建扫描时从设备抄下来）。
    -- NULL = 未记录（加这一列之前的老数据；界面上可以手工标一次，见 server.set_scan_readback）。
    -- 数据页「口径」按钮靠它把已存的 µm 反推成设备原值：**库里的数一个字节都不动**。
    readback_to_um REAL
);

CREATE TABLE IF NOT EXISTS point (
    scan_id     INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    target_um   REAL    NOT NULL,
    actual_um   REAL,
    settled_ms  REAL,
    on_target   INTEGER,
    settle_source TEXT,
    image_path  TEXT,
    -- 这一点**采图那一刻**用的曝光（µs，相机读回值）。NULL = 未记录：
    -- 占位相机没有曝光概念、没采到图的点、以及加这一列之前的老数据。
    exposure_us INTEGER,
    taken_at    REAL,
    PRIMARY KEY (scan_id, idx)
);

-- 手动保存的原始帧（「保存原生帧」存下的那些）。
-- **与扫描各点的图分开**：扫描图归 point.image_path，和它自己的扫描绑在一起；
-- 这里是独立的一张张帧，可以各自命名。
-- filename 就是主键：改名时**磁盘文件名和这条记录必须一起改**（见 server.rename_grab），
-- 只改一边就是"库里有、点开 404"或者"文件还在、列表里没了"。
-- 保存时的撞名由调用方避开（thorlabs_ccd.save_raw 发现同名就加 _2），
-- 下面的 ON CONFLICT 只是"同一张重复登记"的兜底，不是覆盖别人的入口。
CREATE TABLE IF NOT EXISTS grab (
    filename    TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);

-- 数据页「裁剪」的记录：**一条扫描一行**（同名的几条一起裁，共用同一个矩形）。
-- 口径是**原始坐标**：x0/y0 是这一刀在原始帧里的左上角，(w,h) 是裁下来的尺寸。
-- 取数时只在读文件那一层做一次减法（x_file = x - x0），别处一律原始坐标。
-- 和 PNG 自己 tEXt 里的 CropRect 各记一份，对不上就报错 —— 原点偏移错了不会报错，
-- 只会让曲线整体错位，两份记录是唯一能自动发现它的地方。
-- state：cropping 是"正在动文件"（进程被杀会留下这个状态，启动时按下面 recover 的规矩收尾），
-- done 才是裁完了。**只有 done 才允许取数按偏移读**（cropping 期间那些文件可能是缺的）。
CREATE TABLE IF NOT EXISTS crop (
    scan_id     INTEGER PRIMARY KEY REFERENCES scan(id) ON DELETE CASCADE,
    group_name  TEXT    NOT NULL,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    h           INTEGER NOT NULL,
    frames      INTEGER NOT NULL,
    saved_bytes INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
"""

# 已有库的补列。CREATE TABLE IF NOT EXISTS 对已存在的表**什么都不做**，
# 不加这一步，老 data/scans.db 上插点会因为缺列直接失败。
# 表名/列名是模块常量，不是外部输入。
MIGRATIONS = (
    # (表, 列, 类型, 老行补什么值)
    ("point", "settle_source", "TEXT", SETTLE_UNKNOWN),
    # 曝光对老行**没有**能补的值（当时没记），如实留 NULL —— 界面显示「—」，
    # 不许拿"现在的曝光"去倒填：那是编数，不是记录。
    ("point", "exposure_us", "INTEGER", None),
    # 老扫描没记是哪台设备采的，**不许猜一个系数填上**（猜错 = 整条曲线的横轴静默错位）。
    # 留 NULL 就是"未记录"，数据页只能标一次之后才能切原值。
    ("scan", "readback_to_um", "REAL", None),
)

# 终止态：进程启动时把这两个状态之外的残留扫描判为 aborted
FINAL_STATUS = ("done", "aborted", "failed")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, kind, backfill in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            # 回填放在 if **外面**、无条件跑：列可能是上一次启动补的，那时还没有回填逻辑，
            # 老行会一直是 NULL。WHERE ... IS NULL 让它幂等，每次启动跑一遍不花钱。
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} IS NULL", (backfill,)
            )
        # 上次进程被杀时留下的 running/paused 任务不可能再继续了
        conn.execute(
            "UPDATE scan SET status = 'aborted', message = '服务重启，任务中断',"
            " finished_at = ? WHERE status NOT IN (?, ?, ?)",
            (time.time(), *FINAL_STATUS),
        )


def create_scan(
    name: str,
    start_um: float,
    stop_um: float,
    count: int,
    settle_ms: int,
    readback_to_um: float,
) -> int:
    with _db() as conn:
        cur = conn.execute(
            # 直接写 running：这一行建好后立刻起扫描线程，pending 没有可观察的窗口
            "INSERT INTO scan (name, start_um, stop_um, count, settle_ms, status, created_at,"
            " readback_to_um) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (name, start_um, stop_um, count, settle_ms, time.time(), readback_to_um),
        )
        return int(cur.lastrowid)


def set_scan_readback(scan_id: int, readback_to_um: Optional[float]) -> None:
    """标一次「这条扫描是哪台设备采的」—— 只写口径，**不碰任何测量数据**。

    老扫描（这一列之前没有）没记设备，反推原值必须知道系数；系数只能由人指认。
    """
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET readback_to_um = ? WHERE id = ?", (readback_to_um, scan_id)
        )


def set_status(scan_id: int, status: str, message: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET status = ?, message = ? WHERE id = ?",
            (status, message, scan_id),
        )


def finish_scan(scan_id: int, status: str, message: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE scan SET status = ?, message = ?, finished_at = ? WHERE id = ?",
            (status, message, time.time(), scan_id),
        )


def add_point(
    scan_id: int,
    idx: int,
    target_um: float,
    actual_um: float,
    settled_ms: float,
    on_target: bool,
    settle_source: str,
    image_path: Optional[str],
    exposure_us: Optional[int] = None,
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO point"
            " (scan_id, idx, target_um, actual_um, settled_ms, on_target, settle_source,"
            "  image_path, exposure_us, taken_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, idx, target_um, actual_um, settled_ms, int(on_target), settle_source,
             image_path, exposure_us, time.time()),
        )


def register_grab(filename: str) -> None:
    """记下这一帧；已经有记录就不动它的名字（重复保存同名文件时才走 upsert）。"""
    with _db() as conn:
        conn.execute(
            "INSERT INTO grab (filename, created_at) VALUES (?, ?)"
            " ON CONFLICT(filename) DO UPDATE SET created_at = excluded.created_at",
            (filename, time.time()),
        )


def list_grabs() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM grab ORDER BY created_at DESC")]


def rename_grab(old: str, new: str) -> None:
    """把一帧的**文件名**改成 new（磁盘上真改），同时更新库里的记录。

    只改库不动文件，库里就会指向一个不存在的文件 —— 所以这两步必须一起做。
    真正的改名动作（同名冲突、非法字符、路径穿越的检查）在 server 层，
    因为那里才知道 IMAGE_DIR 在哪、以及要给用户报什么错。
    """
    with _db() as conn:
        conn.execute("UPDATE grab SET filename = ? WHERE filename = ?", (new, old))


def delete_grab(filename: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM grab WHERE filename = ?", (filename,))


def delete_grabs(filenames: list[str]) -> None:
    with _db() as conn:
        conn.executemany("DELETE FROM grab WHERE filename = ?", [(f,) for f in filenames])


def list_scans(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM point p WHERE p.scan_id = s.id) AS done"
            " FROM scan s ORDER BY s.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scan(scan_id: int) -> Optional[dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM scan WHERE id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def get_points(scan_id: int, limit: int = 200_000) -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM point WHERE scan_id = ? ORDER BY idx LIMIT ?", (scan_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_scan(scan_id: int) -> None:
    """删扫描连同它的图像。point 行由外键级联删除，这里只先取出图像路径。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT image_path FROM point WHERE scan_id = ? AND image_path IS NOT NULL",
            (scan_id,),
        ).fetchall()
        conn.execute("DELETE FROM scan WHERE id = ?", (scan_id,))
    for row in rows:
        # 尽力而为：文件被占用/只读不该让已经删掉的 DB 行回滚成 500
        try:
            (DATA_DIR / row["image_path"]).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除图像 %s 失败：%s", row["image_path"], exc)


# ------------------------------------------------------------------ 数据页：裁剪
# 记录本身只管"这一刀在哪、什么状态"；动文件那套在 thorlabs_ccd.crop_frames 里。
# **先落库（cropping）再动文件**：进程被杀时靠这条记录判断该往哪边收尾（见 recover_crops）。

def scans_named(name: str) -> list[dict[str, Any]]:
    """同名的那几条扫描 —— 裁剪一次对整组动手，先把它们都取出来。"""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM scan WHERE name = ? ORDER BY id", (name,)).fetchall()
    return [dict(r) for r in rows]


def scan_groups() -> list[dict[str, Any]]:
    """按名字把扫描分组（数据页「扫描组」那一栏）。

    组内只认**一个**矩形：几条扫描若记着不同的矩形，rect 就给 None（界面显示"不统一"）——
    整组一起裁的全部意义就是组内坐标完全一致，出现两种框说明有人绕过界面动过库。
    """
    with _db() as conn:
        scans = conn.execute(
            "SELECT s.id, s.name, s.status,"
            " (SELECT COUNT(*) FROM point p WHERE p.scan_id = s.id) AS points,"
            " (SELECT COUNT(*) FROM point p WHERE p.scan_id = s.id"
            "  AND p.image_path IS NOT NULL) AS images"
            " FROM scan s ORDER BY s.id"
        ).fetchall()
        crops = {r["scan_id"]: dict(r) for r in conn.execute("SELECT * FROM crop")}
    groups: dict[str, dict[str, Any]] = {}
    for r in scans:
        g = groups.setdefault(r["name"], {
            "name": r["name"], "scans": 0, "points": 0, "images": 0,
            "cropped": 0, "cropping": 0, "saved_bytes": 0, "rects": set(), "last_id": r["id"],
        })
        g["scans"] += 1
        g["points"] += r["points"]
        g["images"] += r["images"]
        g["last_id"] = r["id"]
        c = crops.get(r["id"])
        if c:
            g["cropped" if c["state"] == "done" else "cropping"] += 1
            g["saved_bytes"] += c["saved_bytes"]
            g["rects"].add((c["x0"], c["y0"], c["w"], c["h"]))
    out = []
    for g in groups.values():
        rects = g.pop("rects")
        g["rect"] = list(rects.pop()) if len(rects) == 1 else None
        out.append(g)
    return sorted(out, key=lambda g: g["last_id"], reverse=True)


def get_crop(scan_id: int) -> Optional[dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM crop WHERE scan_id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def all_crops() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM crop").fetchall()
    return [dict(r) for r in rows]


def put_crops(records: list[dict[str, Any]]) -> None:
    """一批裁剪记录，**一个事务**写进去（半批记录比没有记录更难查）。

    同一个扫描**再裁**是更新同一条记录（scan_id 就是主键）：矩形换成新的，
    saved_bytes **累加**（那个数问的是"这条一共省了多少"，不是"最后一刀省了多少"）。
    created_at 不动 —— 它记的是这一组第一次挨裁的时刻。
    """
    with _db() as conn:
        conn.executemany(
            "INSERT INTO crop (scan_id, group_name, x0, y0, w, h, frames, saved_bytes,"
            " state, created_at) VALUES (:scan_id, :group_name, :x0, :y0, :w, :h, :frames,"
            " :saved_bytes, :state, :created_at)"
            " ON CONFLICT(scan_id) DO UPDATE SET"
            " group_name = excluded.group_name,"
            " x0 = excluded.x0, y0 = excluded.y0, w = excluded.w, h = excluded.h,"
            " frames = excluded.frames,"
            " saved_bytes = crop.saved_bytes + excluded.saved_bytes,"
            " state = excluded.state",
            records,
        )


def finish_crop(scan_id: int, frames: int, saved_bytes: int) -> None:
    """裁完了：补上这条扫描实际裁了多少张、省了多少字节，并把状态改成 done。"""
    with _db() as conn:
        conn.execute(
            "UPDATE crop SET frames = ?, saved_bytes = saved_bytes + ?, state = 'done'"
            " WHERE scan_id = ?",
            (frames, saved_bytes, scan_id),
        )


def set_crop_rects(records: list[dict[str, Any]]) -> None:
    """把矩形改回**某一批开工之前**的样子（进程被杀在裁剪半路时收尾用）。

    只动矩形：frames/saved_bytes 是"干过多少活"的流水，不是坐标，改回去反而对不上。
    """
    with _db() as conn:
        conn.executemany(
            "UPDATE crop SET x0 = :x0, y0 = :y0, w = :w, h = :h WHERE scan_id = :scan_id",
            records,
        )


def set_crop_state(scan_ids: list[int], state: str) -> None:
    with _db() as conn:
        conn.executemany("UPDATE crop SET state = ? WHERE scan_id = ?",
                         [(state, sid) for sid in scan_ids])


def delete_crops(scan_ids: list[int]) -> None:
    with _db() as conn:
        conn.executemany("DELETE FROM crop WHERE scan_id = ?", [(sid,) for sid in scan_ids])
